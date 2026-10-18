import asyncio
import base64
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
        relay.SUBSCRIPTION_URL = "https://subscription.invalid/private"
        relay._snapshot = None
        relay._upstream_headers = None
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
        relay._snapshot = self.previous_snapshot
        relay._upstream_headers = self.previous_headers
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

    async def test_client_profile_change_refetches_instead_of_serving_wrong_cache(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            user_agent = request.headers.get("user-agent", "")
            seen.append(user_agent)
            return httpx.Response(
                200,
                content=f"profile={user_agent}".encode(),
                headers={"content-type": "text/plain"},
            )

        relay.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        browser_response = await relay.serve_subscription(make_request([(b"user-agent", b"Browser/test")]))
        happ_response = await relay.serve_subscription(make_request([(b"user-agent", b"Happ/test")]))

        self.assertEqual(browser_response.body, b"profile=Browser/test")
        self.assertEqual(happ_response.body, b"profile=Happ/test")
        self.assertEqual(seen, ["Browser/test", "Happ/test"])

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
