import asyncio
import base64
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

import app as relay


def make_request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 1234),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


class SubscriptionRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_url = relay.SUBSCRIPTION_URL
        self.previous_http = relay.http
        self.previous_snapshot = relay._snapshot
        self.previous_headers = relay._upstream_headers
        self.previous_task = relay._refresh_task
        self.previous_sas_url = relay.SAS_SUBSCRIPTION_URL
        self.previous_sas_snapshots = relay._sas_snapshots.copy()
        relay.SUBSCRIPTION_URL = "https://subscription.invalid/private"
        relay.SAS_SUBSCRIPTION_URL = ""
        relay._snapshot = None
        relay._upstream_headers = None
        relay._sas_snapshots.clear()
        relay._refresh_task = None

    async def asyncTearDown(self):
        if relay._refresh_task is not None:
            relay._refresh_task.cancel()
            try:
                await relay._refresh_task
            except asyncio.CancelledError:
                pass
        if relay.http is not None and relay.http is not self.previous_http:
            await relay.http.aclose()
        relay.http = self.previous_http
        relay.SUBSCRIPTION_URL = self.previous_url
        relay.SAS_SUBSCRIPTION_URL = self.previous_sas_url
        relay._snapshot = self.previous_snapshot
        relay._upstream_headers = self.previous_headers
        relay._sas_snapshots.clear()
        relay._sas_snapshots.update(self.previous_sas_snapshots)
        relay._refresh_task = self.previous_task

    async def test_first_request_fetches_and_serves_raw_subscription(self):
        received = {}
        body = b"vless://example\nvmess://example\n"

        def handler(request: httpx.Request) -> httpx.Response:
            received.update(request.headers)
            return httpx.Response(
                200,
                content=body,
                headers={
                    "content-type": "text/plain; charset=utf-8",
                    "profile-title": "Test profile",
                    "content-encoding": "identity",
                },
            )

        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = make_request(
            [
                (b"user-agent", b"Happ/test"),
                (b"x-hwid", b"test-device"),
                (b"x-device-info", b"test-os"),
                (b"cookie", b"must-not-forward"),
            ]
        )

        response = await relay.serve_subscription(request)

        self.assertEqual(response.body, body)
        self.assertEqual(response.headers["profile-title"], "Test profile")
        self.assertEqual(received["user-agent"], "Happ/test")
        self.assertEqual(received["x-hwid"], "test-device")
        self.assertEqual(received["x-device-info"], "test-os")
        self.assertNotIn("cookie", received)
        self.assertNotIn("content-encoding", response.headers)

    async def test_missing_url_returns_service_error_without_network(self):
        relay.SUBSCRIPTION_URL = ""
        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail("network call")))

        with self.assertRaises(HTTPException) as caught:
            await relay.current_snapshot(make_request([]))

        self.assertEqual(caught.exception.status_code, 503)

    async def test_relay_refreshes_the_combined_subscription_every_five_minutes(self):
        self.assertEqual(relay.REFRESH_INTERVAL_SECONDS, 5 * 60)

    async def test_profile_diagnostics_never_include_header_values(self):
        summary = relay._profile_summary(
            {
                "user-agent": "Happ/private-version",
                "x-hwid": "private-device-id",
                "x-device-os": "private-device-os",
            }
        )

        self.assertIn("client=happ", summary)
        self.assertIn("hwid=present", summary)
        self.assertNotIn("private", summary)

    async def test_configured_upstream_profile_overrides_browser_headers(self):
        with patch.dict(
            "os.environ",
            {
                "UPSTREAM_USER_AGENT": "Happ/configured",
                "UPSTREAM_X_HWID": "configured-device",
                "UPSTREAM_X_DEVICE_OS": "iOS",
            },
            clear=True,
        ):
            headers = relay._upstream_request_headers(
                make_request(
                    [
                        (b"user-agent", b"Chrome/browser"),
                        (b"x-hwid", b"browser-must-not-win"),
                    ]
                )
            )

        self.assertEqual(headers["user-agent"], "Happ/configured")
        self.assertEqual(headers["x-hwid"], "configured-device")
        self.assertEqual(headers["x-device-os"], "iOS")

    async def test_fixed_hwid_uses_happ_user_agent_for_browser_request(self):
        with patch.dict("os.environ", {"UPSTREAM_X_HWID": "configured-device"}, clear=True):
            headers = relay._upstream_request_headers(
                make_request([(b"user-agent", b"Mozilla/5.0 Chrome/browser")])
            )

        self.assertEqual(headers["user-agent"], "Happ")
        self.assertEqual(headers["x-hwid"], "configured-device")

    async def test_payload_inspection_counts_servers_and_detects_limit_message(self):
        body = base64.b64encode(
            b"vless://one@example\nvmess://two@example\n"
        )
        self.assertEqual(relay._inspect_payload(body), (2, False))

        placeholder = base64.b64encode(b"DEVICE LIMIT FULL (5/5) - remove a device first")
        self.assertEqual(relay._inspect_payload(placeholder), (0, True))

    async def test_client_profile_change_refetches_instead_of_serving_wrong_cache(self):
        seen = []
        profiles = {
            "Browser/test": b"vless://browser@browser.example:443#Browser",
            "Happ/test": b"vless://happ@happ.example:443#Happ",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            user_agent = request.headers.get("user-agent", "")
            seen.append(user_agent)
            return httpx.Response(
                200,
                content=profiles[user_agent],
                headers={"content-type": "text/plain"},
            )

        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        browser_response = await relay.serve_subscription(make_request([(b"user-agent", b"Browser/test")]))
        happ_response = await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))

        self.assertEqual(browser_response.body, profiles["Browser/test"])
        self.assertEqual(happ_response.body, profiles["Happ/test"])
        self.assertEqual(seen, ["Browser/test", "Happ/test"])

    async def test_empty_upstream_profile_is_rejected_without_replacing_snapshot(self):
        previous = relay.SubscriptionSnapshot(
            b"vless://saved@saved.example:443#Saved", {}, time.monotonic() - 10
        )
        relay._snapshot = previous
        relay._upstream_headers = {"user-agent": "Happ/previous"}
        relay.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"", headers={"content-type": "text/plain"})
            )
        )

        with self.assertRaises(HTTPException) as caught:
            await relay.serve_subscription(make_request([(b"user-agent", b"Happ/new")]))

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIs(relay._snapshot, previous)

    async def test_device_limit_is_forwarded_intact_and_skips_sas_merge(self):
        placeholder = base64.b64encode(b"DEVICE LIMIT FULL (5/5) - remove a device first")
        sas_requests = []
        relay.SAS_SUBSCRIPTION_URL = "https://sas.invalid/"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://sas.invalid/":
                sas_requests.append(request.url)
                return httpx.Response(200, content=b"vless://sas@sas.example:443#SAS")
            return httpx.Response(200, content=placeholder, headers={"content-type": "text/plain"})

        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))

        self.assertEqual(response.body, placeholder)
        self.assertEqual(sas_requests, [])

    async def test_relay_combines_primary_and_sas_and_keeps_primary_metadata(self):
        main_uri = "vless://main-id@main.example:443#Main"
        sas_uri = "trojan://sas-secret@sas.example:443#SAS%20Node"
        body_by_url = {
            "https://subscription.invalid/private": base64.b64encode(main_uri.encode()),
            "https://sas.invalid/": base64.b64encode(f"{sas_uri}\n{main_uri}".encode()),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            body = body_by_url[str(request.url)]
            headers = {"content-type": "text/plain; charset=utf-8"}
            if str(request.url) == "https://subscription.invalid/private":
                headers["subscription-userinfo"] = "upload=7; download=11; total=99; expire=0"
            return httpx.Response(200, content=body, headers=headers)

        relay.SAS_SUBSCRIPTION_URL = "https://sas.invalid/"
        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.decode().splitlines(), [main_uri, sas_uri])
        self.assertEqual(response.headers["subscription-userinfo"], "upload=7; download=11; total=99; expire=0")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    async def test_relay_uses_last_good_sas_snapshot_when_sas_is_temporarily_unavailable(self):
        main_uri = "vless://main-id@main.example:443#Main"
        sas_uri = "vless://sas-id@sas.example:443#SAS"
        headers = {"user-agent": "Happ/test"}
        cache_key = tuple(sorted(headers.items()))
        relay.SAS_SUBSCRIPTION_URL = "https://sas.invalid/"
        relay._sas_snapshots[cache_key] = relay.SubscriptionSnapshot(sas_uri.encode(), {}, time.monotonic() - 600)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://sas.invalid/":
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, content=main_uri.encode(), headers={"content-type": "text/plain"})

        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))
        self.assertEqual(response.body.decode().splitlines(), [main_uri, sas_uri])

    async def test_relay_does_not_publish_partial_feed_before_first_sas_snapshot(self):
        relay.SAS_SUBSCRIPTION_URL = "https://sas.invalid/"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://sas.invalid/":
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, content=b"vless://main-id@main.example:443#Main", headers={"content-type": "text/plain"})

        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(HTTPException) as caught:
            await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIsNone(relay._snapshot)

    async def test_html_page_is_rejected_and_not_cached(self):
        relay.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="<!doctype html><html>panel</html>", headers={"content-type": "text/html"})
            )
        )

        with self.assertRaises(HTTPException) as caught:
            await relay.serve_subscription(make_request([(b"user-agent", b"Browser/test")]))

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIsNone(relay._snapshot)

    async def test_device_limit_response_is_forwarded_without_rewriting(self):
        placeholder = base64.b64encode(
            b"vless://not-a-real-server#DEVICE LIMIT FULL (5/5) - remove a device first"
        )
        relay.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=placeholder,
                    headers={"content-type": "text/plain; charset=utf-8"},
                )
            )
        )

        response = await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, placeholder)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIsNone(relay._snapshot)

    async def test_refresh_keeps_last_good_profile_when_provider_reports_device_limit(self):
        original = relay.SubscriptionSnapshot(
            b"vless://saved@saved.example:443#Saved", {}, time.monotonic() - 10
        )
        placeholder = base64.b64encode(b"DEVICE LIMIT FULL (5/5) - remove a device first")
        relay._snapshot = original
        relay._upstream_headers = {"user-agent": "Happ/test"}

        async def fetch(headers):
            if not getattr(fetch, "called", False):
                fetch.called = True
                return relay.SubscriptionSnapshot(placeholder, {}, time.monotonic())
            raise asyncio.CancelledError

        with patch.object(relay, "REFRESH_INTERVAL_SECONDS", 0), patch.object(
            relay, "_fetch_snapshot", side_effect=fetch
        ):
            with self.assertRaises(asyncio.CancelledError):
                await relay._refresh_loop()

        self.assertIs(relay._snapshot, original)

    async def test_refresh_reuses_headers_and_keeps_last_good_snapshot_on_failure(self):
        original = relay.SubscriptionSnapshot(b"old", {}, 1.0)
        replacement = relay.SubscriptionSnapshot(b"new", {}, 2.0)
        seen_headers = []
        calls = 0

        async def fetch(headers):
            nonlocal calls
            seen_headers.append(headers.copy())
            calls += 1
            if calls == 1:
                return replacement
            if calls == 2:
                raise HTTPException(status_code=502, detail="mock failure")
            raise asyncio.CancelledError

        relay._snapshot = original
        stable_headers = {"user-agent": "Happ/test", "x-hwid": "stable"}
        with patch.object(relay, "REFRESH_INTERVAL_SECONDS", 0), patch.object(
            relay, "_fetch_snapshot", side_effect=fetch
        ):
            with self.assertRaises(asyncio.CancelledError):
                relay._upstream_headers = stable_headers
                await relay._refresh_loop()

        self.assertIs(relay._snapshot, replacement)
        self.assertEqual(seen_headers, [stable_headers, stable_headers, stable_headers])


if __name__ == "__main__":
    unittest.main()
