import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import panel
from test_panel import FakeConn, FakePool, make_request


PROVIDER = "Ab12Cd34"
AUTH_KEY = "0123456789ABCDEFghijklmnopQRSTUV"
INSTALL_CODE = "Abcdef123456"
DRIVE_API_KEY = "drive-test-key"
ENV = {"HAPP_PROVIDER_CODE": PROVIDER, "HAPP_AUTH_KEY": AUTH_KEY,
       "DRIVE_API_KEY": DRIVE_API_KEY}


def make_user(**overrides):
    result = {
        "id": 5, "slug": "ann", "status": "active", "enabled": True,
        "expires_at": None, "drive_file_id": "file-1", "device_limit": 3,
        "device_limit_status": "waiting", "device_limit_error": None,
        "happ_install_code": None, "limited_link": None,
    }
    result.update(overrides)
    return result


class DeviceLimitConn:
    def __init__(self, user):
        self.user = user
        self.events = []

    async def fetchrow(self, query, *args):
        self.events.append(("fetchrow", query, args))
        if query.startswith("SELECT id,slug,status,enabled,expires_at,drive_file_id,device_limit"):
            return dict(self.user) if self.user else None
        if query.startswith("UPDATE users SET happ_install_code=$3,device_limit_status='waiting'"):
            if not self.user or self.user["device_limit"] != args[1]:
                return None
            self.user.update(happ_install_code=args[2], device_limit_status="waiting", device_limit_error=None)
            return {"id": self.user["id"]}
        if query.startswith("UPDATE users SET happ_install_code=$3,limited_link=$4"):
            if not self.user or self.user["device_limit"] != args[1]:
                return None
            self.user.update(happ_install_code=args[2], limited_link=args[3],
                             device_limit_status="ready", device_limit_error=None)
            return {"id": self.user["id"]}
        if query.startswith("UPDATE users SET device_limit=NULL"):
            if not self.user:
                return None
            self.user.update(device_limit=None, happ_install_code=None, limited_link=None,
                             device_limit_status="none", device_limit_error=None)
            return {"id": self.user["id"]}
        return None

    async def execute(self, query, *args):
        self.events.append(("execute", query, args))
        if not self.user:
            return "UPDATE 0"
        if "SET device_limit_status='error'" in query:
            self.user.update(device_limit_status="error", device_limit_error=args[2])
        elif "SET device_limit_status='waiting'" in query:
            self.user.update(device_limit_status="waiting", device_limit_error=args[0]
                             if "device_limit_error='Ожидается" in query else None)
        return "UPDATE 1"


