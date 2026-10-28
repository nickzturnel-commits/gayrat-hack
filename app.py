import asyncio
import base64
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

import panel as admin_panel


load_dotenv()
logger = logging.getLogger(__name__)

SUBSCRIPTION_URL = os.getenv("SUBSCRIPTION_URL", "").strip()
SAS_SUBSCRIPTION_URL = os.getenv("SAS_SUBSCRIPTION_URL", "").strip()
REFRESH_INTERVAL_SECONDS = 5 * 60
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
UPSTREAM_HEADER_ENV = {
    "user-agent": "UPSTREAM_USER_AGENT",
    "x-hwid": "UPSTREAM_X_HWID",
    "x-device-info": "UPSTREAM_X_DEVICE_INFO",
    "x-device-model": "UPSTREAM_X_DEVICE_MODEL",
    "x-device-os": "UPSTREAM_X_DEVICE_OS",
    "x-ver-os": "UPSTREAM_X_VER_OS",
    "x-app-version": "UPSTREAM_X_APP_VERSION",
}
UPSTREAM_DEFAULT_USER_AGENT = "Happ"
SERVER_URI_PATTERN = re.compile(rb"(?i)(?:vless|vmess|trojan|ss|hysteria2?|tuic)://")
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
_sas_snapshots: dict[tuple[tuple[str, str], ...], SubscriptionSnapshot] = {}
_refresh_task: asyncio.Task | None = None
_refresh_lock = asyncio.Lock()


def _client_headers(request: Request) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in request.headers.items()
        if name.lower() in REQUEST_HEADERS
    }


def _upstream_request_headers(request: Request) -> dict[str, str]:
    """Use configured device headers when present; otherwise forward the caller profile."""
    headers = _client_headers(request)
    for header, env_name in UPSTREAM_HEADER_ENV.items():
        configured_value = os.getenv(env_name, "").strip()
        if configured_value:
            headers[header] = configured_value

    # If a fixed HWID is configured but this request came from a browser, don't
    # send the browser identity upstream. Use a Happ-style UA unless overridden.
    fixed_hwid = os.getenv("UPSTREAM_X_HWID", "").strip()
    configured_ua = os.getenv("UPSTREAM_USER_AGENT", "").strip()
    incoming_ua = _client_headers(request).get("user-agent", "").lower()
    is_browser = any(token in incoming_ua for token in ("mozilla", "chrome", "safari", "firefox", "edg/"))
    if fixed_hwid and not configured_ua and is_browser:
        headers["user-agent"] = UPSTREAM_DEFAULT_USER_AGENT
    return headers


def _profile_summary(headers: dict[str, str]) -> str:
    user_agent = headers.get("user-agent", "").lower()
    if "happ" in user_agent:
        client = "happ"
    elif any(token in user_agent for token in ("mozilla", "chrome", "safari", "firefox", "edg/")):
        client = "browser"
    elif user_agent:
        client = "other"
    else:
        client = "none"
    return "client=%s, user-agent=%s, hwid=%s, device-headers=%s" % (
        client,
        "present" if user_agent else "missing",
        "present" if headers.get("x-hwid") else "missing",
        ",".join(sorted(name for name in headers if name.startswith("x-"))) or "none",
    )


def _inspect_payload(body: bytes) -> tuple[int, bool]:
    """Return URI count and device-limit marker status without exposing body data."""
    candidate = body
    compact = b"".join(body.split())
    if compact and len(compact) <= 4 * 1024 * 1024:
        try:
            decoded = base64.b64decode(
                compact + b"=" * ((4 - len(compact) % 4) % 4),
                altchars=b"-_",
                validate=True,
            )
            if SERVER_URI_PATTERN.search(decoded) or b"device limit" in decoded.lower():
                candidate = decoded
        except (ValueError, base64.binascii.Error):
            pass
    lowered = candidate.lower()
    device_limit = b"device limit full" in lowered or b"remove a device first" in lowered
    return len(SERVER_URI_PATTERN.findall(candidate)), device_limit


