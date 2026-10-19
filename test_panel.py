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
        return "UPDATE 1"

    async def fetch(self, query, *args):
        if "SELECT * FROM users" in query:
            return self.users
        if "SELECT id,original_name,custom_name,hidden,sort_order FROM servers" in query:
            return self.servers
        if "SELECT server_id,hidden,custom_name FROM user_servers" in query:
            return self.user_servers
        return []

    async def fetchrow(self, query, *args):
        if "SELECT id,status FROM users" in query:
            return {"id": args[0], "status": self.status}
        return None

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

    def test_subscription_base64_decodes_only_when_it_contains_uris(self):
        raw = "vless://user@host\n"
        self.assertEqual(panel._decode_subscription(base64.b64encode(raw.encode())), raw.strip())
        self.assertIsNotNone(panel._invalid_payload(base64.b64encode(b"DEVICE LIMIT FULL"), ""))

    def test_duplicate_server_entries_with_different_names_collapse(self):
        entries = "vless://id@host:443#First\nvless://id@host:443#Second\n"
        self.assertEqual(panel._extract_uris(entries), ["vless://id@host:443#First"])


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


class ManifestTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertIn("download=8", result["users"][0]["content"])

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
        self.assertIn(f"expire={int(expires.timestamp())}", content)
        self.assertEqual(content.count("vless://"), 1)
        self.assertIn("127.0.0.1:1", content)
        encoded_announce = content.split("#announce: base64:", 1)[1].splitlines()[0]
        self.assertIn("Подписка истекла", base64.b64decode(encoded_announce).decode())

    async def test_pending_delete_is_removed_only_after_success_report(self):
        conn = FakeConn(status="pending_delete")
        fake_pool = FakePool(conn)
        data = panel.SyncReport(user_id=9, drive_file_id="drive-id", ok=True)
        with patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), patch.object(panel.state, "db", new=AsyncMock(return_value=fake_pool)):
            result = await panel.sync_report(data, make_request({"X-Sync-Key": "sync"}))
        self.assertTrue(result["deleted"])
        self.assertTrue(conn.deleted)

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