class DeviceLimitApiTests(unittest.IsolatedAsyncioTestCase):
    def mock_client(self, conn, requests, *, crypto_status=200):
        def handler(request):
            requests.append(request)
            if request.url.host == "api.happ-proxy.com":
                return httpx.Response(200, json={"rc": 1, "msg": "Ok", "install_code": INSTALL_CODE})
            if request.url.host == "crypto.happ.su":
                # The install code must reach durable storage before we issue
                # the request that turns the Drive URL into a limited link.
                self.assertEqual(conn.user["happ_install_code"], INSTALL_CODE)
                if crypto_status != 200:
                    return httpx.Response(crypto_status, text="temporary failure")
                return httpx.Response(200, json={"data": {"url": "happ://crypt5/EncryptedValue"}})
            return httpx.Response(404)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    async def create_link(self, conn, *, env=None, requests=None, crypto_status=200):
        requests = requests if requests is not None else []
        with patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel, "_happ_client", side_effect=lambda: self.mock_client(
                 conn, requests, crypto_status=crypto_status)), \
             patch.dict(os.environ, env or ENV, clear=False):
            result = await panel._create_limited_link(5)
        return result, requests

    async def test_generates_counter_persists_it_then_returns_encrypted_link(self):
        conn = DeviceLimitConn(make_user())
        result, requests = await self.create_link(conn)
        self.assertEqual(result["device_limit_status"], "ready")
        self.assertEqual(result["device_limit"], 3)
        self.assertEqual(conn.user["limited_link"], "happ://crypt5/EncryptedValue")
        self.assertEqual([request.method for request in requests], ["GET", "POST"])
        self.assertEqual(requests[0].url.params["install_limit"], "3")
        self.assertEqual(requests[0].url.params["provider_code"], PROVIDER)
        self.assertEqual(requests[0].url.params["auth_key"], AUTH_KEY)
        encrypt_payload = json.loads(requests[1].content)
        self.assertEqual(encrypt_payload["url"],
                         f"https://www.googleapis.com/drive/v3/files/file-1?key={DRIVE_API_KEY}&alt=media#?installid={INSTALL_CODE}")

    async def test_one_device_limit_is_sent_to_happ_and_drive_url_is_encrypted(self):
        conn = DeviceLimitConn(make_user(device_limit=1))
        result, requests = await self.create_link(conn)
        self.assertEqual(requests[0].url.params["install_limit"], "1")
        self.assertEqual(result["limited_link"], "happ://crypt5/EncryptedValue")
        self.assertIn("installid=" + INSTALL_CODE, json.loads(requests[1].content)["url"])

    async def test_rebuild_reuses_counter_and_does_not_call_add_install(self):
        conn = DeviceLimitConn(make_user(happ_install_code=INSTALL_CODE,
                                         limited_link="happ://crypt5/Old"))
        requests = []
        with patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel, "_happ_client", side_effect=lambda: self.mock_client(conn, requests)), \
             patch.dict(os.environ, ENV, clear=False):
            result = await panel._create_limited_link(5, rebuild=True)
        self.assertEqual(result["limited_link"], "happ://crypt5/EncryptedValue")
        self.assertEqual([request.method for request in requests], ["POST"])
        self.assertEqual(conn.user["happ_install_code"], INSTALL_CODE)

    async def test_crypto_failure_keeps_saved_counter_and_marks_safe_error(self):
        conn = DeviceLimitConn(make_user())
        with self.assertRaises(HTTPException) as caught:
            await self.create_link(conn, crypto_status=503)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(conn.user["happ_install_code"], INSTALL_CODE)
        self.assertEqual(conn.user["device_limit_status"], "error")
        self.assertNotIn(AUTH_KEY, str(caught.exception.detail))
        self.assertNotIn("drive-test-key", str(caught.exception.detail))

    async def test_missing_credentials_marks_error_without_exposing_secrets(self):
        conn = DeviceLimitConn(make_user())
        with self.assertRaises(HTTPException) as caught:
            await self.create_link(conn, env={"HAPP_PROVIDER_CODE": "", "HAPP_AUTH_KEY": ""})
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(conn.user["device_limit_status"], "error")
        self.assertNotIn(AUTH_KEY, str(caught.exception.detail))

    async def test_legacy_missing_drive_disabled_pending_and_expired_users_are_refused(self):
        invalid_users = (
            make_user(slug="legacy"),
            make_user(drive_file_id=panel.LEGACY_DRIVE_FILE_ID),
            make_user(drive_file_id=None),
            make_user(enabled=False),
            make_user(status="pending_delete"),
            make_user(expires_at=panel.datetime.now(panel.timezone.utc) - panel.timedelta(seconds=1)),
        )
        for user in invalid_users:
            with self.subTest(user=user):
                conn = DeviceLimitConn(user)
                with patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
                     patch.object(panel, "_happ_client", side_effect=AssertionError("Happ must not be called")), \
                     patch.dict(os.environ, ENV, clear=False):
                    with self.assertRaises(HTTPException):
                        await panel._create_limited_link(5)

    async def test_editing_limit_resets_code_and_link_and_requires_new_counter(self):
        class EditConn(DeviceLimitConn):
            async def fetchrow(self, query, *args):
                if query.startswith("SELECT slug,drive_file_id,device_limit"):
                    return {"slug": "ann", "drive_file_id": "file-1", "device_limit": 3}
                if query.startswith("UPDATE users SET name=$2"):
                    self.update_query, self.update_args = query, args
                    return {"id": 5, "device_limit": args[12], "device_limit_status": "waiting",
                            "happ_install_code": None, "limited_link": None}
                return await super().fetchrow(query, *args)

        conn = EditConn(make_user(happ_install_code=INSTALL_CODE, limited_link="happ://crypt5/Old"))
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel, "_schedule_limited_link") as schedule, \
             patch.object(panel.state, "push_drive_sync", new=AsyncMock(return_value={"ok": True})):
            result = await panel.users_update(5, panel.UserBody(name="Ann", device_limit=99), make_request())
        self.assertEqual(result["user"]["device_limit"], 99)
        self.assertIn("happ_install_code=CASE WHEN $14 THEN NULL", conn.update_query)
        self.assertIn("limited_link=CASE WHEN $14 THEN NULL", conn.update_query)
        self.assertTrue(conn.update_args[13])
        schedule.assert_called_once_with(5)

    async def test_legacy_limit_edit_is_rejected_without_update(self):
        class LegacyConn(DeviceLimitConn):
            async def fetchrow(self, query, *args):
                if query.startswith("SELECT slug,drive_file_id,device_limit"):
                    return {"slug": "legacy", "drive_file_id": "legacy-file", "device_limit": None}
                self.did_update = True
                return None

        conn = LegacyConn(make_user(slug="legacy", drive_file_id=panel.LEGACY_DRIVE_FILE_ID,
                                    device_limit=None))
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))):
            with self.assertRaises(HTTPException) as caught:
                await panel.users_update(5, panel.UserBody(name="Main", device_limit=3), make_request())
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(getattr(conn, "did_update", False))


class DeviceLimitSettingsAndManifestTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_boundaries_and_schema_default(self):
        for value in (1, 99):
            self.assertEqual(panel.UserBody(name="Valid", device_limit=value).device_limit, value)
        for value in (0, 100, -1, True, 2.0):
            with self.subTest(value=value), self.assertRaises(Exception):
                panel.UserBody(name="Invalid", device_limit=value)
        self.assertIsNone(panel.DEFAULT_SETTINGS["device_limit_for_new_users"])

    async def test_create_uses_default_only_when_request_omits_device_limit(self):
        class CreateConn(FakeConn):
            async def fetchval(self, query, *args):
                return 0

            async def fetchrow(self, query, *args):
                self.insert_args = args
                self.insert_query = query
                return {"id": 21, "device_limit": args[10], "device_limit_status": args[11],
                        "happ_install_code": None, "limited_link": None}

        for supplied, expected in ((False, 99), (True, None)):
            conn = CreateConn()
            with self.subTest(supplied=supplied), \
                 patch.object(panel, "_auth"), \
                 patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
                 patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                     panel.DEFAULT_SETTINGS, device_limit_for_new_users=99))), \
                 patch.object(panel, "_schedule_limited_link") as schedule, \
                 patch.object(panel.state, "push_drive_sync", new=AsyncMock(return_value={"ok": True})):
                data = panel.UserBody(name="New profile", **({"device_limit": None} if supplied else {}))
                result = await panel.users_create(data, make_request())
            self.assertEqual(conn.insert_args[10], expected)
            self.assertEqual(conn.insert_args[11], "none" if expected is None else "waiting")
            self.assertEqual(result["user"]["device_limit"], expected)
            self.assertEqual(schedule.call_count, 0 if expected is None else 1)

    async def test_creation_and_drive_sync_are_not_blocked_by_async_happ_generation(self):
        class CreateConn(FakeConn):
            async def fetchval(self, query, *args):
                return 0

            async def fetchrow(self, query, *args):
                return {"id": 22, "device_limit": args[10], "device_limit_status": args[11],
                        "happ_install_code": None, "limited_link": None}

        drive_sync = AsyncMock(return_value={"ok": True, "pending": False})
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(CreateConn()))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(panel.DEFAULT_SETTINGS))), \
             patch.object(panel, "_schedule_limited_link") as schedule, \
             patch.object(panel.state, "push_drive_sync", new=drive_sync):
            response = await panel.users_create(panel.UserBody(name="Async Happ", device_limit=3), make_request())
        self.assertEqual(response["drive_sync"]["ok"], True)
        drive_sync.assert_awaited_once_with([22])
        schedule.assert_called_once_with(22)

    async def test_default_limit_settings_accept_none_1_and_99_and_reject_out_of_range(self):
        class SettingsConn(FakeConn):
            def __init__(self):
                super().__init__()
                self.saved = []

            async def execute(self, query, *args):
                self.saved.append(args)
                return "INSERT 0 1"

        for value in (None, "", 1, 99):
            with self.subTest(value=value):
                conn = SettingsConn()
                with patch.object(panel, "_auth"), \
                     patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))):
                    result = await panel.settings_put(panel.SettingsBody(values={
                        "device_limit_for_new_users": value}), make_request())
                saved_value = json.loads(conn.saved[0][1])
                self.assertEqual(saved_value, None if value in (None, "") else int(value))
                self.assertEqual(result["settings"]["device_limit_for_new_users"], saved_value)
        for value in (0, 100, True, 1.5, "1.5"):
            with self.subTest(value=value), \
                 patch.object(panel, "_auth"), \
                 patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(SettingsConn()))):
                with self.assertRaises(HTTPException) as caught:
                    await panel.settings_put(panel.SettingsBody(values={
                        "device_limit_for_new_users": value}), make_request())
                self.assertEqual(caught.exception.status_code, 422)

    async def test_successful_drive_report_schedules_link_but_failed_report_does_not(self):
        class ReportConn(FakeConn):
            async def fetchrow(self, query, *args):
                if "SELECT id,status,device_limit FROM users" in query:
                    return {"id": args[0], "status": "active", "device_limit": 4}
                return await super().fetchrow(query, *args)

        for ok in (True, False):
            conn = ReportConn()
            with self.subTest(ok=ok), \
                 patch.dict(os.environ, {"SYNC_KEY": "sync"}, clear=False), \
                 patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
                 patch.object(panel, "_schedule_limited_link") as schedule:
                result = await panel.sync_report(panel.SyncReport(user_id=9, drive_file_id="new-drive-id", ok=ok),
                                                 make_request({"X-Sync-Key": "sync"}))
            self.assertTrue(result["ok"])
            self.assertEqual(schedule.call_count, 1 if ok else 0)

    async def test_manifest_does_not_return_install_code_or_encrypted_link(self):
        uri = "vless://uuid@node.example:443#Node"
        server = panel.parse_server_uri(uri)
        user = {
            "id": 50, "slug": "limited", "name": "Limited", "note": "", "enabled": True,
            "expires_at": None, "title_override": None, "announce_override": None,
            "status": "active", "drive_file_id": "drive-file", "device_limit": 5,
            "happ_install_code": INSTALL_CODE, "limited_link": "happ://crypt5/SecretLink",
        }
        conn = FakeConn(users=[user], server={"id": server["id"], "original_name": "Node",
                                             "custom_name": None, "hidden": False, "sort_order": 0})
        env = {"SYNC_KEY": "manifest-sync", "HAPP_AUTH_KEY": AUTH_KEY, "HAPP_PROVIDER_ID": "Ab12Cd34"}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(uri.encode(), {}, False))), \
             patch.object(panel, "_settings", new=AsyncMock(return_value=dict(
                 panel.DEFAULT_SETTINGS, announce_enabled=False))):
            manifest = await panel.sync_manifest(make_request({"X-Sync-Key": "manifest-sync"}))
        rendered = json.dumps(manifest)
        self.assertNotIn(INSTALL_CODE, rendered)
        self.assertNotIn("happ://crypt5/SecretLink", rendered)
        self.assertNotIn(AUTH_KEY, rendered)
        self.assertIn("#providerid Ab12Cd34", manifest["users"][0]["content"])

    async def test_authenticated_user_list_never_returns_install_code(self):
        conn = FakeConn(users=[{"id": 3, "slug": "limited", "device_limit": 5,
                                "happ_install_code": INSTALL_CODE, "limited_link": "happ://crypt5/Visible"}])
        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(conn))):
            result = await panel.users_list(make_request())
        user = result["users"][0]
        self.assertNotIn("happ_install_code", user)
        self.assertEqual(user["limited_link"], "happ://crypt5/Visible")
        self.assertEqual(result["device_limit_bounds"], {"min": 1, "max": 99})

    async def test_sync_error_redaction_removes_happ_credentials(self):
        with patch.dict(os.environ, {"HAPP_PROVIDER_CODE": PROVIDER, "HAPP_AUTH_KEY": AUTH_KEY}, clear=False):
            safe = panel._safe_sync_error(
                f"bad {PROVIDER} {AUTH_KEY} https://private.example "
                "happ://crypt5/EncryptedLink https://sub.example/#?installid=Abcdef123456")
        self.assertNotIn(PROVIDER, safe)
        self.assertNotIn(AUTH_KEY, safe)
        self.assertNotIn("private.example", safe)
        self.assertNotIn("happ://crypt5", safe)
        self.assertNotIn("installid=Abcdef123456", safe)

    async def test_limited_qr_requires_and_uses_encrypted_link(self):
        class QrConn(FakeConn):
            async def fetchrow(self, query, *args):
                if "SELECT limited_link FROM users" in query:
                    return {"limited_link": "happ://crypt5/QR"}
                return None

        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(QrConn()))), \
             patch.object(panel, "_drive_qr_svg", return_value="<svg>limited</svg>") as qr:
            result = await panel.limited_qr(5, make_request())
        qr.assert_called_once_with("happ://crypt5/QR")
        self.assertEqual(result.body, b"<svg>limited</svg>")

    async def test_dashboard_reports_configured_limit_and_error_counts(self):
        rows = [
            {"id": 1, "enabled": True, "status": "active", "expires_at": None,
             "slug": "ready", "drive_file_id": "file-1", "device_limit": 4, "device_limit_status": "ready"},
            {"id": 2, "enabled": True, "status": "active", "expires_at": None,
             "slug": "failed", "drive_file_id": "file-2", "device_limit": 99, "device_limit_status": "error"},
            {"id": 3, "enabled": True, "status": "active", "expires_at": None,
             "slug": "legacy", "drive_file_id": panel.LEGACY_DRIVE_FILE_ID, "device_limit": None, "device_limit_status": "none"},
        ]

        class DashboardConn(FakeConn):
            async def fetch(self, query, *args):
                if query.startswith("SELECT enabled,status,expires_at,slug,drive_file_id,device_limit"):
                    return rows
                if query.startswith("SELECT id,name,last_sync_error"):
                    return []
                return []

            async def fetchrow(self, query, *args):
                return None

        with patch.object(panel, "_auth"), \
             patch.object(panel.state, "fetch_upstream", new=AsyncMock(return_value=(b"", {}, False))), \
             patch.object(panel.state, "db", new=AsyncMock(return_value=FakePool(DashboardConn()))):
            result = await panel.dashboard(make_request())
        self.assertEqual(result["device_limits"], {"count": 2, "errors": 1})


if __name__ == "__main__":
    unittest.main()
