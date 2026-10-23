import base64
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import app
import panel
from starlette.requests import Request


def make_request(headers=None):
    return Request({"type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
                    "path": "/api/sync/manifest", "raw_path": b"/api/sync/manifest", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
                    "client": ("testclient", 1234), "server": ("testserver", 443), "root_path": ""})


class FakeConn:
    def __init__(self, users=None, server=None, status="active", servers=None, user_servers=None):
        self.users = users or []
        self.server = server
        self.servers = servers if servers is not None else ([server] if server else [])
        self.user_servers = user_servers or []
        self.status = status
        self.deleted = False

    async def execute(self, query, *args):
        if query.startswith("DELETE FROM users"):
            self.deleted = True
        elif query.startswith("DELETE FROM servers WHERE id=ANY"):
            target_id, old_ids = args
            self.servers[:] = [row for row in self.servers
                               if row.get("id") == target_id or row.get("id") not in old_ids]
            self.user_servers[:] = [row for row in self.user_servers if row.get("server_id") == target_id
                                    or row.get("server_id") not in old_ids]
        elif query.startswith("UPDATE servers SET present_upstream=FALSE"):
            for row in self.servers:
                row["present_upstream"] = False
        elif query.startswith("INSERT INTO servers(id,original_name,source_key"):
            server_id, original_name, source_key, sort_order = args
            row = next((item for item in self.servers if item.get("id") == server_id), None)
            if row is None:
                row = {"id": server_id, "original_name": original_name, "source_key": source_key,
                       "custom_name": None, "hidden": False, "sort_order": sort_order,
                       "present_upstream": True}
                self.servers.append(row)
            else:
                row.update(original_name=original_name, source_key=source_key, present_upstream=True)
        elif query.startswith("UPDATE servers AS target SET"):
            target_id, old_ids = args
            target = next((item for item in self.servers if item.get("id") == target_id), None)
            old_rows = [item for item in self.servers if item.get("id") in old_ids and item.get("id") != target_id]
            if target:
                target["custom_name"] = target.get("custom_name") or next(
                    (item.get("custom_name") for item in sorted(old_rows, key=lambda x: x.get("last_seen_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True) if item.get("custom_name")), None)
                target["hidden"] = bool(target.get("hidden")) or any(item.get("hidden") for item in old_rows)
        elif query.startswith("INSERT INTO user_servers(user_id,server_id,hidden,custom_name)"):
            target_id, old_ids = args
            for item in list(self.user_servers):
                if item.get("server_id") not in old_ids:
                    continue
                target = next((row for row in self.user_servers if row.get("user_id") == item.get("user_id")
                               and row.get("server_id") == target_id), None)
                if target is None:
                    self.user_servers.append({**item, "server_id": target_id})
                else:
                    if target.get("hidden") is None:
                        target["hidden"] = item.get("hidden")
                    if target.get("custom_name") is None:
                        target["custom_name"] = item.get("custom_name")
        return "UPDATE 1"

    async def fetch(self, query, *args):
        if "SELECT * FROM users" in query:
            return self.users
        if "SELECT * FROM servers" in query:
            if "WHERE present_upstream=TRUE" in query:
                return [row for row in self.servers if row.get("present_upstream", True)]
            return self.servers
        if "SELECT id,original_name,source_key,custom_name,hidden,sort_order" in query:
            return self.servers
        if "SELECT server_id,COUNT(*) AS override_count FROM user_servers" in query:
            counts = {}
            for row in self.user_servers:
                counts[row["server_id"]] = counts.get(row["server_id"], 0) + 1
            return [{"server_id": key, "override_count": value} for key, value in counts.items()]
        if "SELECT id,original_name,custom_name,hidden,sort_order FROM servers" in query:
            if "WHERE present_upstream=TRUE" in query:
                return [row for row in self.servers if row.get("present_upstream", True)]
            return self.servers
        if "SELECT server_id,hidden,custom_name FROM user_servers" in query:
            return self.user_servers
        if "FROM servers s LEFT JOIN user_servers" in query:
            rows = []
            for server in self.servers:
                if "WHERE s.present_upstream=TRUE" in query and not server.get("present_upstream", True):
                    continue
                override = next((item for item in self.user_servers if item.get("server_id") == server["id"]
                                 and item.get("user_id") == args[0]), {})
                rows.append({"id": server["id"], "original_name": server["original_name"],
                             "global_name": server.get("custom_name"), "global_hidden": server.get("hidden", False),
                             "sort_order": server.get("sort_order", 0),
                             "present_upstream": server.get("present_upstream", True),
                             "user_hidden": override.get("hidden"), "user_name": override.get("custom_name")})
            return rows
        return []

    async def fetchrow(self, query, *args):
        if "SELECT id,status FROM users" in query:
            return {"id": args[0], "status": self.status}
        return None

    async def fetchval(self, query, *args):
        if "FROM users WHERE id=$1" in query:
            return any(user.get("id") == args[0] for user in self.users)
        if "FROM servers WHERE id=$1" in query:
            return any(row.get("id") == args[0] and row.get("present_upstream", True) for row in self.servers)
        return False

    def transaction(self):
        return FakeAcquire(self)


class FakeAcquire:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *args): return False


class FakePool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return FakeAcquire(self.conn)


class DatabaseUrlTests(unittest.TestCase):
    def test_encoded_space_and_at_in_password(self):
        parsed = panel.parse_database_url(
            "postgresql://postgres:My happy@life%2064@db.example.supabase.co:5432/postgres"
        )
        self.assertEqual(parsed["password"], "My happy@life 64")
        self.assertEqual(parsed["port"], 5432)
        self.assertEqual(parsed["statement_cache_size"], 100)
        self.assertEqual(parsed["ssl"], "require")

    def test_raw_space_and_session_pooler_parameters(self):
        parsed = panel.parse_database_url(
            "postgres://postgres.ref:My happy life@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
        )
        self.assertEqual(parsed["user"], "postgres.ref")
        self.assertEqual(parsed["password"], "My happy life")
        self.assertEqual(parsed["statement_cache_size"], 0)


class UriTests(unittest.TestCase):
    def test_protocol_renames_preserve_uri_and_escape_fragment(self):
        cases = [
            "vless://uuid@host:443?security=tls#old",
            "trojan://password@host:443#old",
            "ss://YWVzLTI1Ni1nY206cGFzcw@host:443#old",
            "hysteria2://password@host:443#old",
            "tuic://uuid:password@host:443#old",
        ]
        for uri in cases:
            with self.subTest(uri=uri):
                parsed = panel.parse_server_uri(uri)
                renamed = panel.rename_server_uri(uri, "Name with space")
                self.assertIn("#Name%20with%20space", renamed)
                self.assertEqual(parsed["id"], panel.parse_server_uri(uri.split("#", 1)[0] + "#different")["id"])

    def test_vmess_ps_is_excluded_from_stable_id_and_renamed(self):
        first = {"v": "2", "add": "server", "port": "443", "id": "uuid", "ps": "first"}
        second = dict(first, ps="second")
        encode = lambda value: "vmess://" + base64.b64encode(json.dumps(value).encode()).decode()
        uri1, uri2 = encode(first), encode(second)
        self.assertEqual(panel.parse_server_uri(uri1)["id"], panel.parse_server_uri(uri2)["id"])
        renamed = panel.rename_server_uri(uri1, "Renamed")
        obj = json.loads(base64.b64decode(renamed[8:]))
        self.assertEqual(obj["ps"], "Renamed")

    def test_source_name_key_is_stable_across_case_and_unicode_spacing(self):
        first = panel.parse_server_uri("vless://old@old.example#  Ｓerver   North ")
        second = panel.parse_server_uri("vless://new@new.example#server north")
        self.assertEqual(first["source_key"], second["source_key"])
        self.assertNotEqual(first["id"], second["id"])

    def test_subscription_base64_decodes_only_when_it_contains_uris(self):
        raw = "vless://user@host\n"
        self.assertEqual(panel._decode_subscription(base64.b64encode(raw.encode())), raw.strip())
        self.assertIsNotNone(panel._invalid_payload(base64.b64encode(b"DEVICE LIMIT FULL"), ""))

    def test_duplicate_server_entries_with_different_names_collapse(self):
        entries = "vless://id@host:443#First\nvless://id@host:443#Second\n"
        self.assertEqual(panel._extract_uris(entries), ["vless://id@host:443#First"])


class ServerReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_uri_keeps_global_and_per_user_customizations(self):
        previous = panel.parse_server_uri("vless://old-id@old.example:443#Nord")
        changed = panel.parse_server_uri("vless://new-id@new.example:8443#Nord")
        stale = panel.parse_server_uri("trojan://gone@gone.example:443#Removed")
        old_row = {"id": previous["id"], "original_name": "Nord", "source_key": previous["source_key"],
                   "custom_name": "My Nord", "hidden": True, "sort_order": 7,
                   "present_upstream": True}
        stale_row = {"id": stale["id"], "original_name": "Removed", "source_key": stale["source_key"],
                     "custom_name": None, "hidden": False, "sort_order": 8,
                     "present_upstream": True}
        personal = {"user_id": 5, "server_id": previous["id"], "hidden": False,
                    "custom_name": "Personal Nord"}
        conn = FakeConn(servers=[old_row, stale_row], user_servers=[personal])

        synced = await panel._sync_servers(conn, f"{changed['uri']}\n".encode())

        self.assertEqual(len(synced), 1)
        self.assertEqual(synced[0]["id"], previous["id"])
        self.assertEqual(synced[0]["uri"], changed["uri"])
        active = [row for row in conn.servers if row.get("present_upstream")]
        self.assertEqual([row["id"] for row in active], [previous["id"]])
        self.assertEqual(active[0]["custom_name"], "My Nord")
        self.assertTrue(active[0]["hidden"])
        self.assertEqual(active[0]["sort_order"], 7)
        self.assertEqual(personal["server_id"], previous["id"])
        self.assertEqual(personal["custom_name"], "Personal Nord")
        self.assertFalse(stale_row["present_upstream"])
        user = {"id": 5, "slug": "person-5", "enabled": True, "expires_at": None,
                "announce_override": "", "title_override": None, "status": "active",
                "drive_file_id": None}
        content = await panel.build_manifest_content(
            conn, user, changed["uri"].encode(), {},
            dict(panel.DEFAULT_SETTINGS, announce_enabled=False), servers=synced)
        self.assertIn("vless://new-id@new.example:8443#Personal%20Nord", content)

    async def test_historical_uri_rows_merge_personal_overrides_into_survivor(self):
        first = panel.parse_server_uri("vless://first@one.example#North")
        second = panel.parse_server_uri("vless://second@two.example#North")
        current = panel.parse_server_uri("vless://current@three.example#North")
        old_global = {"id": first["id"], "original_name": "North", "source_key": first["source_key"],
                      "custom_name": "North Custom", "hidden": False, "sort_order": 2,
                      "present_upstream": False}
        old_personal = {"id": second["id"], "original_name": "North", "source_key": second["source_key"],
                        "custom_name": None, "hidden": False, "sort_order": 3,
                        "present_upstream": False}
        override = {"user_id": 12, "server_id": second["id"], "hidden": True,
                    "custom_name": "My North"}
        conn = FakeConn(servers=[old_global, old_personal], user_servers=[override])

        result = await panel._sync_servers(conn, current["uri"].encode())

        self.assertEqual(result[0]["id"], second["id"])
        self.assertEqual(len([row for row in conn.servers if row.get("present_upstream")]), 1)
        survivor = next(row for row in conn.servers if row.get("id") == second["id"])
        self.assertEqual(survivor["custom_name"], "North Custom")
        migrated = next(row for row in conn.user_servers if row.get("server_id") == second["id"])
        self.assertEqual(migrated["custom_name"], "My North")
        self.assertTrue(migrated["hidden"])

    async def test_server_list_refreshes_snapshot_and_hides_old_rows(self):
        active = panel.parse_server_uri("vless://current@current.example#Current")
        old = panel.parse_server_uri("vless://old@old.example#Old")
        conn = FakeConn(servers=[
            {"id": active["id"], "original_name": "Current", "source_key": active["source_key"],
             "custom_name": None, "hidden": False, "sort_order": 0, "present_upstream": True},
            {"id": old["id"], "original_name": "Old", "source_key": old["source_key"],
             "custom_name": None, "hidden": False, "sort_order": 1, "present_upstream": True},
        ])
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(active["uri"].encode(), {}, False))), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))):
            result = await panel.servers_list(make_request())

        self.assertEqual([row["original_name"] for row in result["servers"]], ["Current"])
        self.assertFalse(next(row for row in conn.servers if row["id"] == old["id"])["present_upstream"])

    async def test_user_server_list_only_returns_current_upstream_servers(self):
        current = panel.parse_server_uri("vless://now@current.example#Current")
        old = panel.parse_server_uri("vless://past@old.example#Old")
        conn = FakeConn(users=[{"id": 24}], servers=[
            {"id": current["id"], "original_name": "Current", "source_key": current["source_key"],
             "custom_name": None, "hidden": False, "sort_order": 0, "present_upstream": True},
            {"id": old["id"], "original_name": "Old", "source_key": old["source_key"],
             "custom_name": None, "hidden": False, "sort_order": 1, "present_upstream": True},
        ])
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(current["uri"].encode(), {}, False))), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))):
            result = await panel.user_servers_list(24, make_request())

        self.assertEqual([row["original_name"] for row in result["servers"]], ["Current"])

    async def test_server_list_does_not_label_stale_fallback_as_current(self):
        pool = AsyncMock()
        db_mock = AsyncMock(return_value=pool)
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(b"old", {}, True))), \
             patch.object(panel.state, "db", new=db_mock):
            with self.assertRaises(HTTPException) as raised:
                await panel.servers_list(make_request())
            self.assertEqual(raised.exception.status_code, 503)
            db_mock.assert_not_awaited()

class UpstreamCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = panel.PanelState()

    async def asyncTearDown(self):
        await self.state.close()

    async def test_empty_cache_manifest_fetch_uses_happ_user_agent(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, content=b"vless://id@server#one", headers={"content-type": "text/plain"})

        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"SUBSCRIPTION_URL": "https://subscription.invalid/path"}, clear=True):
            body, _, stale = await self.state.fetch_upstream()
        self.assertEqual(body, b"vless://id@server#one")
        self.assertFalse(stale)
        self.assertEqual(seen["user-agent"], "Happ")
        self.assertNotIn("python-httpx", seen["user-agent"].lower())

    async def test_stale_cache_survives_failed_refresh(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, content=b"vless://id@server", headers={"content-type": "text/plain"})
            return httpx.Response(200, text="<html>not a subscription</html>", headers={"content-type": "text/html"})

        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"SUBSCRIPTION_URL": "https://subscription.invalid/path"}, clear=True):
            good, _, _ = await self.state.fetch_upstream()
            self.state.upstream_fetched_at = 0
            stale, _, was_stale = await self.state.fetch_upstream()
        self.assertEqual(stale, good)
        self.assertTrue(was_stale)
        self.assertIn("HTML", self.state.upstream_warning)

    async def test_device_limit_on_cold_start_is_not_cached(self):
        placeholder = base64.b64encode(b"DEVICE LIMIT FULL (5/5) - remove a device first")
        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=placeholder, headers={"content-type": "text/plain"})
        ))
        with patch.dict(os.environ, {"SUBSCRIPTION_URL": "https://subscription.invalid/path"}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                await self.state.fetch_upstream()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIsNone(self.state.upstream_body)

    async def test_manifest_endpoint_fetches_itself_on_cold_start(self):
        # The fetch path is the one called by /api/sync/manifest; it doesn't depend on a relay GET.
        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"vless://id@server", headers={"content-type": "text/plain"})
        ))
        with patch.dict(os.environ, {"SUBSCRIPTION_URL": "https://subscription.invalid/path"}, clear=True):
            body, _, stale = await self.state.fetch_upstream()
        self.assertIn(b"vless://", body)
        self.assertFalse(stale)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_key_and_csrf_are_enforced(self):
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            relay_health = await client.get("/health")
            self.assertEqual(relay_health.text, "ok")
            self.assertNotIn("content-security-policy", relay_health.headers)
            with patch.dict(os.environ, {"SYNC_KEY": "secret"}, clear=False):
                bad = await client.get("/api/sync/manifest", headers={"X-Sync-Key": "wrong"})
                self.assertEqual(bad.status_code, 401)
            with patch.dict(os.environ, {"SYNC_KEY": ""}, clear=False):
                disabled = await client.get("/api/sync/manifest")
                self.assertEqual(disabled.status_code, 503)

    async def test_login_and_csrf_protected_logout(self):
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "correct", "ADMIN_PASSWORD_HASH": "", "SESSION_SECRET": "s" * 48}, clear=False):
                bad = await client.post("/api/admin/auth/login", json={"username": "admin", "password": "wrong"})
                self.assertEqual(bad.status_code, 401)
                good = await client.post("/api/admin/auth/login", json={"username": "admin", "password": "correct"})
                self.assertEqual(good.status_code, 200)
                csrf = good.json()["csrf_token"]
                no_csrf = await client.post("/api/admin/auth/logout", json={})
                self.assertEqual(no_csrf.status_code, 403)
                yes_csrf = await client.post("/api/admin/auth/logout", json={}, headers={"X-CSRF-Token": csrf})
                self.assertEqual(yes_csrf.status_code, 200)