async def _fetch_snapshot(headers: dict[str, str]) -> SubscriptionSnapshot:
    if not SUBSCRIPTION_URL:
        raise HTTPException(status_code=503, detail="SUBSCRIPTION_URL is not configured.")
    if http is None:
        raise HTTPException(status_code=503, detail="Subscription service is not ready.")

    logger.info("Fetching upstream subscription (%s).", _profile_summary(headers))
    try:
        upstream = await http.get(SUBSCRIPTION_URL, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Subscription fetch failed (transport=%s; %s).", type(exc).__name__, _profile_summary(headers))
        raise HTTPException(status_code=502, detail="Could not fetch the subscription.") from exc

    if upstream.status_code >= 400:
        logger.warning(
            "Subscription source returned an error (status=%d, content-type=%s, bytes=%d; %s).",
            upstream.status_code,
            upstream.headers.get("content-type", "unspecified").lower(),
            len(upstream.content),
            _profile_summary(headers),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Subscription server returned {upstream.status_code}.",
        )

    content_type = upstream.headers.get("content-type", "").lower()
    uri_count, device_limit = _inspect_payload(upstream.content)
    logger.info(
        "Upstream response (status=%d, content-type=%s, bytes=%d, server-links=%d, device-limit=%s).",
        upstream.status_code,
        content_type or "unspecified",
        len(upstream.content),
        uri_count,
        "yes" if device_limit else "no",
    )
    if device_limit:
        logger.warning("Upstream returned its device-limit placeholder; forwarding it unchanged.")

    body_prefix = upstream.content.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if "text/html" in content_type or body_prefix.startswith((b"<!doctype html", b"<html")):
        logger.warning(
            "Subscription source returned HTML (content-type=%s, bytes=%d; %s).",
            content_type or "unspecified",
            len(upstream.content),
            _profile_summary(headers),
        )
        raise HTTPException(
            status_code=502,
            detail="Upstream returned a web page instead of subscription data.",
        )

    body = upstream.content
    if SAS_SUBSCRIPTION_URL:
        profile_key = tuple(sorted(headers.items()))
        try:
            sas = await http.get(SAS_SUBSCRIPTION_URL, headers=headers)
            if sas.status_code >= 400:
                raise ValueError(f"SAS ответил HTTP {sas.status_code}")
            invalid = admin_panel._invalid_payload(sas.content, sas.headers.get("content-type", ""))
            if invalid:
                raise ValueError(invalid)
            body = admin_panel.merge_subscription_bodies(
                upstream.content,
                sas.content,
                upstream.headers.get("content-type", ""),
                sas.headers.get("content-type", ""),
            )
            _sas_snapshots[profile_key] = SubscriptionSnapshot(
                body=sas.content,
                headers={},
                fetched_at=time.monotonic(),
            )
            if len(_sas_snapshots) > 64:
                oldest_key = min(_sas_snapshots, key=lambda key: _sas_snapshots[key].fetched_at)
                _sas_snapshots.pop(oldest_key, None)
        except Exception as exc:
            cached_sas = _sas_snapshots.get(profile_key)
            if cached_sas is None:
                logger.warning("SAS subscription fetch failed (%s); no saved SAS snapshot is available.", type(exc).__name__)
                raise HTTPException(status_code=502, detail="Could not fetch the SAS subscription.") from exc
            try:
                body = admin_panel.merge_subscription_bodies(
                    upstream.content,
                    cached_sas.body,
                    upstream.headers.get("content-type", ""),
                    "text/plain",
                )
            except ValueError as merge_exc:
                raise HTTPException(status_code=502, detail="Could not build the combined subscription.") from merge_exc
            logger.warning("SAS subscription refresh failed (%s); serving the last good SAS snapshot.", type(exc).__name__)

    response_headers = {
        name.lower(): value
        for name, value in upstream.headers.items()
        if name.lower() not in SKIP_RESPONSE_HEADERS
    }
    if SAS_SUBSCRIPTION_URL:
        response_headers["content-type"] = "text/plain; charset=utf-8"
    response_headers["cache-control"] = "no-store, no-cache, must-revalidate"
    logger.info(
        "Fetched subscription payload (content-type=%s, bytes=%d, device-headers=%s).",
        content_type or "unspecified",
        len(upstream.content),
        ",".join(sorted(name for name in headers if name.startswith("x-"))) or "none",
    )
    return SubscriptionSnapshot(
        body=body,
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

    request_headers = _upstream_request_headers(request)
    if _snapshot is not None and request_headers == _upstream_headers:
        logger.info("Subscription cache hit (age-seconds=%d).", max(0, int(time.monotonic() - _snapshot.fetched_at)))
        return _snapshot

    logger.info("Subscription cache miss; fetching with current request profile.")

    async with _refresh_lock:
        if _snapshot is not None and request_headers == _upstream_headers:
            logger.info("Subscription cache hit after lock (age-seconds=%d).", max(0, int(time.monotonic() - _snapshot.fetched_at)))
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
    logger.info(
        "Relay started (subscription-url-configured=%s, sas-url-configured=%s, fixed-hwid-configured=%s, fixed-user-agent-configured=%s).",
        "yes" if SUBSCRIPTION_URL else "no",
        "yes" if SAS_SUBSCRIPTION_URL else "no",
        "yes" if os.getenv("UPSTREAM_X_HWID", "").strip() else "no",
        "yes" if os.getenv("UPSTREAM_USER_AGENT", "").strip() else "no",
    )
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

    logger.info("Subscription request (%s).", _profile_summary(_upstream_request_headers(request)))
    snapshot = await current_snapshot(request)
    logger.info("Serving subscription (bytes=%d, age-seconds=%d).", len(snapshot.body), max(0, int(time.monotonic() - snapshot.fetched_at)))
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


admin_panel.install(app)
