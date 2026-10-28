import base64
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import app
import panel
from starlette.requests import Request


def make_request(headers=None, query_string=b""):
    return Request({"type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
                    "path": "/api/sync/manifest", "raw_path": b"/api/sync/manifest", "query_string": query_string,
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
            rows = self.users
            if "status='pending_delete'" in query:
                rows = [row for row in rows if row.get("status") == "pending_delete"]
            if "id=ANY" in query and args:
                ids = set(args[0])
                rows = [row for row in rows if row.get("id") in ids]
            return rows
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

    def test_subscription_extracts_new_nodes_with_unescaped_spaces_in_names(self):
        raw = "\n".join([
            "# subscription metadata",
            "vless://uuid@de.example:443?security=tls#Germany West 01",
            "trojan://secret@jp.example:443#Japan Tokyo",
            "vless://bad path@invalid.example:443#Malformed connection",
        ])
        encoded = base64.b64encode(raw.encode("utf-8"))

        uris = panel._extract_uris(panel._decode_subscription(encoded))

        self.assertEqual(uris, [
            "vless://uuid@de.example:443?security=tls#Germany West 01",
            "trojan://secret@jp.example:443#Japan Tokyo",
        ])
        self.assertEqual(panel.parse_server_uri(uris[0])["name"], "Germany West 01")

    def test_xray_json_shadowsocks_outbound_converts_from_plain_and_base64_json(self):
        xray_json = json.dumps({
            "remarks": "🇩🇪DE, Frankfurt",
            "outbounds": [{
                "protocol": "shadowsocks",
                "settings": {"servers": [{
                    "address": "203.0.113.46", "port": 869,
                    "method": "chacha20-ietf-poly1305", "password": "fixture-password",
                }]},
            }],
        })
        expected_userinfo = base64.urlsafe_b64encode(
            b"chacha20-ietf-poly1305:fixture-password").decode().rstrip("=")
        expected = (f"ss://{expected_userinfo}@203.0.113.46:869#"
                    f"{panel.quote('🇩🇪DE, Frankfurt', safe='')}")

        for body in (xray_json.encode(), base64.b64encode(xray_json.encode())):
            with self.subTest(encoded=body != xray_json.encode()):
                decoded = panel._decode_subscription(body)
                uris = panel._extract_uris(decoded)
                self.assertEqual(uris, [expected])
                self.assertIsNone(panel._invalid_payload(body, "application/json"))

    def test_sas_xray_profile_list_converts_vless_and_preserves_transport_security(self):
        xhttp_outbound = {
            "tag": "proxy-0", "protocol": "vless",
            "settings": {"vnext": [{"address": "203.0.113.20", "port": 443, "users": [
                {"id": "00000000-0000-4000-8000-000000000020", "encryption": "none", "flow": ""}
            ]}]},
            "streamSettings": {
                "network": "xhttp", "security": "tls",
                "tlsSettings": {"serverName": "cdn.example", "fingerprint": "chrome", "alpn": ["h3"]},
                "xhttpSettings": {"path": "/edge", "host": "cdn.example", "mode": "auto",
                                  "extra": {"scMaxConcurrentPosts": 8}},
                "finalmask": {"XDNS": {"enabled": True}},
            },
        }
        reality_outbound = {
            "tag": "proxy", "protocol": "vless",
            "settings": {"vnext": [{"address": "203.0.113.21", "port": 8443, "users": [
                {"id": "00000000-0000-4000-8000-000000000021", "encryption": "none", "flow": "xtls-rprx-vision"}
            ]}]},
            "streamSettings": {
                "network": "grpc", "security": "reality",
                "realitySettings": {"serverName": "real.example", "fingerprint": "firefox",
                                    "publicKey": "fixture-public-key", "shortId": "1a2b", "spiderX": "/"},
                "grpcSettings": {"serviceName": "service.v1", "multiMode": True},
            },
        }
        profile_json = json.dumps([
            {"remarks": "🇪🇺 Автовыбор", "outbounds": [xhttp_outbound, reality_outbound,
                {"tag": "direct", "protocol": "freedom", "settings": {}}]},
            {"remarks": "SAS Frankfurt", "outbounds": [xhttp_outbound]},
            {"remarks": "SAS Reality", "outbounds": [reality_outbound]},
        ], ensure_ascii=False)

        uris = panel._extract_uris(panel._decode_subscription(profile_json.encode("utf-8")))
        by_name = {panel.parse_server_uri(uri)["name"]: uri for uri in uris}

        self.assertEqual(set(by_name), {"SAS Frankfurt", "SAS Reality"})
        xhttp = parse_qs(urlsplit(by_name["SAS Frankfurt"]).query)
        self.assertEqual(xhttp["type"], ["xhttp"])
        self.assertEqual(xhttp["security"], ["tls"])
        self.assertEqual(xhttp["sni"], ["cdn.example"])
        self.assertEqual(xhttp["fp"], ["chrome"])
        self.assertEqual(xhttp["alpn"], ["h3"])
        self.assertEqual(xhttp["path"], ["/edge"])
        self.assertEqual(xhttp["host"], ["cdn.example"])
        self.assertEqual(json.loads(xhttp["extra"][0]), {"scMaxConcurrentPosts": 8})
        self.assertEqual(json.loads(xhttp["fm"][0]), {"XDNS": {"enabled": True}})

        grpc = parse_qs(urlsplit(by_name["SAS Reality"]).query)
        self.assertEqual(grpc["type"], ["grpc"])
        self.assertEqual(grpc["security"], ["reality"])
        self.assertEqual(grpc["flow"], ["xtls-rprx-vision"])
        self.assertEqual(grpc["pbk"], ["fixture-public-key"])
        self.assertEqual(grpc["sid"], ["1a2b"])
        self.assertEqual(grpc["spx"], ["/"])
        self.assertEqual(grpc["serviceName"], ["service.v1"])
        self.assertEqual(grpc["mode"], ["multi"])

    def test_merger_combines_primary_uri_list_with_sas_json_profiles(self):
        primary_uri = "vless://primary@primary.example:443?encryption=none&type=tcp#Primary"
        sas_profile = [{
            "remarks": "SAS Frankfurt",
            "outbounds": [{
                "protocol": "vless",
                "settings": {"vnext": [{"address": "203.0.113.22", "port": 443, "users": [
                    {"id": "00000000-0000-4000-8000-000000000022", "encryption": "none"}
                ]}]},
                "streamSettings": {"network": "ws", "security": "tls",
                                   "tlsSettings": {"serverName": "ws.example", "fingerprint": "chrome"},
                                   "wsSettings": {"path": "/stream", "headers": {"Host": "cdn.example"}}},
            }],
        }]
        merged = panel.merge_subscription_bodies(
            base64.b64encode(primary_uri.encode()),
            json.dumps(sas_profile).encode(),
            "text/plain",
            "application/json",
        ).decode()

        self.assertEqual(len(panel._extract_uris(merged)), 2)
        self.assertTrue(merged.startswith(primary_uri + "\n"))
        self.assertIn("#SAS%20Frankfurt", merged)
        sas_uri = next(uri for uri in panel._extract_uris(merged) if "SAS%20Frankfurt" in uri)
        sas_params = parse_qs(urlsplit(sas_uri).query)
        self.assertEqual(sas_params["type"], ["ws"])
        self.assertEqual(sas_params["security"], ["tls"])
        self.assertEqual(sas_params["path"], ["/stream"])
        self.assertEqual(sas_params["host"], ["cdn.example"])

    def test_ss_uri_without_fragment_is_a_valid_server_line(self):
        userinfo = base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:fixture-password").decode().rstrip("=")
        uri = f"ss://{userinfo}@203.0.113.46:869"

        self.assertEqual(panel._extract_uris(uri), [uri])
        self.assertEqual(panel.parse_server_uri(uri)["scheme"], "ss")


class ServerReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_base64_nodes_with_spaced_labels_reach_generated_manifest(self):
        provider_text = "\n".join([
            "vless://uuid@de.example:443?security=tls#Germany West 01",
            "trojan://secret@jp.example:443#Japan Tokyo",
        ])
        body = base64.b64encode(provider_text.encode("utf-8"))
        conn = FakeConn()
        user = {"id": 42, "slug": "new-nodes", "name": "New nodes", "enabled": True,
                "expires_at": None, "announce_override": "", "title_override": None,
                "status": "active", "drive_file_id": None}

        servers = await panel._sync_servers(conn, body)
        content = await panel.build_manifest_content(
            conn, user, body, {}, dict(panel.DEFAULT_SETTINGS, announce_enabled=False), servers=servers)

        self.assertEqual(len(servers), 2)
        self.assertEqual(len({server["id"] for server in servers}), 2)
        self.assertIn("vless://uuid@de.example:443?security=tls#Germany%20West%2001", content)
        self.assertIn("trojan://secret@jp.example:443#Japan%20Tokyo", content)

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

    async def test_primary_and_sas_feeds_merge_in_order_and_keep_primary_headers(self):
        main_uri = "vless://main-id@main.example:443#Main"
        sas_uri = "trojan://sas-secret@sas.example:443#SAS%20Node"

        def handler(request):
            if str(request.url) == "https://primary.invalid/path":
                return httpx.Response(200, content=base64.b64encode(main_uri.encode()), headers={
                    "content-type": "text/plain", "subscription-userinfo": "upload=1; download=2; total=9; expire=0"
                })
            self.assertEqual(str(request.url), "https://sas.invalid/")
            return httpx.Response(200, content=base64.b64encode(f"{sas_uri}\n{main_uri}".encode()), headers={
                "content-type": "text/plain"
            })

        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {
            "SUBSCRIPTION_URL": "https://primary.invalid/path",
            "SAS_SUBSCRIPTION_URL": "https://sas.invalid/",
        }, clear=True):
            body, headers, stale = await self.state.fetch_upstream()

        self.assertEqual(body.decode().splitlines(), [main_uri, sas_uri])
        self.assertEqual(headers["subscription-userinfo"], "upload=1; download=2; total=9; expire=0")
        self.assertFalse(stale)

    async def test_manifest_style_forced_sas_refresh_picks_up_changed_servers(self):
        sas_body = [b"vless://sas-one@sas.example:443#SAS-One", b"vless://sas-two@sas.example:443#SAS-Two"]
        sas_calls = 0

        def handler(request):
            nonlocal sas_calls
            if str(request.url) == "https://primary.invalid/path":
                return httpx.Response(200, content=b"vless://main@main.example:443#Main", headers={"content-type": "text/plain"})
            sas_calls += 1
            return httpx.Response(200, content=sas_body[min(sas_calls - 1, 1)], headers={"content-type": "text/plain"})

        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {
            "SUBSCRIPTION_URL": "https://primary.invalid/path",
            "SAS_SUBSCRIPTION_URL": "https://sas.invalid/",
        }, clear=True):
            first, _, _ = await self.state.fetch_upstream(force_sas=True)
            second, _, _ = await self.state.fetch_upstream(force_sas=True)

        self.assertIn(b"sas-one", first)
        self.assertIn(b"sas-two", second)
        self.assertEqual(sas_calls, 2)

    async def test_sas_failure_uses_last_good_copy_and_marks_combined_data_stale(self):
        calls = 0

        def handler(request):
            nonlocal calls
            if str(request.url) == "https://primary.invalid/path":
                return httpx.Response(200, content=b"vless://main@main.example:443#Main", headers={"content-type": "text/plain"})
            calls += 1
            if calls == 1:
                return httpx.Response(200, content=b"vless://sas@sas.example:443#SAS", headers={"content-type": "text/plain"})
            return httpx.Response(503, text="temporarily unavailable")

        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {
            "SUBSCRIPTION_URL": "https://primary.invalid/path",
            "SAS_SUBSCRIPTION_URL": "https://sas.invalid/",
        }, clear=True):
            await self.state.fetch_upstream(force_sas=True)
            body, _, stale = await self.state.fetch_upstream(force_sas=True)

        self.assertIn(b"vless://sas@sas.example:443#SAS", body)
        self.assertTrue(stale)
        self.assertIn("SAS", self.state.upstream_warning)

    async def test_cold_sas_failure_blocks_incomplete_combined_profile(self):
        def handler(request):
            if str(request.url) == "https://sas.invalid/":
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, content=b"vless://main@main.example:443#Main", headers={"content-type": "text/plain"})

        self.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {
            "SUBSCRIPTION_URL": "https://primary.invalid/path",
            "SAS_SUBSCRIPTION_URL": "https://sas.invalid/",
        }, clear=True):
            with self.assertRaises(HTTPException) as caught:
                await self.state.fetch_upstream(force_sas=True)

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIsNone(self.state.combined_body)


class AnnouncementWeatherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_cache = panel.state.announce_cache
        self.previous_quote = panel.state.announce_quote
        panel.state.announce_cache = None
        panel.state.announce_quote = None

    async def asyncTearDown(self):
        panel.state.announce_cache = self.previous_cache
        panel.state.announce_quote = self.previous_quote

    async def test_weather_is_refetched_and_always_included_when_announcement_is_cached(self):
        weather = AsyncMock(side_effect=[
            "☀️ Ашхабад сейчас: 24°C",
            "🌧 Ашхабад сейчас: 19°C",
        ])
        settings = dict(panel.DEFAULT_SETTINGS, announce_refresh_minutes=60)
        with patch.object(panel, "_current_ashgabat_weather", new=weather), \
             patch.object(panel.secrets, "choice", side_effect=lambda choices: choices[0]):
            first = await panel._announce(settings)
            second = await panel._announce(settings)

        self.assertIn("☀️ Ашхабад сейчас: 24°C", first)
        self.assertIn("🌧 Ашхабад сейчас: 19°C", second)
        self.assertEqual(first.count("Ашхабад сейчас:"), 1)
        self.assertEqual(second.count("Ашхабад сейчас:"), 1)
        self.assertEqual(weather.await_count, 2)
        self.assertNotIn("__CURRENT_ASHGABAT_WEATHER__", second)
        # Quote/footer template remains cached within its configured refresh window.
        self.assertEqual(first.splitlines()[3], second.splitlines()[3])

    async def test_weather_formatter_handles_current_and_legacy_open_meteo_fields(self):
        ashgabat_now = datetime.now(timezone(timedelta(hours=5)))
        valid_time = ashgabat_now.strftime("%Y-%m-%dT%H:%M")
        modern = panel._format_ashgabat_weather({
            "utc_offset_seconds": 18000,
            "current": {"temperature_2m": 24.5, "weather_code": 2, "time": valid_time},
        })
        legacy = panel._format_ashgabat_weather({
            "utc_offset_seconds": 18000,
            "current_weather": {"temperature": 7, "weathercode": 71, "time": valid_time},
        })
        self.assertEqual(modern, f"⛅ Ашхабад сейчас: переменная облачность, 24.5°C · данные на {valid_time[-5:]}")
        self.assertEqual(legacy, f"❄️ Ашхабад сейчас: небольшой снегопад, 7°C · данные на {valid_time[-5:]}")

    async def test_stale_weather_response_is_rejected(self):
        stale_time = (datetime.now(timezone(timedelta(hours=5))) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        with self.assertRaises(ValueError):
            panel._format_ashgabat_weather({
                "utc_offset_seconds": 18000,
                "current": {"temperature_2m": 24, "weather_code": 0, "time": stale_time},
            })

    async def test_temperature_only_response_is_valid_without_guessing_weather_condition(self):
        now = datetime.now(timezone(timedelta(hours=5)))
        result = panel._format_ashgabat_weather({
            "utc_offset_seconds": 18000,
            "timezone": "Asia/Ashgabat",
            "current": {
                "time": now.strftime("%Y-%m-%dT%H:%M"),
                "interval": 900,
                "temperature_2m": 16.2,
            },
        })
        self.assertEqual(result, f"🌡 Ашхабад сейчас: 16.2°C · данные на {now:%H:%M}")

    async def test_weather_fetch_uses_current_ashgabat_fields_without_relay_client(self):
        seen = {}
        client_options = {}
        real_async_client = httpx.AsyncClient

        def handler(request):
            seen["url"] = request.url
            seen["user_agent"] = request.headers.get("user-agent")
            seen["cache_control"] = request.headers.get("cache-control")
            return httpx.Response(200, json={
                "utc_offset_seconds": 18000,
                "current": {
                    "temperature_2m": 26,
                    "time": datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%dT%H:%M"),
                },
            })

        def make_weather_client(**kwargs):
            client_options.update(kwargs)
            return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

        relay_client = panel.state.upstream_client
        with patch.object(panel.httpx, "AsyncClient", new=make_weather_client):
            result = await panel._current_ashgabat_weather()

        self.assertTrue(result.startswith("🌡 Ашхабад сейчас: 26°C · данные на "))
        self.assertEqual(seen["url"].host, "api.open-meteo.com")
        self.assertEqual(seen["url"].params.get("latitude"), "37.9601")
        self.assertEqual(seen["url"].params.get("longitude"), "58.3261")
        self.assertEqual(seen["url"].params.get("current"), "temperature_2m")
        self.assertEqual(seen["url"].params.get("timezone"), "Asia/Ashgabat")
        self.assertEqual(seen["user_agent"], "SecureX-Weather/1.0")
        self.assertEqual(seen["cache_control"], "no-cache")
        self.assertEqual(client_options["timeout"], 5.0)
        self.assertIs(panel.state.upstream_client, relay_client)

    async def test_weather_http_error_logs_status_and_safe_provider_reason(self):
        real_async_client = httpx.AsyncClient

        def handler(request):
            return httpx.Response(
                429,
                json={"error": True, "reason": "API request limit exceeded"},
            )

        def make_weather_client(**kwargs):
            return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(panel.httpx, "AsyncClient", new=make_weather_client), \
             self.assertLogs("vpn_panel", level="WARNING") as captured:
            result = await panel._current_ashgabat_weather()

        self.assertEqual(result, panel.ASHGABAT_WEATHER_UNAVAILABLE)
        self.assertTrue(any("Open-Meteo HTTP 429" in line for line in captured.output))
        self.assertTrue(any("API request limit exceeded" in line for line in captured.output))
        self.assertFalse(any("api.open-meteo.com" in line for line in captured.output))

    async def test_announcement_rotates_once_per_configured_wall_clock_slot(self):
        weather = AsyncMock(side_effect=[
            "☀️ Ашхабад сейчас: ясно, 24°C",
            "☀️ Ашхабад сейчас: ясно, 24°C",
            "🌧 Ашхабад сейчас: умеренный дождь, 19°C",
        ])
        clock = [300.1, 301.0, 600.1]
        settings = dict(panel.DEFAULT_SETTINGS, announce_refresh_minutes=5,
                        quotes=["Цитата один", "Цитата два"],
                        footers=["Обновлено {t}"], days=["День"] * 7)
        with patch.object(panel, "_current_ashgabat_weather", new=weather), \
             patch.object(panel.time, "time", side_effect=clock), \
             patch.object(panel.secrets, "choice", side_effect=lambda choices: choices[0]):
            first = await panel._announce(settings)
            same_slot = await panel._announce(settings)
            next_slot = await panel._announce(settings)

        self.assertEqual(first.splitlines()[3], same_slot.splitlines()[3])
        self.assertNotEqual(first.splitlines()[3], next_slot.splitlines()[3])
        self.assertIn("🌧 Ашхабад сейчас: умеренный дождь, 19°C", next_slot)
        self.assertEqual(weather.await_count, 3)

    async def test_unavailable_weather_is_explicitly_shown_instead_of_stale_or_fake_data(self):
        with patch.object(panel, "_current_ashgabat_weather", new=AsyncMock(
                return_value=panel.ASHGABAT_WEATHER_UNAVAILABLE)):
            text = await panel._announce(dict(panel.DEFAULT_SETTINGS))
        self.assertIn(panel.ASHGABAT_WEATHER_UNAVAILABLE, text)


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


class DriveSyncPushTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_apps_script_response_is_reported_as_a_delivery_failure(self):
        class FakeResponse:
            status_code = 200
            def json(self): return {"ok": False, "processed": 1, "failed": 1}

        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, *args, **kwargs): return FakeResponse()

        state = panel.PanelState()
        with patch.dict(os.environ, {"DRIVE_SYNC_URL": "https://script.example/exec", "SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.httpx, "AsyncClient", FakeClient):
            result = await state.push_drive_sync([7])

        self.assertFalse(result["ok"])
        self.assertFalse(result["pending"])
        self.assertEqual(result["failed"], 1)
        self.assertIn("ошибок: 1", result["message"])
        self.assertIsNotNone(state.drive_push_last)


class ManifestTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_forces_sas_refresh_and_includes_servers_in_every_profile(self):
        body = b"vless://main@main.example:443#Main\nvless://sas@sas.example:443#SAS"
        users = [
            {"id": 40, "slug": "legacy", "name": "Main profile", "note": "", "enabled": True,
             "expires_at": None, "title_override": None, "announce_override": "", "status": "active",
             "drive_file_id": panel.LEGACY_DRIVE_FILE_ID},
            {"id": 41, "slug": "subscriber", "name": "Subscriber", "note": "", "enabled": True,
             "expires_at": None, "title_override": None, "announce_override": "", "status": "active",
             "drive_file_id": None},
        ]
        conn = FakeConn(users=users)
        fetch = AsyncMock(return_value=(body, {"subscription-userinfo": "upload=1; download=2; total=9; expire=0"}, False))
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel.state, "fetch_upstream", new=fetch), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                 panel.DEFAULT_SETTINGS, announce_enabled=False))):
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))

        fetch.assert_awaited_once_with(force_sas=True)
        self.assertEqual(len(result["users"]), 2)
        for user in result["users"]:
            self.assertIn("vless://main@main.example:443#Main", user["content"])
            self.assertIn("vless://sas@sas.example:443#SAS", user["content"])

    async def test_sync_manifest_delivers_all_new_nodes_from_base64_with_spaced_names(self):
        provider_text = "\n".join([
            "vless://uuid@de.example:443?security=tls#Germany West 01",
            "trojan://secret@jp.example:443#Japan Tokyo",
        ])
        body = base64.b64encode(provider_text.encode("utf-8"))
        user = {"id": 43, "slug": "manifest-nodes", "name": "Manifest nodes", "note": "",
                "enabled": True, "expires_at": None, "title_override": None,
                "announce_override": "", "status": "active", "drive_file_id": None}
        conn = FakeConn(users=[user])

        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(body, {}, False))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                 panel.DEFAULT_SETTINGS, announce_enabled=False))):
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))

        content = result["users"][0]["content"]
        self.assertIn("vless://uuid@de.example:443?security=tls#Germany%20West%2001", content)
        self.assertIn("trojan://secret@jp.example:443#Japan%20Tokyo", content)

    async def test_sync_manifest_converts_xray_shadowsocks_json_to_happ_server_uri(self):
        xray_json = json.dumps({
            "remarks": "🇩🇪DE, Frankfurt",
            "outbounds": [{
                "protocol": "shadowsocks",
                "settings": {"servers": [{
                    "address": "203.0.113.46", "port": 869,
                    "method": "chacha20-ietf-poly1305", "password": "fixture-password",
                }]},
            }],
        })
        user = {"id": 44, "slug": "xray-json", "name": "Xray JSON", "note": "",
                "enabled": True, "expires_at": None, "title_override": None,
                "announce_override": "", "status": "active", "drive_file_id": None}
        conn = FakeConn(users=[user])
        expected_userinfo = base64.urlsafe_b64encode(
            b"chacha20-ietf-poly1305:fixture-password").decode().rstrip("=")
        expected = (f"ss://{expected_userinfo}@203.0.113.46:869#"
                    f"{panel.quote('🇩🇪DE, Frankfurt', safe='')}")

        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(xray_json.encode(), {}, False))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                 panel.DEFAULT_SETTINGS, announce_enabled=False))):
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))

        self.assertIn(expected, result["users"][0]["content"])

    async def test_pending_delete_manifest_does_not_require_provider(self):
        pending = {"id": 53, "slug": "revoked", "name": "Revoked", "enabled": False,
                   "status": "pending_delete", "drive_file_id": "drive-id"}
        pool = FakePool(FakeConn(users=[pending]))
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=pool)), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(side_effect=HTTPException(503, "upstream offline"))) as fetch:
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}, b"user_ids=53"))
        self.assertEqual(len(result["users"]), 1)
        self.assertEqual(result["users"][0]["status"], "pending_delete")
        self.assertEqual(result["users"][0]["content"], "")
        self.assertNotIn("partial", result)
        fetch.assert_not_awaited()

    async def test_provider_failure_returns_only_pending_deletions_for_retry(self):
        active = {"id": 54, "slug": "active", "name": "Active", "enabled": True,
                  "status": "active", "drive_file_id": "active-file"}
        pending = {"id": 55, "slug": "revoked", "name": "Revoked", "enabled": False,
                   "status": "pending_delete", "drive_file_id": "revoked-file"}
        pool = FakePool(FakeConn(users=[active, pending]))
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=pool)), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(side_effect=HTTPException(503, "upstream offline"))):
            result = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))
        self.assertTrue(result["partial"])
        self.assertEqual([user["id"] for user in result["users"]], [55])
        self.assertEqual(result["users"][0]["content"], "")

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

    async def test_each_sync_manifest_contains_fresh_weather_and_rotated_announcement(self):
        server_uri = "vless://id@weather.example#Weather"
        parsed = panel.parse_server_uri(server_uri)
        user = {"id": 30, "slug": "weather", "name": "Weather", "note": "", "enabled": True,
                "expires_at": None, "title_override": None, "announce_override": None,
                "status": "active", "drive_file_id": "drive-id"}
        conn = FakeConn(users=[user], server={"id": parsed["id"], "original_name": parsed["name"],
                                             "custom_name": None, "hidden": False, "sort_order": 0})
        pool = FakePool(conn)
        old_cache, old_quote = panel.state.announce_cache, panel.state.announce_quote
        panel.state.announce_cache = None
        panel.state.announce_quote = None
        weather = AsyncMock(side_effect=[
            "☀️ Ашхабад сейчас: ясно, 24°C · данные на 10:00",
            "🌧 Ашхабад сейчас: умеренный дождь, 19°C · данные на 10:00",
        ])
        try:
            with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
                 patch.object(panel.state, "db", new=AsyncMock(return_value=pool)), \
                 patch.object(panel.state, "fetch_upstream", new=AsyncMock(
                     return_value=(server_uri.encode(), {}, False))), \
                 patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                     panel.DEFAULT_SETTINGS, announce_refresh_minutes=5))), \
                 patch.object(panel, "_current_ashgabat_weather", new=weather), \
                 patch.object(panel.time, "time", side_effect=[300.1, 600.1]), \
                 patch.object(panel.secrets, "choice", side_effect=lambda choices: choices[0]):
                first = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))
                second = await panel.sync_manifest(make_request({"X-Sync-Key": "sync"}))
        finally:
            panel.state.announce_cache, panel.state.announce_quote = old_cache, old_quote

        def decoded_announce(manifest):
            encoded = manifest["users"][0]["content"].split("#announce: base64:", 1)[1].splitlines()[0]
            return base64.b64decode(encoded).decode()

        first_announcement = decoded_announce(first)
        second_announcement = decoded_announce(second)
        self.assertIn("☀️ Ашхабад сейчас: ясно, 24°C", first_announcement)
        self.assertIn("🌧 Ашхабад сейчас: умеренный дождь, 19°C", second_announcement)
        self.assertNotEqual(first_announcement.splitlines()[3], second_announcement.splitlines()[3])
        self.assertEqual(weather.await_count, 2)

    async def test_sync_manifest_uses_apps_script_weather_header_without_render_request(self):
        server_uri = "vless://id@weather-header.example#Weather"
        parsed = panel.parse_server_uri(server_uri)
        user = {"id": 31, "slug": "weather-header", "name": "Weather", "note": "", "enabled": True,
                "expires_at": None, "title_override": None, "announce_override": None,
                "status": "active", "drive_file_id": "drive-id"}
        conn = FakeConn(users=[user], server={"id": parsed["id"], "original_name": parsed["name"],
                                             "custom_name": None, "hidden": False, "sort_order": 0})
        payload = {
            "utc_offset_seconds": 18000,
            "current": {"time": datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%dT%H:%M"),
                        "interval": 900, "temperature_2m": 16.2},
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        weather_fetch = AsyncMock(side_effect=AssertionError("Render should not fetch Open-Meteo"))
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(
                 return_value=(server_uri.encode(), {}, False))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                 panel.DEFAULT_SETTINGS, announce_enabled=True))), \
             patch.object(panel, "_current_ashgabat_weather", new=weather_fetch):
            result = await panel.sync_manifest(make_request({
                "X-Sync-Key": "sync",
                "X-Weather-Data": encoded,
            }))

        encoded_announce = result["users"][0]["content"].split("#announce: base64:", 1)[1].splitlines()[0]
        announcement = base64.b64decode(encoded_announce).decode()
        self.assertIn("🌡 Ашхабад сейчас: 16.2°C", announcement)
        weather_fetch.assert_not_awaited()

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
        self.assertIn("/static/admin.js?v=20261027-1", desktop.text)
        self.assertIn("/static/admin.js?v=20261027-1", mobile.text)
        self.assertIn("/static/admin-actions.js?v=20261025-1", desktop.text)
        self.assertIn("/static/admin-actions.js?v=20261025-1", mobile.text)
        self.assertIn("/static/admin.css?v=20261025-1", mobile.text)
        self.assertIn('class="mobile-nav-glider"', mobile.text)
        self.assertIn('data-active-index="0"', mobile.text)
        self.assertEqual(javascript.status_code, 200)
        self.assertIn('name="expires_at" type="datetime-local"', javascript.text)
        self.assertIn("Подписка отключена, ссылка заменена заглушкой", javascript.text)
        self.assertIn("dataset.activeIndex = String(index)", javascript.text)
        self.assertIn('class="button button-secondary user-qr-action" data-action="qr" data-user-id=', javascript.text)
        self.assertIn('data-action="copy-link" data-link=', javascript.text)
        self.assertNotIn('data-action="qr" data-link=', javascript.text)
        self.assertIn("showDriveQr(shareAction.userId)", javascript.text)
        self.assertIn("driveQrLoadingContent()", javascript.text)
        self.assertIn("credentials: 'same-origin'", javascript.text)
        self.assertIn("День/приветствие, цитата и подпись меняются с этим интервалом", javascript.text)
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
        self.assertIn("mobile-nav-glider", stylesheet.text)
        self.assertIn("cubic-bezier(.2, 1.22, .32, 1)", stylesheet.text)
        self.assertNotIn(".mobile-shell { display: block !important; }", stylesheet.text)

    async def test_pending_delete_is_removed_only_after_success_report(self):
        conn = FakeConn(status="pending_delete")
        fake_pool = FakePool(conn)
        data = panel.SyncReport(user_id=9, drive_file_id="drive-id", ok=True)
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), patch.object(panel.state, "db", new=AsyncMock(return_value=fake_pool)):
            result = await panel.sync_report(data, make_request({"X-Sync-Key": "sync"}))
        self.assertTrue(result["deleted"])
        self.assertTrue(conn.deleted)

    async def test_failed_revocation_report_keeps_user_pending_delete(self):
        conn = FakeConn(status="pending_delete")
        fake_pool = FakePool(conn)
        data = panel.SyncReport(user_id=10, drive_file_id="drive-id", ok=False, error="Drive update failed")
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), patch.object(panel.state, "db", new=AsyncMock(return_value=fake_pool)):
            result = await panel.sync_report(data, make_request({"X-Sync-Key": "sync"}))
        self.assertNotIn("deleted", result)
        self.assertFalse(conn.deleted)

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