class DriveQrTests(unittest.IsolatedAsyncioTestCase):
    async def test_users_list_preloads_copyable_link_only_for_ready_drive_file(self):
        conn = FakeConn(users=[{"id": 8, "drive_file_id": "ready-file"}, {"id": 9, "drive_file_id": None}])
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.dict(os.environ, {"DRIVE_API_KEY": "list-key"}, clear=False):
            result = await panel.users_list(make_request())

        self.assertEqual(result["users"][0]["drive_link"],
                         "https://www.googleapis.com/drive/v3/files/ready-file?key=list-key&alt=media")
        self.assertIsNone(result["users"][1]["drive_link"])

    async def test_drive_link_and_qr_use_same_private_download_url(self):
        class DriveConn(FakeConn):
            async def fetchrow(self, query, *args):
                if "SELECT drive_file_id FROM users" in query:
                    return {"drive_file_id": "drive-file-id"}
                return await super().fetchrow(query, *args)

        conn = DriveConn()
        request = make_request()
        expected = "https://www.googleapis.com/drive/v3/files/drive-file-id?key=test-key&alt=media"
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}, clear=False), \
             patch.object(panel, "_drive_qr_svg", return_value="<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>") as render_qr:
            link_result = await panel.drive_link(7, request)
            qr_result = await panel.drive_qr(7, request)

        self.assertEqual(link_result["link"], expected)
        render_qr.assert_called_once_with(expected)
        self.assertEqual(qr_result.body, b"<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>")
        self.assertTrue(qr_result.headers["content-type"].startswith("image/svg+xml"))
        self.assertEqual(qr_result.headers["cache-control"], "no-store")

    async def test_qr_route_is_mounted_and_returns_image_response(self):
        class DriveConn(FakeConn):
            async def fetchrow(self, query, *args):
                if "SELECT drive_file_id FROM users" in query:
                    return {"drive_file_id": "mounted-file"}
                return await super().fetchrow(query, *args)

        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(DriveConn()))), \
             patch.dict(os.environ, {"DRIVE_API_KEY": "mounted-key"}, clear=False):
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
                response = await client.get("/api/admin/users/13/drive-qr.svg")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(b"<svg", response.content)

    def test_qr_renderer_returns_standalone_svg(self):
        svg = panel._drive_qr_svg("https://www.googleapis.com/drive/v3/files/example?key=secret&alt=media")
        self.assertIn("<svg", svg)
        self.assertIn("<path", svg)


class ManifestTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_user_traffic_toggle_uses_explicit_choice_or_admin_default(self):
        class CreateConn(FakeConn):
            async def fetchval(self, query, *args):
                return 0
            async def fetchrow(self, query, *args):
                self.insert_query = query
                self.insert_args = args
                return {"id": 41, "hide_traffic_usage": args[-1]}

        for explicit, default, expected in ((None, True, True), (False, True, False), (True, False, True)):
            with self.subTest(explicit=explicit, default=default):
                conn = CreateConn()
                pool = FakePool(conn)
                with patch.object(panel, "_auth"), \
                     patch.object(panel.state, "db", new=AsyncMock(return_value=pool)), \
                     patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                         panel.DEFAULT_SETTINGS, hide_traffic_for_new_users=default))), \
                     patch.object(panel.state, "push_drive_sync", new=AsyncMock(return_value={"ok": True})):
                    data = panel.UserBody(name="New user", hide_traffic_usage=explicit)
                    result = await panel.users_create(data, make_request())
                self.assertEqual(result["user"]["hide_traffic_usage"], expected)
                self.assertIn("hide_traffic_usage", conn.insert_query)

    async def test_manifest_builds_active_and_disabled_profiles(self):
        server = panel.parse_server_uri("vless://id@node.example#Origin")
        server_row = {"id": server["id"], "original_name": "Origin", "custom_name": None, "hidden": False, "sort_order": 0}
        active = {"id": 1, "slug": "active", "name": "Active", "note": "", "enabled": True,
                  "expires_at": None, "title_override": None, "announce_override": None, "status": "active", "drive_file_id": None}
        disabled = dict(active, id=2, slug="disabled", name="Disabled", enabled=False)
        conn = FakeConn(users=[active, disabled], server=server_row)
        fake_pool = FakePool(conn)
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=fake_pool)), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(b"vless://id@node.example#Origin", {"subscription-userinfo": "upload=3; download=8; total=100; expire=0"}, False))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(panel.DEFAULT_SETTINGS, announce_enabled=False))):
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))
        self.assertEqual(len(result["users"]), 2)
        self.assertIn("vless://", result["users"][0]["content"])
        self.assertEqual(result["users"][1]["content"].count("vless://"), 1)
        self.assertIn("127.0.0.1:1", result["users"][1]["content"])
        self.assertIn("⛔ Подписка отключена", base64.b64decode(result["users"][1]["content"].split("#announce: base64:")[1].splitlines()[0]).decode())
        self.assertIn("vless://id@node.example", result["users"][0]["content"])
        self.assertIn("#subscription-userinfo: upload=3; download=8; total=100; expire=0", result["users"][0]["content"])
        self.assertIn("#subscription-userinfo: upload=3; download=8; total=100; expire=0", result["users"][1]["content"])

    async def test_global_and_user_overrides_control_order_names_and_visibility(self):
        a_uri, b_uri = "vless://a@a.example#A", "trojan://b@b.example#B"
        a, b = panel.parse_server_uri(a_uri), panel.parse_server_uri(b_uri)
        user = {"id": 4, "enabled": True, "expires_at": None, "announce_override": "", "title_override": None, "status": "active"}
        conn = FakeConn(servers=[
            {"id": b["id"], "original_name": "B", "custom_name": "Global B", "hidden": True, "sort_order": 0},
            {"id": a["id"], "original_name": "A", "custom_name": "Global A", "hidden": False, "sort_order": 1},
        ], user_servers=[{"server_id": b["id"], "hidden": False, "custom_name": "Personal B"}])
        content = await panel.build_manifest_content(conn, user, (a_uri + "\n" + b_uri).encode(), {}, dict(panel.DEFAULT_SETTINGS, announce_enabled=False))
        self.assertLess(content.index("Personal%20B"), content.index("Global%20A"))
        self.assertIn("Personal%20B", content)
        self.assertIn("Global%20A", content)

    async def test_user_expiry_overrides_provider_expiry_and_uses_one_placeholder(self):
        expires = datetime.now(timezone.utc) - timedelta(days=2)
        user = {"id": 5, "enabled": True, "expires_at": expires, "announce_override": None,
                "title_override": None, "status": "active"}
        conn = FakeConn(server={"id": "unused", "original_name": "Server", "custom_name": None, "hidden": False, "sort_order": 0})
        content = await panel.build_manifest_content(conn, user, b"vless://id@host#name",
            {"subscription-userinfo": "upload=1; download=2; total=500; expire=1"},
            dict(panel.DEFAULT_SETTINGS, announce_enabled=False))
        self.assertIn(f"#subscription-userinfo: upload=1; download=2; total=500; expire={int(expires.timestamp())}", content)
        self.assertEqual(content.count("vless://"), 1)
        self.assertIn("127.0.0.1:1", content)
        self.assertIn(panel.quote("Время подписки закончилось", safe=""), content)
        encoded_announce = content.split("#announce: base64:", 1)[1].splitlines()[0]
        self.assertIn("Время подписки закончилось", base64.b64decode(encoded_announce).decode())

    async def test_future_expiry_keeps_real_servers_until_deadline(self):
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        uri = "vless://id@node.example#Server"
        server = panel.parse_server_uri(uri)
        conn = FakeConn(server={"id": server["id"], "original_name": "Server", "custom_name": None,
                               "hidden": False, "sort_order": 0})
        user = {"id": 50, "slug": "timed", "name": "Timed", "enabled": True, "expires_at": expires,
                "announce_override": None, "title_override": None, "status": "active"}
        content = await panel.build_manifest_content(conn, user, uri.encode(),
            {"subscription-userinfo": "upload=1; download=2; total=3; expire=0"},
            dict(panel.DEFAULT_SETTINGS, announce_enabled=False), servers=[server])
        self.assertIn(uri, content)
        self.assertNotIn("127.0.0.1:1", content)
        self.assertIn(f"expire={int(expires.timestamp())}", content)

    async def test_user_can_hide_shared_counter_but_legacy_always_shows_it(self):
        uri = "vless://id@node.example#Origin"
        server = panel.parse_server_uri(uri)
        conn = FakeConn(server={"id": server["id"], "original_name": "Origin", "custom_name": None,
                               "hidden": False, "sort_order": 0})
        headers = {"subscription-userinfo": "upload=11; download=22; total=33; expire=0"}
        settings = dict(panel.DEFAULT_SETTINGS, announce_enabled=False)
        user = {"id": 11, "slug": "person-1", "name": "Person", "enabled": True, "expires_at": None,
                "title_override": None, "announce_override": None, "status": "active", "hide_traffic_usage": True}
        legacy = dict(user, id=12, slug="legacy", name="Основная", drive_file_id=panel.LEGACY_DRIVE_FILE_ID)

        hidden = await panel.build_manifest_content(conn, user, uri.encode(), headers, settings, servers=[server])
        owner = await panel.build_manifest_content(conn, legacy, uri.encode(), headers, settings, servers=[server])
        self.assertNotIn("#subscription-userinfo:", hidden)
        self.assertIn("#subscription-userinfo: upload=11; download=22; total=33; expire=0", owner)

    async def test_user_counter_defaults_to_visible_and_uses_provider_values(self):
        uri = "vless://id@node.example#Origin"
        server = panel.parse_server_uri(uri)
        conn = FakeConn(server={"id": server["id"], "original_name": "Origin", "custom_name": None,
                               "hidden": False, "sort_order": 0})
        user = {"id": 13, "slug": "person-2", "name": "Person", "enabled": True, "expires_at": None,
                "title_override": None, "announce_override": None, "status": "active"}
        content = await panel.build_manifest_content(conn, user, uri.encode(),
            {"subscription-userinfo": "upload=100; download=200; total=1000; expire=0"},
            dict(panel.DEFAULT_SETTINGS, announce_enabled=False), servers=[server])
        self.assertIn("#subscription-userinfo: upload=100; download=200; total=1000; expire=0", content)

    async def test_admin_routes_choose_separate_desktop_and_mobile_pages(self):
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            selector = await client.get("/admin")
            desktop = await client.get("/admin/desktop")
            mobile = await client.get("/admin/mobile")
            javascript = await client.get("/static/admin.js")
            action_script = await client.get("/static/admin-actions.js")
            device_script = await client.get("/static/admin-device.js")
            stylesheet = await client.get("/static/admin.css")
        self.assertEqual(selector.status_code, 200)
        self.assertIn("img-src 'self' data: blob:", selector.headers.get("content-security-policy", ""))
        self.assertIn("matchMedia", selector.text)
        self.assertIn("/admin/mobile", selector.text)
        self.assertIn("/admin/desktop", selector.text)
        self.assertEqual(desktop.status_code, 200)
        self.assertEqual(mobile.status_code, 200)
        self.assertIn('class="desktop-ui"', desktop.text)
        self.assertIn('class="mobile-ui"', mobile.text)
        self.assertIn("/static/admin.js?v=20261021-4", desktop.text)
        self.assertIn("/static/admin.js?v=20261021-4", mobile.text)
        self.assertIn("/static/admin-actions.js?v=20261021-4", desktop.text)
        self.assertIn("/static/admin-actions.js?v=20261021-4", mobile.text)
        self.assertIn("/static/admin.css?v=20261021-4", mobile.text)
        self.assertEqual(javascript.status_code, 200)
        self.assertIn('name="expires_at" type="datetime-local"', javascript.text)
        self.assertIn("его Drive-файл навсегда", javascript.text)
        self.assertIn('class="button button-secondary user-qr-action" data-action="qr" data-user-id=', javascript.text)
        self.assertIn('data-action="copy-link" data-link=', javascript.text)
        self.assertNotIn('data-action="qr" data-link=', javascript.text)
        self.assertIn("showDriveQr(shareAction.userId)", javascript.text)
        self.assertIn("driveQrLoadingContent()", javascript.text)
        self.assertIn("credentials: 'same-origin'", javascript.text)
        self.assertNotIn("function driveShareContent(userId, link, includeQr)", javascript.text)
        self.assertIn("document.execCommand('copy')", javascript.text)
        self.assertIn("navigator.clipboard.writeText(value)", javascript.text)
        self.assertIn("selectForCopy(visibleField)", javascript.text)
        self.assertLess(javascript.text.index("if (shareAction?.kind === 'qr')"),
                        javascript.text.index("shareAction?.kind === 'copy-link'"))
        self.assertNotIn("button.dataset.qr", javascript.text)
        self.assertNotIn("button.dataset.link", javascript.text)
        self.assertEqual(device_script.status_code, 200)
        self.assertIn("matchMedia", device_script.text)
        self.assertEqual(action_script.status_code, 200)
        self.assertIn("resolveUserAction", action_script.text)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("no-cache", javascript.headers.get("cache-control", ""))
        self.assertIn("no-cache", stylesheet.headers.get("cache-control", ""))
        self.assertIn("prefers-reduced-motion", stylesheet.text)
        self.assertIn(".hidden { display: none !important; }", stylesheet.text)
        self.assertIn(".mobile-content .user-actions .user-qr-action", stylesheet.text)
        self.assertIn("Liquid glass pass", stylesheet.text)
        self.assertIn("blur(52px) saturate(190%)", stylesheet.text)
        self.assertNotIn(".mobile-shell { display: block !important; }", stylesheet.text)

    async def test_pending_delete_is_removed_only_after_success_report(self):
        conn = FakeConn(status="pending_delete")
        fake_pool = FakePool(conn)
        data = panel.SyncReport(user_id=9, drive_file_id="drive-id", ok=True)
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), patch.object(panel.state, "db", new=AsyncMock(return_value=fake_pool)):
            result = await panel.sync_report(data, make_request({"X-Sync-Key": "sync"}))
        self.assertTrue(result["deleted"])
        self.assertTrue(conn.deleted)

    async def test_expired_user_stays_in_manifest_with_only_expiry_placeholder(self):
        uri = "vless://id@expired.example#Server"
        parsed = panel.parse_server_uri(uri)
        user = {"id": 91, "slug": "expired", "name": "Expired", "enabled": True,
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1), "note": "",
                "title_override": None, "announce_override": None, "status": "active", "drive_file_id": "drive-id"}
        conn = FakeConn(users=[user], server={"id": parsed["id"], "original_name": parsed["name"],
                                             "custom_name": None, "hidden": False, "sort_order": 0})
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(uri.encode(), {}, False))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(panel.DEFAULT_SETTINGS, announce_enabled=False))):
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))
        manifest_user = result["users"][0]
        self.assertEqual(manifest_user["status"], "active")
        self.assertEqual(manifest_user["content"].count("vless://"), 1)
        self.assertIn(panel.quote("Время подписки закончилось", safe=""), manifest_user["content"])
        self.assertFalse(conn.deleted)

    async def test_successful_report_does_not_delete_expired_active_user(self):
        conn = FakeConn(status="active")
        fake_pool = FakePool(conn)
        data = panel.SyncReport(user_id=92, drive_file_id="drive-id", ok=True)
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), patch.object(panel.state, "db", new=AsyncMock(return_value=fake_pool)):
            result = await panel.sync_report(data, make_request({"X-Sync-Key": "sync"}))
        self.assertNotIn("deleted", result)
        self.assertFalse(conn.deleted)

    async def test_manifest_fills_empty_cache_itself_before_returning_users(self):
        old_client = panel.state.upstream_client
        old_cache = (panel.state.upstream_body, panel.state.upstream_headers, panel.state.upstream_fetched_at)
        user = {"id": 10, "slug": "cold", "name": "Cold", "note": "", "enabled": True,
                "expires_at": None, "title_override": None, "announce_override": None, "status": "active", "drive_file_id": None}
        parsed = panel.parse_server_uri("vless://id@cold.example#Cold")
        conn = FakeConn(users=[user], server={"id": parsed["id"], "original_name": parsed["name"],
                                             "custom_name": None, "hidden": False, "sort_order": 0})
        pool = FakePool(conn)
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, content=b"vless://id@cold.example#Cold", headers={"content-type": "text/plain"})

        panel.state.upstream_body = None
        panel.state.upstream_headers = {}
        panel.state.upstream_fetched_at = 0
        panel.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.dict(os.environ, {"SUBSCRIPTION_URL": "https://subscription.invalid/path", "SYNC_KEY": "sync"}, clear=False), \
                 patch.object(panel.state, "db", new=AsyncMock(return_value=pool)), \
                 patch.object(panel, "_settings", new=AsyncMock(return_value=dict(panel.DEFAULT_SETTINGS, announce_enabled=False))):
                result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))
            self.assertIn("vless://", result["users"][0]["content"])
            self.assertEqual(seen["user-agent"], "Happ")
            self.assertIsNotNone(panel.state.upstream_body)
        finally:
            await panel.state.upstream_client.aclose()
            panel.state.upstream_client = old_client
            panel.state.upstream_body, panel.state.upstream_headers, panel.state.upstream_fetched_at = old_cache


if __name__ == "__main__":
    unittest.main()
