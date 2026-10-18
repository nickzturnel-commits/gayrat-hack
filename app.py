import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse


load_dotenv()
logger = logging.getLogger(__name__)

SUBSCRIPTION_URL = os.getenv("SUBSCRIPTION_URL", "").strip()
REFRESH_INTERVAL_SECONDS = 15 * 60
REQUEST_HEADERS = frozenset(
    {
        "user-agent",
        "accept-language",
        "x-hwid",
        "x-device-info",
        "x-device-model",
        "x-device-os",
        "x-ver-os",
        "x-app-version",
    }
)
SKIP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "date",
        "keep-alive",
        "server",
        "set-cookie",
        "strict-transport-security",
        "transfer-encoding",
    }
)


@dataclass(frozen=True)
class SubscriptionSnapshot:
    body: bytes
    headers: dict[str, str]
    fetched_at: float


http: httpx.AsyncClient | None = None
_snapshot: SubscriptionSnapshot | None = None
_upstream_headers: dict[str, str] | None = None
_refresh_task: asyncio.Task | None = None
_refresh_lock = asyncio.Lock()


def _client_headers(request: Request) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in request.headers.items()
        if name.lower() in REQUEST_HEADERS
    }


async def _fetch_snapshot(headers: dict[str, str]) -> SubscriptionSnapshot:
    if not SUBSCRIPTION_URL:
        raise HTTPException(status_code=503, detail="SUBSCRIPTION_URL is not configured.")
    if http is None:
        raise HTTPException(status_code=503, detail="Subscription service is not ready.")

    try:
        upstream = await http.get(SUBSCRIPTION_URL, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not fetch the subscription.") from exc

    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Subscription server returned {upstream.status_code}.",
        )

    content_type = upstream.headers.get("content-type", "").lower()
    body_prefix = upstream.content.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if "text/html" in content_type or body_prefix.startswith((b"<!doctype html", b"<html")):
        raise HTTPException(
            status_code=502,
            detail="Upstream returned a web page instead of subscription data.",
        )

    response_headers = {
        name.lower(): value
        for name, value in upstream.headers.items()
        if name.lower() not in SKIP_RESPONSE_HEADERS
    }
    response_headers["cache-control"] = "no-store, no-cache, must-revalidate"
    logger.info(
        "Fetched subscription payload (content-type=%s, bytes=%d, device-headers=%s).",
        content_type or "unspecified",
        len(upstream.content),
        ",".join(sorted(name for name in headers if name.startswith("x-"))) or "none",
    )
    return SubscriptionSnapshot(
        body=upstream.content,
        headers=response_headers,
        fetched_at=time.monotonic(),
    )


async def _refresh_loop() -> None:
    global _snapshot
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            async with _refresh_lock:
                current_headers = _upstream_headers
                if current_headers is None:
                    continue
                updated = await _fetch_snapshot(current_headers)
                _snapshot = updated
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never include the secret URL or subscription body in logs.
            logger.warning("Subscription refresh failed (%s).", type(exc).__name__)
            continue
        logger.info("Subscription cache refreshed.")


async def current_snapshot(request: Request) -> SubscriptionSnapshot:
    global _snapshot, _upstream_headers, _refresh_task

    request_headers = _client_headers(request)
    if _snapshot is not None and request_headers == _upstream_headers:
        return _snapshot

    async with _refresh_lock:
        if _snapshot is not None and request_headers == _upstream_headers:
            return _snapshot

        # A browser, health probe, or VPN client can be the first caller.
        # Refresh whenever the incoming client profile differs so one caller
        # cannot poison the shared cache for another.
        updated = await _fetch_snapshot(request_headers)
        _snapshot = updated
        _upstream_headers = request_headers

        if _refresh_task is not None:
            _refresh_task.cancel()
            try:
                await _refresh_task
            except asyncio.CancelledError:
                pass

        _refresh_task = asyncio.create_task(_refresh_loop())
        return _snapshot


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http, _refresh_task
    http = httpx.AsyncClient(follow_redirects=True, timeout=20.0)
    try:
        yield
    finally:
        if _refresh_task is not None:
            _refresh_task.cancel()
            try:
                await _refresh_task
            except asyncio.CancelledError:
                pass
            _refresh_task = None
        await http.aclose()
        http = None


app = FastAPI(title="VPN subscription relay", lifespan=lifespan)


async def serve_subscription(request: Request) -> Response:
    if request.method == "HEAD":
        return Response(status_code=200, media_type="text/plain")

    snapshot = await current_snapshot(request)
    return Response(content=snapshot.body, headers=snapshot.headers)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def latest_subscription(request: Request):
    return await serve_subscription(request)


@app.api_route("/sub.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def subscription_file(request: Request):
    return await serve_subscription(request)


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"
