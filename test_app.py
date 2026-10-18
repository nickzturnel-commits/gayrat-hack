import asyncio
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
                await relay._refresh_loop(stable_headers)

        self.assertIs(relay._snapshot, replacement)
        self.assertEqual(seen_headers, [stable_headers, stable_headers, stable_headers])


if __name__ == "__main__":
    unittest.main()
