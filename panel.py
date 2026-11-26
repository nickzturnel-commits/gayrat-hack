"""Admin panel, subscription generation and Google Drive sync API.

This module deliberately owns an upstream client/cache separate from the public relay.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import contextvars
from contextlib import asynccontextmanager
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import asyncpg
import httpx
import qrcode
import yaml
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt
from qrcode.image.svg import SvgPathImage
from starlette.middleware.base import BaseHTTPMiddleware
import happ_limit

log = logging.getLogger("vpn_panel")
router = APIRouter()
_test_environment = contextvars.ContextVar("panel_test_environment", default=False)

LEGACY_DRIVE_FILE_ID = "160lsZ2nDrSXTzA09r9jeGHnd6QyFylw9"
PANEL_UPSTREAM_DEFAULT_USER_AGENT = "Happ"
PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PUBLIC_ID_LENGTH = 6
PUBLIC_ID_ALLOCATION_ATTEMPTS = 32
UPSTREAM_TIMEOUT_SECONDS = 20.0
PANEL_CACHE_TTL_SECONDS = 300
GEO_FILE_CACHE_TTL_SECONDS = 24 * 60 * 60
SERVER_STALE_RETENTION_DAYS = 60
SESSION_SECONDS = 12 * 60 * 60
DB_HINT = ("Не удалось подключиться к базе. Если используется прямой хост db.*.supabase.co, "
           "возьмите в Supabase (Connect → Session pooler) строку подключения и поставьте её в DATABASE_URL")
URI_LINE = re.compile(r"(?i)^\s*(?:vless|vmess|trojan|ssr?|hysteria2?|hy2|tuic|socks5?|https?|wireguard|anytls|ssh|mieru)://")
URI_ANY = re.compile(r"(?i)(?:vless|vmess|trojan|ssr?|hysteria2?|hy2|tuic|socks5?|https?|wireguard|anytls|ssh|mieru)://")
DEVICE_MARKERS = (b"device limit full", b"remove a device first")
SKIP_UPSTREAM_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


class UnsupportedProxyConfig(ValueError):
    """A source contains proxy entries that cannot safely be converted yet."""

DEFAULT_SETTINGS = {
    "title": "Secure X", "support_url": "https://t.me/moserrr1", "web_page_url": "", "update_interval": 1,
    "profile_format": "url",
    "timezone": "Asia/Ashgabat",
    # Individual profiles share provider usage. Admins may hide the counter on
    # new user profiles, while the legacy account profile stays visible.
    "default_traffic_limit_bytes": None,
    "hide_traffic_for_new_users": False,
    "include_sas_for_new_users": True,
    "device_limit_for_new_users": None,
}

# Applied only to test subscriptions. Keep the palette aligned with the black
# Happ theme used as the visual reference; no Provider ID or domain is copied.
TEST_HAPP_COLOR_PROFILE = {
    "backgroundColors": ["#050505FF", "#0B0B0BFF", "#151515FF"],
    "backgroundGradientRotationAngle": 0,
    "backgroundGradientColorIntensity": 1,
    "backgroundImageType": "system",
    "buttonImageType": "light",
    "buttonColor": "#242424FF",
    "buttonTextColor": "#FFFFFFFF",
    "buttonTimerColor": "#FFFFFFFF",
    "powerIconColor": "#000000CC",
    "serverRowBackgroundColor": "#161616CC",
    "serverRowTitleTextColor": "#FFFFFFFF",
    "serverRowSubTitleTextColor": "#A6A6A6FF",
    "serverRowChevronColor": "#FFFFFFFF",
    "selectedServerRowColor": "#303030E6",
    "subsHeaderColor": "#101010FF",
    "subscriptionInfoBackgroundColor": "#0D0D0DFF",
    "subscriptionInfoTextColor": "#FFFFFFFF",
    "subscriptionTrafficBackgroundColor": "#202020FF",
    "disclosureHeaderTextColor": "#FFFFFFFF",
    "disclosureSubHeaderTextColor": "#A6A6A6FF",
    "topBarButtonsColor": "#FFFFFFFF",
    "supportIconColor": "#FFFFFFFF",
    "profileWebPageIconColor": "#BDBDBDFF",
    "subHeaderButtonColor": "#FFFFFFFF",
    "additionalOptionsButtonColor": "#FFFFFF99",
    "settingsControlsTintColor": "#FFFFFFFF",
    "elipseColors": ["#FFFFFFCC", "#8A8A8AFF", "#404040FF"],
}


def parse_database_url(value: str) -> dict:
    """Parse PostgreSQL URLs without letting urllib confuse @/spaces in passwords."""
    raw = (value or "").strip()
    match = re.match(r"^(postgres(?:ql)?)://(.+)$", raw, re.I)
    if not match:
        raise ValueError("DATABASE_URL must use postgres:// or postgresql://")
    authority_and_path = match.group(2)
    if "@" not in authority_and_path:
        raise ValueError("DATABASE_URL is missing host")
    credentials, location = authority_and_path.rsplit("@", 1)
    if ":" not in credentials:
        raise ValueError("DATABASE_URL is missing password")
    user, password = credentials.split(":", 1)
    if "/" not in location:
        raise ValueError("DATABASE_URL is missing database name")
    host_port, database_query = location.split("/", 1)
    db_part = database_query.split("?", 1)[0]
    if not db_part or not host_port:
        raise ValueError("DATABASE_URL is incomplete")
    # IPv6 literal hosts are accepted even though Supabase hostnames are DNS names.
    if host_port.startswith("["):
        close = host_port.find("]")
        if close < 0:
            raise ValueError("DATABASE_URL has invalid host")
        host = host_port[1:close]
        port = int(host_port[close + 2:]) if host_port[close + 1:].startswith(":") else 5432
    else:
        host, sep, port_text = host_port.rpartition(":")
        if not sep:
            host, port = host_port, 5432
        else:
            port = int(port_text)
    if not (1 <= port <= 65535):
        raise ValueError("DATABASE_URL has invalid port")
    return {"host": host, "port": port, "user": unquote(user), "password": unquote(password),
            "database": unquote(db_part), "ssl": "require", "statement_cache_size": 0 if port == 6543 else 100}


def _slugify(name: str) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:36] or "user"
    return f"{base}-{secrets.token_hex(3)}"


def _xray_json_uris(text: str) -> list[str] | None:
    """Convert supported Xray JSON outbounds to standard proxy share links."""
    try:
        document = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    documents = document if isinstance(document, list) else [document]
    documents = [item for item in documents if isinstance(item, dict) and isinstance(item.get("outbounds"), list)]
    if not documents:
        return None

    by_identity: dict[tuple[str, str], tuple[int, str]] = {}
    best_rank_by_connection: dict[str, int] = {}
    unsupported: set[str] = set()

    def add_link(uri: str, rank: int):
        try:
            parsed = parse_server_uri(uri)
            # Keep provider entries with the same connection credentials but
            # different display labels. The label is part of what the user
            # expects to see in Happ, even when both entries route identically.
            identity = (parsed["id"], parsed["source_key"])
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return
        best_rank_by_connection[parsed["id"]] = max(
            rank, best_rank_by_connection.get(parsed["id"], -1))
        previous = by_identity.get(identity)
        if previous is None or rank > previous[0]:
            by_identity[identity] = (rank, uri)

    def valid_endpoint(address_value, port_value):
        if not isinstance(address_value, str) or isinstance(port_value, bool):
            return None
        address = address_value.strip()
        try:
            port = int(port_value)
        except (TypeError, ValueError):
            return None
        if not address or any(char.isspace() for char in address) or not 1 <= port <= 65535:
            return None
        host = f"[{address}]" if ":" in address and not address.startswith("[") else address
        return host, port

    def query_uri(scheme: str, userinfo: str, host: str, port: int,
                  params: list[tuple[str, str]], label: str) -> str:
        query = urlencode(params, doseq=True, quote_via=quote, safe="")
        return f"{scheme}://{userinfo}@{host}:{port}?{query}#{quote(label, safe='')}"

    def outbound_label(outbound: dict, profile_name: str, fallback: str,
                       *, singleton: bool, protocol: str) -> tuple[str, int]:
        tag = str(outbound.get("remarks") or outbound.get("tag") or "").strip()
        generic = bool(re.fullmatch(
            rf"(?:proxy|outbound|{re.escape(protocol)})(?:[-_ ]?\d+)?", tag, flags=re.I))
        if tag and not generic:
            return tag, 2
        if singleton and profile_name and not re.search(r"(?i)auto|автовыбор|selector", profile_name):
            return profile_name, 3
        if profile_name and not re.search(r"(?i)auto|автовыбор|selector", profile_name):
            return profile_name, 1
        return fallback, 0

    def transport_values(stream: dict, network: str) -> dict:
        return stream.get(f"{network}Settings") if isinstance(stream.get(f"{network}Settings"), dict) else {}

    def append_transport_params(params: list[tuple[str, str]], network: str, connection: dict):
        if network in {"ws", "httpupgrade"}:
            path = connection.get("path")
            host_header = connection.get("host")
            if network == "ws" and isinstance(connection.get("headers"), dict):
                host_header = connection["headers"].get("Host") or host_header
            if isinstance(path, str) and path:
                params.append(("path", path))
            if isinstance(host_header, str) and host_header:
                params.append(("host", host_header))
        elif network == "grpc":
            service_name = connection.get("serviceName")
            authority = connection.get("authority")
            if isinstance(service_name, str) and service_name:
                params.append(("serviceName", service_name))
            if isinstance(authority, str) and authority:
                params.append(("authority", authority))
            mode = connection.get("mode")
            if not isinstance(mode, str) or not mode:
                mode = "multi" if connection.get("multiMode") else "gun"
            if mode != "gun":
                params.append(("mode", mode))
        elif network == "xhttp":
            for key in ("path", "host", "mode"):
                value = connection.get(key)
                if isinstance(value, str) and value:
                    params.append((key, value))
            extra = connection.get("extra")
            if isinstance(extra, dict):
                params.append(("extra", json.dumps(extra, separators=(",", ":"), ensure_ascii=False)))
            elif isinstance(extra, str) and extra:
                params.append(("extra", extra))
        elif network == "tcp":
            header = connection.get("header")
            if isinstance(header, dict) and isinstance(header.get("type"), str) and header["type"] != "none":
                params.append(("headerType", header["type"]))
                request = header.get("request") if isinstance(header.get("request"), dict) else {}
                path_values = request.get("path")
                if isinstance(path_values, list) and path_values:
                    params.append(("path", ",".join(str(item) for item in path_values)))
                request_headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
                host_values = request_headers.get("Host")
                if isinstance(host_values, list) and host_values:
                    params.append(("host", ",".join(str(item) for item in host_values)))

    for profile in documents:
        outbounds = profile["outbounds"]
        if not isinstance(outbounds, list):
            continue
        vless_outbounds = [item for item in outbounds
                           if isinstance(item, dict) and str(item.get("protocol", "")).lower() == "vless"]
        profile_name = str(profile.get("remarks") or "").strip()
        singleton_profile = len(vless_outbounds) == 1

        for outbound in outbounds:
            if not isinstance(outbound, dict):
                continue
            protocol = str(outbound.get("protocol", "")).lower()
            settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
            if protocol not in {"ss", "shadowsocks", "trojan", "vmess", "vless", "freedom", "blackhole", "dns", "loopback", "direct"}:
                if isinstance(settings.get("servers"), list) or isinstance(settings.get("vnext"), list):
                    unsupported.add(protocol or "unknown")
                continue
            if protocol in {"ss", "shadowsocks"}:
                servers = settings.get("servers")
                if not isinstance(servers, list):
                    continue
                for index, server in enumerate(servers, 1):
                    if not isinstance(server, dict):
                        continue
                    endpoint = valid_endpoint(server.get("address"), server.get("port"))
                    method, password = server.get("method"), server.get("password")
                    if endpoint is None or not isinstance(method, str) or not isinstance(password, str) or not password:
                        continue
                    host, port = endpoint
                    auth = base64.urlsafe_b64encode(f"{method.strip()}:{password}".encode("utf-8")).decode("ascii").rstrip("=")
                    remark = str(outbound.get("remarks") or outbound.get("tag") or profile_name).strip()
                    if len(servers) > 1 and remark:
                        remark = f"{remark} {index}"
                    label = remark or f"{host}:{port}"
                    add_link(f"ss://{auth}@{host}:{port}#{quote(label, safe='')}", 2 if remark else 0)
                continue

            if protocol in {"trojan", "vmess"}:
                servers = settings.get("servers") if protocol == "trojan" else settings.get("vnext")
                if not isinstance(servers, list):
                    continue
                stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
                network = str(stream.get("network") or "tcp").lower()
                if network not in {"tcp", "kcp", "ws", "http", "grpc", "httpupgrade", "xhttp"}:
                    unsupported.add(f"{protocol}/{network}")
                    continue
                security = str(stream.get("security") or ("tls" if protocol == "trojan" else "none")).lower()
                if security not in ({"tls", "none"} if protocol == "trojan" else {"tls", "none"}):
                    unsupported.add(f"{protocol}/{security}")
                    continue
                tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
                connection = transport_values(stream, network)
                outbound_tag = str(outbound.get("remarks") or outbound.get("tag") or "").strip()
                eligible_count = sum(
                    1 for candidate in outbounds
                    if isinstance(candidate, dict)
                    and str(candidate.get("protocol", "")).lower() == protocol
                )

                for index, server in enumerate(servers, 1):
                    if not isinstance(server, dict):
                        continue
                    endpoint = valid_endpoint(server.get("address"), server.get("port"))
                    if endpoint is None:
                        continue
                    host, port = endpoint
                    if protocol == "trojan":
                        password = server.get("password")
                        if not isinstance(password, str) or not password:
                            continue
                        params: list[tuple[str, str]] = [("type", network)]
                        if security == "tls":
                            params.append(("security", "tls"))
                            for key, uri_key in (("serverName", "sni"), ("fingerprint", "fp")):
                                value = tls.get(key)
                                if isinstance(value, str) and value:
                                    params.append((uri_key, value))
                            alpn = tls.get("alpn")
                            if isinstance(alpn, list) and alpn:
                                params.append(("alpn", ",".join(str(item) for item in alpn)))
                        append_transport_params(params, network, connection)
                        label, rank = outbound_label(
                            outbound, profile_name, f"{host}:{port}",
                            singleton=eligible_count == 1, protocol=protocol)
                        if len(servers) > 1 and (outbound_tag or profile_name):
                            label = f"{label} {index}"
                        add_link(query_uri("trojan", quote(password, safe="-._~"), host,
                                           port, params, label), rank)
                        continue

                    users = server.get("users")
                    if not isinstance(users, list):
                        continue
                    for user_index, user in enumerate(users, 1):
                        if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"].strip():
                            continue
                        if security == "tls" and not tls:
                            tls = {}
                        label, rank = outbound_label(
                            outbound, profile_name, f"{host}:{port}",
                            singleton=eligible_count == 1, protocol=protocol)
                        if len(servers) > 1:
                            label = f"{label} {index}"
                        if len(users) > 1:
                            label = f"{label} {user_index}"
                        header = connection.get("header") if isinstance(connection.get("header"), dict) else {}
                        host_header = connection.get("host")
                        if network == "ws" and isinstance(connection.get("headers"), dict):
                            host_header = connection["headers"].get("Host") or host_header
                        vmess_obj = {
                            "v": "2", "ps": label, "add": host.strip("[]"), "port": str(port),
                            "id": user["id"].strip(), "aid": str(user.get("alterId", 0)),
                            "scy": str(user.get("security") or "auto"), "net": network,
                            "type": str(header.get("type") or "none"),
                            "host": str(host_header or tls.get("serverName") or ""),
                            "path": str(connection.get("path") or ""),
                        }
                        if security == "tls":
                            vmess_obj["tls"] = "tls"
                            if tls.get("serverName"):
                                vmess_obj["sni"] = tls["serverName"]
                            if tls.get("fingerprint"):
                                vmess_obj["fp"] = tls["fingerprint"]
                            if isinstance(tls.get("alpn"), list):
                                vmess_obj["alpn"] = ",".join(str(item) for item in tls["alpn"])
                        if network == "grpc" and connection.get("serviceName"):
                            vmess_obj["path"] = connection["serviceName"]
                            vmess_obj["type"] = "multi" if connection.get("multiMode") else "gun"
                        encoded = base64.b64encode(json.dumps(
                            vmess_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")
                        add_link("vmess://" + encoded, rank)
                continue

            if protocol != "vless":
                continue
            vnext = settings.get("vnext")
            if not isinstance(vnext, list):
                continue
            stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
            network = str(stream.get("network") or "tcp").lower()
            if network not in {"tcp", "kcp", "ws", "http", "grpc", "httpupgrade", "xhttp"}:
                unsupported.add(f"{protocol}/{network}")
                continue
            security = str(stream.get("security") or "none").lower()
            if security not in {"none", "tls", "reality"}:
                unsupported.add(f"{protocol}/{security}")
                continue
            tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
            reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
            transport = {
                key: stream.get(key) for key in
                ("tcpSettings", "kcpSettings", "wsSettings", "httpSettings", "grpcSettings", "httpupgradeSettings", "xhttpSettings")
                if isinstance(stream.get(key), dict)
            }
            outbound_tag = str(outbound.get("remarks") or outbound.get("tag") or "").strip()
            generic_tag = bool(re.fullmatch(r"(?:proxy|outbound|vless)(?:[-_ ]?\d+)?", outbound_tag, flags=re.I))

            for server_index, server in enumerate(vnext, 1):
                if not isinstance(server, dict):
                    continue
                endpoint = valid_endpoint(server.get("address"), server.get("port"))
                users = server.get("users")
                if endpoint is None or not isinstance(users, list):
                    continue
                host, port = endpoint
                for user_index, user in enumerate(users, 1):
                    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"].strip():
                        continue
                    params: list[tuple[str, str]] = [("encryption", str(user.get("encryption") or "none")),
                                                     ("type", network)]
                    flow = str(user.get("flow") or "").strip()
                    if flow:
                        params.append(("flow", flow))
                    if security in {"tls", "reality"}:
                        params.append(("security", security))
                        secure_settings = reality if security == "reality" else tls
                        sni = secure_settings.get("serverName")
                        if isinstance(sni, str) and sni:
                            params.append(("sni", sni))
                        fingerprint = secure_settings.get("fingerprint")
                        if isinstance(fingerprint, str) and fingerprint:
                            params.append(("fp", fingerprint))
                        alpn = secure_settings.get("alpn")
                        if isinstance(alpn, list) and alpn:
                            params.append(("alpn", ",".join(str(item) for item in alpn)))
                        if security == "reality":
                            public_key = reality.get("publicKey") or reality.get("password")
                            short_id = reality.get("shortId")
                            spider_x = reality.get("spiderX")
                            if isinstance(public_key, str) and public_key:
                                params.append(("pbk", public_key))
                            if isinstance(short_id, str) and short_id:
                                params.append(("sid", short_id))
                            if isinstance(spider_x, str) and spider_x:
                                params.append(("spx", spider_x))
                            for key in ("pqv",):
                                value = reality.get(key)
                                if isinstance(value, str) and value:
                                    params.append((key, value))
                    elif security != "none":
                        continue

                    connection = transport.get(f"{network}Settings", {})
                    if network in {"ws", "httpupgrade"}:
                        path = connection.get("path")
                        if isinstance(path, str) and path:
                            params.append(("path", path))
                        host_header = connection.get("host")
                        if network == "ws" and isinstance(connection.get("headers"), dict):
                            host_header = connection["headers"].get("Host") or host_header
                        if isinstance(host_header, str) and host_header:
                            params.append(("host", host_header))
                    elif network == "grpc":
                        service_name = connection.get("serviceName")
                        authority = connection.get("authority")
                        if isinstance(service_name, str) and service_name:
                            params.append(("serviceName", service_name))
                        if isinstance(authority, str) and authority:
                            params.append(("authority", authority))
                        mode = connection.get("mode")
                        if not isinstance(mode, str) or not mode:
                            mode = "multi" if connection.get("multiMode") else "gun"
                        if mode != "gun":
                            params.append(("mode", mode))
                    elif network == "xhttp":
                        path = connection.get("path")
                        host_header = connection.get("host")
                        mode = connection.get("mode")
                        if isinstance(path, str) and path:
                            params.append(("path", path))
                        if isinstance(host_header, str) and host_header:
                            params.append(("host", host_header))
                        if isinstance(mode, str) and mode:
                            params.append(("mode", mode))
                        extra = connection.get("extra")
                        if isinstance(extra, dict):
                            params.append(("extra", json.dumps(extra, separators=(",", ":"), ensure_ascii=False)))
                        elif isinstance(extra, str) and extra:
                            params.append(("extra", extra))
                    elif network == "kcp":
                        header = connection.get("header")
                        if isinstance(header, dict) and isinstance(header.get("type"), str) and header["type"]:
                            params.append(("headerType", header["type"]))
                        for key in ("seed", "mtu", "tti"):
                            value = connection.get(key)
                            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value):
                                params.append((key, str(value)))
                    elif network == "tcp":
                        header = connection.get("header")
                        if isinstance(header, dict) and isinstance(header.get("type"), str) and header["type"] != "none":
                            params.append(("headerType", header["type"]))
                            request = header.get("request") if isinstance(header.get("request"), dict) else {}
                            path_values = request.get("path")
                            if isinstance(path_values, list) and path_values:
                                params.append(("path", ",".join(str(item) for item in path_values)))
                            request_headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
                            host_values = request_headers.get("Host")
                            if isinstance(host_values, list) and host_values:
                                params.append(("host", ",".join(str(item) for item in host_values)))
                    finalmask = stream.get("finalmask")
                    if isinstance(finalmask, (dict, list)) and finalmask:
                        params.append(("fm", json.dumps(finalmask, separators=(",", ":"), ensure_ascii=False)))

                    if singleton_profile and profile_name:
                        label, rank = profile_name, 3
                    elif outbound_tag and not generic_tag:
                        label, rank = outbound_tag, 2
                    elif profile_name and not re.search(r"(?i)auto|автовыбор|selector", profile_name):
                        label, rank = profile_name, 1
                    else:
                        label, rank = f"{host}:{port}", 0
                    if len(vnext) > 1:
                        label = f"{label} {server_index}"
                    if len(users) > 1:
                        label = f"{label} {user_index}"
                    uri = query_uri("vless", quote(user["id"].strip(), safe="-._~"), host, port, params, label)
                    add_link(uri, rank)

    # Xray profile lists often repeat a generic auto-selector beside the
    # individually named nodes. Drop only that lower-ranked generic alias;
    # retain distinct provider labels for the same connection.
    if unsupported:
        raise UnsupportedProxyConfig(
            "В JSON найдены неподдерживаемые прокси-конфиги: " + ", ".join(sorted(unsupported)))
    converted = [uri for (connection_id, _name), (rank, uri) in by_identity.items()
                 if rank >= best_rank_by_connection.get(connection_id, rank)]
    return converted or None


def _singbox_json_uris(text: str) -> list[str] | None:
    """Convert common sing-box proxy outbounds to Happ-compatible share links."""
    try:
        document = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    documents = document if isinstance(document, list) else [document]
    documents = [item for item in documents if isinstance(item, dict) and isinstance(item.get("outbounds"), list)]
    if not documents:
        return None

    converted: list[str] = []
    supported = {"vless", "vmess", "trojan", "shadowsocks", "hysteria", "hysteria2", "tuic", "socks", "http", "anytls", "ssh"}
    unsupported: set[str] = set()
    for profile in documents:
        profile_name = str(profile.get("remarks") or profile.get("name") or "").strip()
        for outbound in profile["outbounds"]:
            if not isinstance(outbound, dict):
                continue
            protocol = str(outbound.get("type") or "").lower()
            if protocol not in supported:
                if outbound.get("server") and outbound.get("server_port"):
                    unsupported.add(protocol or "unknown")
                continue
            host_value = outbound.get("server")
            port_value = outbound.get("server_port")
            endpoint = _parse_config_endpoint(host_value, port_value)
            if endpoint is None:
                continue
            host, port = endpoint
            label = str(outbound.get("tag") or outbound.get("name") or profile_name or f"{host}:{port}").strip()
            tls = outbound.get("tls") if isinstance(outbound.get("tls"), dict) else {}
            transport = outbound.get("transport") if isinstance(outbound.get("transport"), dict) else {}
            network = str(transport.get("type") or "tcp").lower()
            if network not in {"tcp", "ws", "http", "grpc", "httpupgrade", "xhttp"}:
                unsupported.add(f"{protocol}/{network}")
                continue
            params: list[tuple[str, str]] = []
            server_name = tls.get("server_name") or tls.get("serverName")
            utls = tls.get("utls") if isinstance(tls.get("utls"), dict) else {}
            fingerprint = utls.get("fingerprint") or tls.get("fingerprint")
            alpn = tls.get("alpn")
            if isinstance(server_name, str) and server_name:
                params.append(("sni", server_name))
            if isinstance(fingerprint, str) and fingerprint:
                params.append(("fp", fingerprint))
            if isinstance(alpn, list) and alpn:
                params.append(("alpn", ",".join(str(item) for item in alpn)))
            if tls.get("insecure") is True:
                params.append(("insecure", "1"))
            if network in {"ws", "httpupgrade", "xhttp"}:
                path = transport.get("path")
                headers = transport.get("headers") if isinstance(transport.get("headers"), dict) else {}
                ws_host = headers.get("Host") or headers.get("host")
                if isinstance(path, str) and path:
                    params.append(("path", path))
                if isinstance(ws_host, str) and ws_host:
                    params.append(("host", ws_host))
                if network == "xhttp" and transport.get("mode"):
                    params.append(("mode", str(transport["mode"])))
            elif network == "grpc":
                for key in ("service_name", "authority", "mode"):
                    if isinstance(transport.get(key), str) and transport[key]:
                        params.append(("serviceName" if key == "service_name" else key, transport[key]))

            if protocol == "vless":
                user_id = outbound.get("uuid")
                if not isinstance(user_id, str) or not user_id:
                    continue
                params.insert(0, ("encryption", str(outbound.get("encryption") or "none")))
                params.insert(1, ("type", network))
                if outbound.get("flow"):
                    params.append(("flow", str(outbound["flow"])))
                reality = tls.get("reality") if isinstance(tls.get("reality"), dict) else {}
                if tls.get("enabled") or reality.get("enabled"):
                    params.append(("security", "reality" if reality.get("enabled") else "tls"))
                if reality.get("public_key"):
                    params.append(("pbk", str(reality["public_key"])))
                if reality.get("short_id"):
                    params.append(("sid", str(reality["short_id"])))
                if reality.get("spider_x"):
                    params.append(("spx", str(reality["spider_x"])))
                converted.append(_make_query_share("vless", quote(user_id, safe="-._~"), host, port, params, label))
            elif protocol == "trojan":
                password = outbound.get("password")
                if not isinstance(password, str) or not password:
                    continue
                params.insert(0, ("type", network))
                if tls.get("enabled") is not False and not any(key == "sni" for key, _ in params):
                    params.append(("security", "tls"))
                converted.append(_make_query_share("trojan", quote(password, safe="-._~"), host, port, params, label))
            elif protocol == "shadowsocks":
                method, password = outbound.get("method"), outbound.get("password")
                if not isinstance(method, str) or not isinstance(password, str) or not password:
                    continue
                auth = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
                converted.append(f"ss://{auth}@{host}:{port}#{quote(label, safe='')}")
            elif protocol == "vmess":
                user_id = outbound.get("uuid")
                if not isinstance(user_id, str) or not user_id:
                    continue
                vmess = {"v": "2", "ps": label, "add": host.strip("[]"), "port": str(port),
                         "id": user_id, "aid": str(outbound.get("alter_id", 0)),
                         "scy": str(outbound.get("security") or "auto"), "net": network,
                         "type": "none", "host": str(transport.get("headers", {}).get("Host", "")
                         if isinstance(transport.get("headers"), dict) else ""),
                         "path": str(transport.get("path") or "")}
                if tls.get("enabled"):
                    vmess["tls"] = "tls"
                    if server_name:
                        vmess["sni"] = server_name
                    if fingerprint:
                        vmess["fp"] = fingerprint
                    if isinstance(alpn, list):
                        vmess["alpn"] = ",".join(str(item) for item in alpn)
                encoded = base64.b64encode(json.dumps(vmess, separators=(",", ":"), ensure_ascii=False).encode()).decode()
                converted.append("vmess://" + encoded)
            elif protocol in {"hysteria", "hysteria2", "tuic"}:
                password = str(outbound.get("password") or "")
                if protocol == "tuic":
                    user_id = str(outbound.get("uuid") or "")
                    if not user_id or not password:
                        continue
                    userinfo = quote(user_id, safe="-._~") + ":" + quote(password, safe="-._~")
                    for source_key, uri_key in (("congestion_control", "congestion_control"),
                                                ("udp_relay_mode", "udp_relay_mode")):
                        if outbound.get(source_key):
                            params.append((uri_key, str(outbound[source_key])))
                    scheme = "tuic"
                elif protocol == "hysteria2":
                    if not password:
                        continue
                    userinfo, scheme = quote(password, safe="-._~"), "hysteria2"
                    obfs = outbound.get("obfs") if isinstance(outbound.get("obfs"), dict) else {}
                    if obfs.get("type"):
                        params.append(("obfs", str(obfs["type"])))
                    if obfs.get("password"):
                        params.append(("obfs-password", str(obfs["password"])))
                else:
                    auth = str(outbound.get("auth_str") or password)
                    if not auth:
                        continue
                    userinfo, scheme = quote(auth, safe="-._~"), "hysteria"
                    for source_key, uri_key in (("up_mbps", "upmbps"), ("down_mbps", "downmbps")):
                        if outbound.get(source_key) is not None:
                            params.append((uri_key, str(outbound[source_key])))
                    if outbound.get("obfs"):
                        params.append(("obfs", str(outbound["obfs"])))
                converted.append(_make_query_share(scheme, userinfo, host, port, params, label))
            elif protocol == "anytls":
                password = outbound.get("password")
                if not isinstance(password, str) or not password:
                    continue
                converted.append(_make_query_share("anytls", quote(password, safe="-._~"), host, port, params, label))
            elif protocol == "ssh":
                user = quote(str(outbound.get("username") or "root"), safe="-._~")
                password = outbound.get("password")
                userinfo = user + (":" + quote(str(password), safe="-._~") if password else "")
                converted.append(f"ssh://{userinfo}@{host}:{port}#{quote(label, safe='')}")
            elif protocol in {"socks", "http"}:
                scheme = "socks5" if protocol == "socks" else "http"
                user = str(outbound.get("username") or "")
                password = str(outbound.get("password") or "")
                userinfo = quote(user, safe="-._~")
                if password:
                    userinfo += ":" + quote(password, safe="-._~")
                auth = userinfo + "@" if userinfo else ""
                converted.append(f"{scheme}://{auth}{host}:{port}#{quote(label, safe='')}")
    if unsupported:
        raise UnsupportedProxyConfig(
            "В sing-box JSON найдены неподдерживаемые прокси-конфиги: " + ", ".join(sorted(unsupported)))
    return converted or None


def _parse_config_endpoint(host_value, port_value):
    if not isinstance(host_value, str) or isinstance(port_value, bool):
        return None
    host = host_value.strip()
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        return None
    if not host or any(char.isspace() for char in host) or not 1 <= port <= 65535:
        return None
    return (f"[{host}]" if ":" in host and not host.startswith("[") else host, port)


def _make_query_share(scheme: str, userinfo: str, host: str, port: int,
                      params: list[tuple[str, str]], label: str) -> str:
    query = urlencode(params, doseq=True, quote_via=quote, safe="")
    return f"{scheme}://{userinfo}@{host}:{port}" + (f"?{query}" if query else "") + f"#{quote(label, safe='')}"


def _clash_yaml_uris(text: str) -> list[str] | None:
    """Read Clash proxy lists and translate their common protocols to URI links."""
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, TypeError, ValueError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        return None
    normalized = []
    for proxy in document["proxies"]:
        if not isinstance(proxy, dict):
            continue
        kind = str(proxy.get("type") or "").lower()
        if kind not in {"vless", "vmess", "trojan", "ss", "shadowsocks", "hysteria", "hysteria2", "tuic", "socks5", "http", "anytls", "ssh"}:
            raise UnsupportedProxyConfig(
                f"В Clash YAML найден неподдерживаемый прокси-конфиг: {kind or 'unknown'}")
        if kind == "ss":
            kind = "shadowsocks"
        elif kind == "socks5":
            kind = "socks"
        network = str(proxy.get("network") or "tcp").lower()
        transport: dict = {"type": network}
        ws = proxy.get("ws-opts") if isinstance(proxy.get("ws-opts"), dict) else {}
        grpc = proxy.get("grpc-opts") if isinstance(proxy.get("grpc-opts"), dict) else {}
        if network in {"ws", "httpupgrade"}:
            transport["path"] = ws.get("path")
            transport["headers"] = ws.get("headers") or {}
        elif network == "grpc":
            transport["service_name"] = grpc.get("grpc-service-name")
            transport["mode"] = grpc.get("grpc-mode")
        reality = proxy.get("reality-opts") if isinstance(proxy.get("reality-opts"), dict) else {}
        tls = {"enabled": bool(proxy.get("tls") or proxy.get("servername") or reality),
               "server_name": proxy.get("servername") or proxy.get("sni"),
               "insecure": bool(proxy.get("skip-cert-verify")),
               "alpn": proxy.get("alpn"), "utls": {"fingerprint": proxy.get("client-fingerprint")},
               "reality": {"enabled": bool(reality), "public_key": reality.get("public-key"),
                           "short_id": reality.get("short-id"), "spider_x": reality.get("spider-x")}}
        item = {"type": kind, "tag": proxy.get("name"), "server": proxy.get("server"),
                "server_port": proxy.get("port"), "uuid": proxy.get("uuid"),
                "password": proxy.get("password"), "username": proxy.get("username") or proxy.get("user"),
                "method": proxy.get("cipher"),
                "alter_id": proxy.get("alterId", 0), "security": proxy.get("cipher"),
                "flow": proxy.get("flow"), "tls": tls, "transport": transport,
                "obfs": {"type": proxy.get("obfs"), "password": proxy.get("obfs-password")},
                "auth_str": proxy.get("auth-str"), "up_mbps": proxy.get("up-mbps"),
                "down_mbps": proxy.get("down-mbps")}
        if kind == "tuic":
            item["congestion_control"] = proxy.get("congestion-controller") or proxy.get("congestion_control")
            item["udp_relay_mode"] = proxy.get("udp-relay-mode") or proxy.get("udp_relay_mode")
        normalized.append(item)
    return _singbox_json_uris(json.dumps({"outbounds": normalized}, ensure_ascii=False))


def _structured_config_uris(text: str) -> list[str] | None:
    for parser in (_xray_json_uris, _singbox_json_uris, _clash_yaml_uris):
        converted = parser(text)
        if converted:
            return converted
    return None


def _decode_subscription(body: bytes) -> str:
    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ValueError("Апстрим вернул пустую подписку")
    compact = "".join(text.split())
    if len(compact) < 4 or len(compact) > 8 * 1024 * 1024:
        return text
    try:
        decoded = base64.b64decode(compact + "=" * ((4 - len(compact) % 4) % 4), altchars=b"-_", validate=True)
        decoded_text = decoded.decode("utf-8-sig").strip()
        if URI_ANY.search(decoded_text):
            return decoded_text
        converted = _structured_config_uris(decoded_text)
        if converted:
            return "\n".join(converted)
    except UnsupportedProxyConfig:
        raise
    except (ValueError, UnicodeDecodeError):
        pass
    return text


def _parse_xray_json_body(body: bytes, source: str = "источник") -> dict:
    """Parse a native Xray JSON feed without first converting its nodes to links."""
    text = body.decode("utf-8-sig", errors="strict").strip()

    def reject_constant(value):
        raise ValueError(f"Некорректное JSON-значение {value}")

    candidates = [text]
    compact = "".join(text.split())
    if compact and len(compact) <= 8 * 1024 * 1024:
        try:
            decoded = base64.b64decode(
                compact + "=" * ((4 - len(compact) % 4) % 4),
                altchars=b"-_", validate=True,
            ).decode("utf-8-sig", errors="strict").strip()
            if decoded != text:
                candidates.append(decoded)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            pass

    document = None
    for candidate in candidates:
        try:
            document = json.loads(candidate, parse_constant=reject_constant)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(document, dict) or not isinstance(document.get("outbounds"), list):
        raise ValueError(f"{source} не вернул Xray JSON с массивом outbounds")
    if not _xray_json_uris(json.dumps(document, ensure_ascii=False)):
        raise ValueError(f"{source} JSON не содержит поддерживаемых VPN-серверов")
    return document


def _set_xray_server_description(document: dict) -> None:
    """Change the one Happ serverDescription field only when it already exists."""
    meta = document.get("meta")
    if isinstance(meta, dict) and "serverDescription" in meta:
        meta["serverDescription"] = "SecureX"


def _xray_outbound_records(document: dict, outbound: dict) -> list[tuple[str, dict]]:
    """Return the source URI identity and an isolated JSON outbound per server."""
    protocol = str(outbound.get("protocol", "")).lower()
    siblings = [item for item in document.get("outbounds", [])
                if isinstance(item, dict) and str(item.get("protocol", "")).lower() == protocol]
    candidate = dict(document)
    # Keep the same profile and protocol cardinality so labels generated by the
    # existing Xray parser match the labels used by server reconciliation.
    candidate["outbounds"] = [outbound] + [
        {"protocol": protocol, "settings": {}} for item in siblings if item is not outbound
    ]
    uris = _xray_json_uris(json.dumps(candidate, ensure_ascii=False)) or []
    if not uris:
        return []

    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    clones = []
    if protocol in {"vless", "vmess"} and isinstance(settings.get("vnext"), list):
        for server in settings["vnext"]:
            if not isinstance(server, dict):
                continue
            users = server.get("users")
            if not isinstance(users, list):
                continue
            for user in users:
                clone = copy.deepcopy(outbound)
                cloned_server = copy.deepcopy(server)
                cloned_server["users"] = [copy.deepcopy(user)]
                clone["settings"]["vnext"] = [cloned_server]
                clones.append(clone)
    elif protocol in {"trojan", "ss", "shadowsocks"} and isinstance(settings.get("servers"), list):
        for server in settings["servers"]:
            clone = copy.deepcopy(outbound)
            clone["settings"]["servers"] = [copy.deepcopy(server)]
            clones.append(clone)
    if len(clones) != len(uris):
        # Most feeds have one server per outbound. Preserve an aggregate outbound
        # intact when its format cannot be safely split into individual nodes.
        clones = [copy.deepcopy(outbound)] if len(uris) == 1 else []
    return list(zip(uris, clones))


def merge_xray_json_bodies(primary_body: bytes, secondary_body: bytes | None = None) -> bytes:
    """Merge two native Xray JSON feeds by combining their outbound objects."""
    primary = _parse_xray_json_body(primary_body, "Основная подписка")
    secondary = (_parse_xray_json_body(secondary_body, "SAS") if secondary_body is not None else None)
    _set_xray_server_description(primary)

    seen: set[tuple[str, str]] = set()
    def add_source(document: dict, *, sas: bool) -> list[dict]:
        selected: list[dict] = []
        for outbound in document["outbounds"]:
            if not isinstance(outbound, dict):
                continue
            records = _xray_outbound_records(document, outbound)
            if not records:
                if not sas:
                    selected.append(copy.deepcopy(outbound))
                continue
            keep: list[dict] = []
            for uri, node in records:
                identity = parse_server_uri(uri)
                key = (identity["id"], identity["source_key"])
                if sas and is_excluded_sas_server(uri):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                keep.append(node)
            effective_tag = str(outbound.get("remarks") or outbound.get("tag") or "").strip()
            generic_tag = not effective_tag or bool(re.fullmatch(
                rf"(?:proxy|outbound|{re.escape(str(outbound.get('protocol', '')).lower())})(?:[-_ ]?\d+)?",
                effective_tag, flags=re.I))
            if len(keep) == len(records):
                if sas and generic_tag:
                    normalized = [node for _uri, node in records]
                    for index, ((uri, _original), node) in enumerate(zip(records, normalized)):
                        node["remarks"] = parse_server_uri(uri)["name"]
                        if len(normalized) > 1 and index > 0:
                            node["tag"] = f"{effective_tag or 'sas'}-securex-{index + 1}"
                    selected.extend(normalized)
                else:
                    selected.append(copy.deepcopy(outbound))
            else:
                # A partial aggregate can be represented as one outbound per
                # selected credential while retaining each original protocol
                # configuration and avoiding excluded or duplicate entries.
                if sas and generic_tag:
                    kept_keys = {(parse_server_uri(uri)["id"], parse_server_uri(uri)["source_key"])
                                 for uri, _node in records if _node in keep}
                    for index, (uri, node) in enumerate(records):
                        identity = parse_server_uri(uri)
                        if (identity["id"], identity["source_key"]) not in kept_keys:
                            continue
                        node["remarks"] = identity["name"]
                        if len(keep) > 1 and index > 0:
                            node["tag"] = f"{effective_tag or 'sas'}-securex-{index + 1}"
                        selected.append(node)
                    continue
                selected.extend(keep)
        return selected

    selected_primary = add_source(primary, sas=False)
    selected_sas = add_source(secondary, sas=True) if secondary is not None else []

    merged = copy.deepcopy(primary)
    merged["outbounds"] = selected_primary + selected_sas
    serialized = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    # Validate the merged root and its proxy entries before it can replace Drive.
    serialized_bytes = serialized.encode("utf-8")
    _parse_xray_json_body(serialized_bytes, "Объединённая подписка")
    return serialized_bytes


def _vmess_obj(uri: str) -> tuple[dict, str]:
    payload, _, fragment = uri[8:].partition("#")
    raw = base64.b64decode(payload + "=" * ((4 - len(payload) % 4) % 4), altchars=b"-_", validate=False)
    obj = json.loads(raw.decode("utf-8"))
    return obj, unquote(str(obj.get("ps") or fragment))


def _server_name_key(name: str) -> str:
    """Normalize the provider's stable display name for server reconciliation."""
    normalized = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(normalized.split()).casefold()


def parse_server_uri(uri: str) -> dict:
    value = uri.strip()
    scheme = value.split(":", 1)[0].lower()
    if scheme == "vmess":
        obj, name = _vmess_obj(value)
        identity_obj = dict(obj)
        identity_obj.pop("ps", None)
        identity = "vmess://" + json.dumps(identity_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fallback_name = f"{obj.get('add', 'server')}:{obj.get('port', '')}".rstrip(":")
    else:
        without_fragment, sep, fragment = value.partition("#")
        name = unquote(fragment) if sep else ""
        identity = without_fragment
        parsed = urlsplit(value)
        fallback_name = parsed.hostname or scheme
        try:
            if parsed.port:
                fallback_name += f":{parsed.port}"
        except ValueError:
            pass
    server_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    display_name = name or fallback_name
    return {"id": server_id, "uri": value, "name": display_name,
            "source_key": _server_name_key(display_name), "scheme": scheme}


def rename_server_uri(uri: str, name: str) -> str:
    scheme = uri.split(":", 1)[0].lower()
    if scheme == "vmess":
        obj, _ = _vmess_obj(uri)
        obj["ps"] = name
        encoded = base64.b64encode(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()).decode()
        return "vmess://" + encoded
    head = uri.split("#", 1)[0]
    return head + "#" + quote(name, safe="")


def _extract_uris(text: str) -> list[str]:
    # Upstream supports one URI per line. Ignore human-readable metadata lines.
    # Provider labels are not always percent-encoded: a fragment such as
    # `#Germany West` is valid input for clients even though it contains spaces.
    # Only allow those spaces after `#`; whitespace in the connection part
    # would make the URI ambiguous and is treated as malformed.
    converted = _structured_config_uris(text)
    if converted is not None:
        text = "\n".join(converted)
    uris = []
    seen = set()
    for line in text.splitlines():
        candidate = line.strip().lstrip("\ufeff")
        if not URI_LINE.match(candidate):
            continue
        connection, separator, _label = candidate.partition("#")
        if any(char.isspace() for char in connection):
            continue
        if not separator and any(char.isspace() for char in candidate):
            continue
        try:
            parsed = parse_server_uri(candidate)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        entry_key = (parsed["id"], parsed["source_key"])
        if entry_key not in seen:
            seen.add(entry_key)
            uris.append(candidate)
    return uris


def _is_html(body: bytes, content_type: str) -> bool:
    start = body.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return "text/html" in (content_type or "").lower() or start.startswith((b"<!doctype html", b"<html"))


def _invalid_payload(body: bytes, content_type: str) -> str | None:
    if len(body) > 8 * 1024 * 1024:
        return "Подписка превышает допустимый размер"
    if _is_html(body, content_type):
        return "Апстрим вернул HTML вместо списка серверов"
    compact = b"".join(body.split())
    candidates = [body]
    if compact:
        try:
            candidates.insert(0, base64.b64decode(compact + b"=" * ((4 - len(compact) % 4) % 4), altchars=b"-_", validate=True))
        except (ValueError, base64.binascii.Error):
            pass
    if any(any(marker in item.lower() for marker in DEVICE_MARKERS) for item in candidates):
        return "Апстрим сообщил о лимите устройств"
    try:
        decoded = _decode_subscription(body)
        uris = _extract_uris(decoded)
        if not uris:
            return "Апстрим не содержит поддерживаемых VPN-серверов"
        for uri in uris:
            parse_server_uri(uri)
    except UnsupportedProxyConfig as exc:
        return str(exc)
    except Exception:
        return "Апстрим вернул некорректную запись сервера"
    return None


def merge_subscription_bodies(primary_body: bytes, secondary_body: bytes,
                              primary_content_type: str = "",
                              secondary_content_type: str = "") -> bytes:
    """Return a plain-text union of supported server URIs from two feeds."""
    merged: list[str] = []
    seen: set[str] = set()
    for body, content_type, source in (
        (primary_body, primary_content_type, "основной подписки"),
        (secondary_body, secondary_content_type, "SAS"),
    ):
        invalid = _invalid_payload(body, content_type)
        if invalid:
            raise ValueError(f"Некорректный источник {source}: {invalid}")
        uris = _extract_uris(_decode_subscription(body))
        if not uris:
            raise ValueError(f"Источник {source} не содержит поддерживаемых VPN-серверов")
        if source == "SAS":
            uris = [uri for uri in uris if not is_excluded_sas_server(uri)]
        for uri in uris:
            if uri not in seen:
                seen.add(uri)
                merged.append(uri)
    return ("\n".join(merged) + "\n").encode("utf-8")


def is_excluded_sas_server(uri: str) -> bool:
    """Exclude every numbered or unnumbered LTE bypass node from the SAS feed."""
    try:
        name = _server_name_key(parse_server_uri(uri)["name"])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if "lte" not in name or not re.search(r"обход\s+глушилок", name):
        return False
    return True


class PanelState:
    _FEED_CACHE_FIELDS = frozenset({
        "upstream_body", "upstream_headers", "upstream_fetched_at", "upstream_success_at",
        "upstream_attempt_at", "upstream_warning", "sas_body", "sas_fetched_at",
        "sas_success_at", "sas_attempt_at", "sas_warning", "combined_body",
        "combined_format", "combined_fetched_at", "combined_success_at", "drive_push_last", "drive_push_error",
    })

    def __getattribute__(self, name):
        if name in PanelState._FEED_CACHE_FIELDS:
            values = object.__getattribute__(self, "__dict__").get("_feed_caches")
            if values is not None:
                env = "test" if _test_environment.get() else "main"
                return values[env][name]
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if name in PanelState._FEED_CACHE_FIELDS:
            values = object.__getattribute__(self, "__dict__").get("_feed_caches")
            if values is not None:
                env = "test" if _test_environment.get() else "main"
                values[env][name] = value
                return
        object.__setattr__(self, name, value)

    def __init__(self):
        defaults = {
            "upstream_body": None, "upstream_headers": {}, "upstream_fetched_at": 0.0,
            "upstream_success_at": None, "upstream_attempt_at": 0.0, "upstream_warning": None,
            "sas_body": None, "sas_fetched_at": 0.0, "sas_success_at": None,
            "sas_attempt_at": 0.0, "sas_warning": None, "combined_body": None,
            "combined_format": None, "combined_fetched_at": 0.0, "combined_success_at": None,
            "drive_push_last": 0.0, "drive_push_error": None,
        }
        self._feed_caches = {env: dict(defaults) for env in ("main", "test")}
        self.pool = None
        self.test_pool = None
        self.db_error = "База ещё не проверялась"
        self.test_db_error = "Тестовая база ещё не проверялась"
        self.db_last_attempt = 0.0
        self.test_db_last_attempt = 0.0
        self.upstream_client: httpx.AsyncClient | None = None
        self.test_geo_file_cache: dict[str, tuple[bytes, float]] = {}
        self.test_geo_file_lock = asyncio.Lock()
        self.upstream_lock = asyncio.Lock()
        self.login_fails: dict[str, tuple[int, float]] = {}
        self.happ_tasks: set[asyncio.Task] = set()
        self._main_drive_sync_task: asyncio.Task | None = None
        self._main_drive_sync_targets: set[int] = set()
        self._main_drive_sync_all = False
        self._drive_sync_reported_at: dict[int, float] = {}

    @property
    def db_configured(self):
        return bool(os.getenv("DATABASE_URL", "").strip())

    @property
    def admin_configured(self):
        return bool(os.getenv("ADMIN_USERNAME") and (os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD_HASH"))
                    and len(os.getenv("SESSION_SECRET", "")) >= 32)

    @property
    def sync_configured(self):
        return bool(os.getenv("SYNC_KEY", "").strip())

    @property
    def drive_push_configured(self):
        return bool(os.getenv("DRIVE_SYNC_URL", "").strip() and self.sync_configured)

    async def connect(self):
        if not self.db_configured:
            self.db_error = "DATABASE_URL не задан"
            self.test_db_error = "DATABASE_URL не задан"
            return
        if self.pool is not None:
            if self.test_pool is None and time.monotonic() - self.test_db_last_attempt >= 4:
                await self._connect_test_schema()
            return
        if time.monotonic() - self.db_last_attempt < 4:
            return
        self.db_last_attempt = time.monotonic()
        try:
            params = parse_database_url(os.getenv("DATABASE_URL", ""))
            self.pool = await asyncpg.create_pool(**params, min_size=1, max_size=3, timeout=5, command_timeout=8)
            async with self.pool.acquire() as conn:
                schema = open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read()
                await conn.execute(schema)
                await conn.execute("ALTER TABLE subscription_activation ADD COLUMN IF NOT EXISTS drive_generation BIGINT NOT NULL DEFAULT 0")
                await self.seed(conn, include_legacy=True)
            self.db_error = ""
            log.info("Admin database connected and schema initialized")
            await self._connect_test_schema()
        except Exception as exc:
            if self.pool is not None:
                await self.pool.close()
                self.pool = None
            self.db_error = DB_HINT
            # Error type only; driver messages can contain a hostname/user or connection details.
            log.warning("Admin database unavailable (%s). %s", type(exc).__name__, DB_HINT)

    async def _connect_test_schema(self):
        if self.test_pool is not None or self.pool is None:
            return
        self.test_db_last_attempt = time.monotonic()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS secure_x_test")
            params = parse_database_url(os.getenv("DATABASE_URL", ""))
            self.test_pool = await asyncpg.create_pool(
                **params, server_settings={"search_path": "secure_x_test"},
                min_size=1, max_size=2, timeout=5, command_timeout=8,
            )
            async with self.test_pool.acquire() as conn:
                schema = open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read()
                await conn.execute(schema)
                await conn.execute("""CREATE TABLE IF NOT EXISTS test_geo_state (
                    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    geo_device_limit INTEGER CHECK (geo_device_limit IS NULL OR geo_device_limit BETWEEN 1 AND 20),
                    geoip_downloaded_at TIMESTAMPTZ,
                    geosite_downloaded_at TIMESTAMPTZ,
                    geoip_requests INTEGER NOT NULL DEFAULT 0,
                    geosite_requests INTEGER NOT NULL DEFAULT 0,
                    download_requests INTEGER NOT NULL DEFAULT 0,
                    activated_at TIMESTAMPTZ
                )""")
                await conn.execute("""ALTER TABLE test_geo_state ADD COLUMN IF NOT EXISTS geo_device_limit INTEGER
                    CHECK (geo_device_limit IS NULL OR geo_device_limit BETWEEN 1 AND 20)""")
                await conn.execute("ALTER TABLE test_geo_state ADD COLUMN IF NOT EXISTS geoip_requests INTEGER NOT NULL DEFAULT 0")
                await conn.execute("ALTER TABLE test_geo_state ADD COLUMN IF NOT EXISTS geosite_requests INTEGER NOT NULL DEFAULT 0")
                await conn.execute("""CREATE TABLE IF NOT EXISTS test_subscription_activation (
                    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    token_nonce TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    browser_hash TEXT,
                    activated_at TIMESTAMPTZ,
                    drive_ready_at TIMESTAMPTZ,
                    encrypted_link TEXT,
                    encrypted_for_file_id TEXT,
                    encryption_error TEXT,
                    device_limit INTEGER NOT NULL DEFAULT 1 CHECK (device_limit BETWEEN 1 AND 20),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
                await conn.execute("""ALTER TABLE test_subscription_activation
                    ADD COLUMN IF NOT EXISTS device_limit INTEGER NOT NULL DEFAULT 1
                    CHECK (device_limit BETWEEN 1 AND 20)""")
                await conn.execute("ALTER TABLE test_subscription_activation ADD COLUMN IF NOT EXISTS drive_ready_at TIMESTAMPTZ")
                await conn.execute("""CREATE TABLE IF NOT EXISTS test_activation_devices (
                    user_id BIGINT NOT NULL REFERENCES test_subscription_activation(user_id) ON DELETE CASCADE,
                    device_hash TEXT NOT NULL,
                    subscription_token_hash TEXT,
                    encrypted_link TEXT,
                    platform TEXT NOT NULL DEFAULT 'unknown',
                    browser TEXT NOT NULL DEFAULT 'unknown',
                    language TEXT NOT NULL DEFAULT '',
                    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (user_id,device_hash)
                )""")
                await conn.execute("ALTER TABLE test_activation_devices ADD COLUMN IF NOT EXISTS subscription_token_hash TEXT")
                await conn.execute("ALTER TABLE test_activation_devices ADD COLUMN IF NOT EXISTS encrypted_link TEXT")
                # Migrate old test activations away from provider-managed limits.
                # This touches only the isolated test schema and makes no Happ API calls.
                await conn.execute("""UPDATE users SET device_limit=NULL,happ_install_code=NULL,limited_link=NULL,
                    device_limit_status='none',device_limit_error=NULL,updated_at=now()
                    WHERE id IN (SELECT user_id FROM test_subscription_activation)
                      AND (device_limit IS NOT NULL OR happ_install_code IS NOT NULL OR limited_link IS NOT NULL
                           OR device_limit_status<>'none')""")
                # Existing one-browser confirmations occupy one slot after the migration.
                await conn.execute("""INSERT INTO test_activation_devices(user_id,device_hash,platform,browser,registered_at)
                    SELECT user_id,browser_hash,'unknown','existing',COALESCE(activated_at,now())
                    FROM test_subscription_activation WHERE browser_hash IS NOT NULL
                    ON CONFLICT(user_id,device_hash) DO NOTHING""")
                await conn.execute("UPDATE test_subscription_activation SET browser_hash=NULL WHERE browser_hash IS NOT NULL")
                await self.seed(conn, include_legacy=False)
            self.test_db_error = ""
            log.info("Test panel database schema initialized")
        except Exception as exc:
            if self.test_pool is not None:
                await self.test_pool.close()
                self.test_pool = None
            self.test_db_error = "Не удалось инициализировать изолированную тестовую схему базы"
            log.warning("Test database unavailable (%s)", type(exc).__name__)

    async def seed(self, conn, *, include_legacy=True):
        # Retire former rotating-announcement settings from both database scopes.
        await conn.execute("DELETE FROM settings WHERE key=ANY($1::text[])",
                           ["announce_enabled", "announce_refresh_minutes", "quotes", "footers", "days"])
        await _release_expired_public_ids(conn)
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO NOTHING", key, json.dumps(value, ensure_ascii=False))
        if include_legacy:
            await conn.execute("""INSERT INTO users(name,slug,drive_file_id) VALUES('Основная (legacy)','legacy', $1)
                ON CONFLICT (slug) DO NOTHING""", LEGACY_DRIVE_FILE_ID)

    async def close(self):
        for task in tuple(self.happ_tasks):
            task.cancel()
        if self.happ_tasks:
            await asyncio.gather(*self.happ_tasks, return_exceptions=True)
        self.happ_tasks.clear()
        if self.upstream_client is not None:
            await self.upstream_client.aclose()
            self.upstream_client = None
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        if self.test_pool is not None:
            await self.test_pool.close()
            self.test_pool = None

    async def db(self, required=True):
        await self.connect()
        test_mode = _test_environment.get()
        pool = self.test_pool if test_mode else self.pool
        error = self.test_db_error if test_mode else self.db_error
        if pool is None and required:
            raise HTTPException(503, error or DB_HINT)
        return pool

    async def push_drive_sync(self, user_ids: list[int] | None = None) -> dict:
        """Ask the authenticated Apps Script web app to update Drive immediately."""
        attempt_started = time.time()
        test_mode = _test_environment.get()
        target = os.getenv("DRIVE_SYNC_URL", "").strip()
        key = os.getenv("SYNC_KEY", "").strip()
        if not target or not key:
            message = "Мгновенная доставка не настроена: добавьте DRIVE_SYNC_URL в Render"
            self.drive_push_error = message
            return {"ok": False, "pending": True, "message": message}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(150.0, connect=12.0)) as client:
                response = await client.post(target, headers={"X-Sync-Key": key, "Accept": "application/json"},
                                             json={"action": "sync", "user_ids": user_ids, "sync_key": key,
                                                   "environment": "test" if test_mode else "main"})
            if response.status_code >= 400:
                raise ValueError("Apps Script отклонил запрос")
            try:
                result = response.json()
            except Exception as exc:
                raise ValueError("Apps Script вернул некорректный ответ") from exc
            if not isinstance(result, dict):
                raise ValueError("Apps Script не подтвердил доставку")
            failed = int(result.get("failed", 0) or 0)
            processed = int(result.get("processed", 0) or 0)
            if result.get("pending") and result.get("ok"):
                # A lock-busy acknowledgement means only that Apps Script
                # queued the work. In particular, processed=0 is not a
                # successful Drive write and must not clear an existing error.
                if test_mode:
                    self.drive_push_error = None
                elif processed > 0:
                    self.drive_push_last = max(self.drive_push_last or 0.0, time.time())
                return {"ok": True, "pending": True,
                        "message": "Изменения приняты; Google Drive обновится при ближайшей автоматической синхронизации.",
                        "processed": processed, "failed": 0}
            if failed:
                detail = _safe_sync_error(result.get("error"))
                self.drive_push_error = f"Apps Script обработал {processed} записей, ошибок: {failed}"
                if detail:
                    self.drive_push_error += f" ({detail})"
                return {"ok": False, "pending": bool(result.get("pending")), "message": self.drive_push_error,
                        "processed": processed, "failed": failed}
            if not result.get("ok"):
                raise ValueError(_safe_sync_error(result.get("error")) or "Apps Script не подтвердил доставку")
            if not test_mode:
                self.drive_push_last = max(self.drive_push_last or 0.0, time.time())
            else:
                self.drive_push_last = time.time()
                self.drive_push_error = None
            return {"ok": True, "pending": False,
                    "message": self.drive_push_error or "Изменения сразу отправлены в Google Drive",
                    "processed": processed, "failed": 0}
        except Exception as exc:
            safe_detail = _safe_sync_error(str(exc))
            if isinstance(exc, httpx.TimeoutException) and not test_mode:
                if user_ids and all(self._drive_sync_reported_at.get(int(user_id), 0.0) >= attempt_started
                                    for user_id in user_ids):
                    # GAS can finish writing and reporting before its HTTP
                    # response reaches Render. Trust the per-user reports.
                    return {"ok": True, "pending": False,
                            "message": "Google Drive обновлён; Apps Script не успел вернуть HTTP-ответ.",
                            "processed": len(user_ids), "failed": 0}
                if self.drive_push_last is not None and self.drive_push_last >= attempt_started:
                    # Reports arrived during this request, so a late HTTP
                    # timeout alone is not evidence that Drive failed.
                    return {"ok": True, "pending": True,
                            "message": "Отчёты синхронизации получены; ожидается завершение запроса Apps Script."}
                self.drive_push_error = "Google Drive обновляется дольше обычного; следующая синхронизация повторит попытку"
                log.info("Immediate Drive update timed out; scheduled sync remains the recovery path")
                return {"ok": True, "pending": True, "message": self.drive_push_error}
            self.drive_push_error = "Не удалось сразу обновить Google Drive; автоматическая попытка повторится позже"
            log.warning("Immediate Drive update failed (%s%s)", type(exc).__name__,
                        f": {safe_detail}" if safe_detail else "")
            return {"ok": False, "pending": True, "message": self.drive_push_error}

    async def request_drive_sync(self, user_ids: list[int] | None = None) -> dict:
        """Queue primary Drive delivery so admin requests never wait on Apps Script."""
        if _test_environment.get():
            # Preserve the isolated test workflow and its existing response semantics.
            return await self.push_drive_sync(user_ids)
        if not os.getenv("DRIVE_SYNC_URL", "").strip() or not os.getenv("SYNC_KEY", "").strip():
            return await self.push_drive_sync(user_ids)

        if user_ids is None:
            self._main_drive_sync_all = True
            self._main_drive_sync_targets.clear()
        elif not self._main_drive_sync_all:
            self._main_drive_sync_targets.update(int(value) for value in user_ids)

        if self._main_drive_sync_task is None or self._main_drive_sync_task.done():
            task = asyncio.create_task(self._drain_main_drive_sync_queue())
            self._main_drive_sync_task = task
            self.happ_tasks.add(task)
            task.add_done_callback(self.happ_tasks.discard)
        return {"ok": True, "pending": True,
                "message": "Синхронизация Google Drive поставлена в очередь и выполняется в фоне."}

    async def _drain_main_drive_sync_queue(self) -> None:
        try:
            while self._main_drive_sync_all or self._main_drive_sync_targets:
                if self._main_drive_sync_all:
                    user_ids = None
                    self._main_drive_sync_all = False
                    self._main_drive_sync_targets.clear()
                else:
                    user_ids = sorted(self._main_drive_sync_targets)
                    self._main_drive_sync_targets.clear()
                context = _test_environment.set(False)
                try:
                    result = await self.push_drive_sync(user_ids)
                    if not result.get("ok"):
                        log.info("Queued Drive sync needs retry: pending=%s", bool(result.get("pending")))
                finally:
                    _test_environment.reset(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Queued Drive sync worker failed (%s)", type(exc).__name__)
        finally:
            self._main_drive_sync_task = None
            # Do not lose a request that arrived as the previous worker ended.
            if self._main_drive_sync_all or self._main_drive_sync_targets:
                task = asyncio.create_task(self._drain_main_drive_sync_queue())
                self._main_drive_sync_task = task
                self.happ_tasks.add(task)
                task.add_done_callback(self.happ_tasks.discard)

    def _upstream_headers_for_panel(self) -> dict[str, str]:
        headers = {}
        names = {"UPSTREAM_USER_AGENT": "User-Agent", "UPSTREAM_X_HWID": "X-HWID",
                 "UPSTREAM_X_DEVICE_INFO": "X-Device-Info", "UPSTREAM_X_DEVICE_MODEL": "X-Device-Model",
                 "UPSTREAM_X_DEVICE_OS": "X-Device-OS", "UPSTREAM_X_VER_OS": "X-Ver-OS",
                 "UPSTREAM_X_APP_VERSION": "X-App-Version"}
        for env_name, header in names.items():
            value = os.getenv(env_name, "").strip()
            if value:
                headers[header] = value
        headers.setdefault("User-Agent", PANEL_UPSTREAM_DEFAULT_USER_AGENT)
        return headers

    async def _fetch_primary_locked(self, url: str, headers: dict[str, str], force: bool):
        now = time.monotonic()
        fresh = self.upstream_body is not None and now - self.upstream_fetched_at < PANEL_CACHE_TTL_SECONDS
        if fresh and not force:
            return self.upstream_body, False
        self.upstream_attempt_at = time.time()
        try:
            response = await self.upstream_client.get(url, headers=headers)
            if response.status_code >= 400:
                raise ValueError(f"Апстрим ответил HTTP {response.status_code}")
            invalid = _invalid_payload(response.content, response.headers.get("content-type", ""))
            if invalid:
                raise ValueError(invalid)
            self.upstream_body = response.content
            self.upstream_headers = {k.lower(): v for k, v in response.headers.items()}
            self.upstream_fetched_at = time.monotonic()
            self.upstream_success_at = time.time()
            self.upstream_warning = None
            log.info("Panel upstream cache updated: bytes=%d; servers=%d", len(response.content), len(_extract_uris(_decode_subscription(response.content))))
            return self.upstream_body, False
        except Exception as exc:
            self.upstream_warning = "Не удалось получить актуальную подписку от основного апстрима"
            if isinstance(exc, ValueError):
                self.upstream_warning = str(exc)
            log.warning("Panel upstream request failed (%s)", type(exc).__name__)
            if self.upstream_body is not None:
                return self.upstream_body, True
            raise HTTPException(502, self.upstream_warning) from exc

    async def _fetch_sas_locked(self, url: str, headers: dict[str, str], force: bool):
        now = time.monotonic()
        fresh = self.sas_body is not None and now - self.sas_fetched_at < PANEL_CACHE_TTL_SECONDS
        if fresh and not force:
            return self.sas_body, False
        self.sas_attempt_at = time.time()
        try:
            response = await self.upstream_client.get(url, headers=headers)
            if response.status_code >= 400:
                raise ValueError(f"SAS ответил HTTP {response.status_code}")
            invalid = _invalid_payload(response.content, response.headers.get("content-type", ""))
            if invalid:
                raise ValueError(invalid)
            self.sas_body = response.content
            self.sas_fetched_at = time.monotonic()
            self.sas_success_at = time.time()
            self.sas_warning = None
            log.info("SAS subscription cache updated: bytes=%d; servers=%d", len(response.content), len(_extract_uris(_decode_subscription(response.content))))
            return self.sas_body, False
        except Exception as exc:
            self.sas_warning = "Не удалось обновить подписку SAS; используется последняя удачная копия"
            log.warning("SAS subscription refresh failed (%s)", type(exc).__name__)
            if self.sas_body is not None:
                return self.sas_body, True
            self.sas_warning = "Не удалось получить подписку SAS; объединённый профиль пока не готов"
            raise HTTPException(502, self.sas_warning) from exc

    async def _profile_output_format(self) -> str:
        try:
            pool = await self.db(required=False)
            if pool is not None:
                async with pool.acquire() as conn:
                    value = await conn.fetchval("SELECT value FROM settings WHERE key='profile_format'")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (TypeError, json.JSONDecodeError):
                        pass
                if value in {"url", "json"}:
                    return value
        except Exception as exc:
            log.debug("Could not read profile format setting (%s)", type(exc).__name__)
        return "url"

    async def fetch_upstream(self, force=False, force_sas=False, allow_sas_failure=False):
        output_format = await self._profile_output_format()

        def primary_json_or_cache(*, allow_combined_fallback: bool = False) -> tuple[bytes, bool]:
            try:
                return merge_xray_json_bodies(primary_body), False
            except ValueError as exc:
                self.upstream_warning = "Основной источник не вернул корректный Xray JSON"
                if (allow_combined_fallback and self.combined_body is not None
                        and self.combined_format == output_format):
                    return self.combined_body, True
                raise HTTPException(502, self.upstream_warning) from exc

        async with self.upstream_lock:
            primary_url = os.getenv("SUBSCRIPTION_URL", "").strip()
            if self.upstream_client is None:
                self.upstream_client = httpx.AsyncClient(follow_redirects=True, timeout=UPSTREAM_TIMEOUT_SECONDS)
            headers = self._upstream_headers_for_panel()
            if primary_url:
                primary_body, primary_stale = await self._fetch_primary_locked(primary_url, headers, force)
            elif self.upstream_body is not None:
                self.upstream_warning = "SUBSCRIPTION_URL не задан; используется последняя удачная копия"
                primary_body, primary_stale = self.upstream_body, True
            else:
                self.upstream_warning = "SUBSCRIPTION_URL не задан"
                raise HTTPException(503, self.upstream_warning)

            sas_url = os.getenv("SAS_SUBSCRIPTION_URL", "").strip()
            sas_stale = False
            if sas_url:
                try:
                    sas_body, sas_stale = await self._fetch_sas_locked(sas_url, headers, force or force_sas)
                except HTTPException:
                    if not allow_sas_failure:
                        raise
                    sas_body = None
                    sas_stale = True
                if sas_body is None:
                    # A subscriber that opted out of SAS can keep receiving the
                    # primary profile during a cold SAS outage.
                    if output_format == "json":
                        combined_body, primary_merge_stale = primary_json_or_cache()
                        primary_stale = primary_stale or primary_merge_stale
                    else:
                        combined_body = primary_body
                else:
                    try:
                        if output_format == "json":
                            combined_body = merge_xray_json_bodies(primary_body, sas_body)
                        else:
                            combined_body = merge_subscription_bodies(
                                primary_body,
                                sas_body,
                                self.upstream_headers.get("content-type", ""),
                                "text/plain",
                            )
                    except ValueError as exc:
                        self.sas_warning = "Не удалось сформировать объединённый профиль SAS"
                        log.warning("Combined subscription rejected (%s)", type(exc).__name__)
                        if self.combined_body is None or self.combined_format != output_format:
                            raise HTTPException(502, self.sas_warning) from exc
                        combined_body = self.combined_body
                        sas_stale = True
            else:
                if output_format == "json":
                    combined_body, primary_merge_stale = primary_json_or_cache(allow_combined_fallback=True)
                    primary_stale = primary_stale or primary_merge_stale
                else:
                    combined_body = primary_body
                self.sas_warning = None

            self.combined_body = combined_body
            self.combined_format = output_format
            self.combined_fetched_at = time.monotonic()
            self.combined_success_at = time.time()
            warnings = [warning for warning in (self.upstream_warning, self.sas_warning) if warning]
            self.upstream_warning = " · ".join(warnings) or None
            return combined_body, self.upstream_headers, primary_stale or sas_stale


state = PanelState()


async def startup(app: FastAPI):
    # Initialize the schema in the background so an unreachable database cannot
    # delay or prevent the public relay from starting.
    asyncio.create_task(state.connect())


async def shutdown(app: FastAPI):
    await state.close()


def _password_ok(password: str) -> bool:
    encoded_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
    if encoded_hash:
        try:
            algorithm, salt, digest = encoded_hash.split("$", 2)
            if algorithm == "pbkdf2_sha256":
                expected = base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4))
                actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000)
                return hmac.compare_digest(actual, expected)
            if algorithm == "scrypt":
                expected = base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4))
                actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=len(expected))
                return hmac.compare_digest(actual, expected)
        except Exception:
            return False
        return False
    expected = os.getenv("ADMIN_PASSWORD", "")
    return hmac.compare_digest(password.encode(), expected.encode())


def _sign_session(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(os.getenv("SESSION_SECRET", "").encode(), raw.encode(), hashlib.sha256).digest()
    return raw + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _session(request: Request):
    test_mode = request.url.path == "/test" or request.url.path.startswith("/test/")
    cookie_name = "test_admin_session" if test_mode else "admin_session"
    token = request.cookies.get(cookie_name, "")
    try:
        raw, sig = token.split(".", 1)
        actual = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        expected = hmac.new(os.getenv("SESSION_SECRET", "").encode(), raw.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if (payload.get("exp", 0) < time.time() or payload.get("u") != os.getenv("ADMIN_USERNAME")
                or payload.get("environment", "main") != ("test" if test_mode else "main")):
            return None
        return payload
    except Exception:
        return None


def _auth(request: Request, csrf=False):
    if not state.admin_configured:
        raise HTTPException(503, "Админ-панель отключена: задайте ADMIN_USERNAME, пароль и SESSION_SECRET длиной не менее 32 символов")
    payload = _session(request)
    if not payload:
        raise HTTPException(401, "Требуется вход")
    if csrf and not hmac.compare_digest(str(payload.get("c", "")), request.headers.get("x-csrf-token", "")):
        raise HTTPException(403, "Проверка запроса не пройдена")
    return payload


class LoginBody(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=1024)


class UserBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    enabled: bool = True
    expires_at: datetime | None = None
    title_override: str | None = Field(default=None, max_length=200)
    traffic_limit_bytes: int | None = Field(default=None, ge=1_000_000, le=1_000_000_000_000)
    hide_traffic_usage: bool | None = None
    include_sas: bool | None = None
    device_limit: StrictInt | None = Field(default=None, ge=happ_limit.MIN_LIMIT, le=happ_limit.MAX_LIMIT)
    geo_device_limit: StrictInt | None = Field(default=None, ge=1, le=20)
    activation_enabled: bool | None = None
    activation_limit: StrictInt | None = Field(default=None, ge=1, le=20)
    test_activation_enabled: bool | None = None
    test_activation_limit: StrictInt | None = Field(default=None, ge=1, le=20)


class TestActivationLimitBody(BaseModel):
    limit: StrictInt = Field(ge=1, le=20)


class DeviceLimitActionBody(BaseModel):
    action: Literal["retry", "rebuild"] = "retry"


class ServerBody(BaseModel):
    custom_name: str | None = Field(default=None, max_length=200)
    hidden: bool = False
    sort_order: int = Field(default=0, ge=-100000, le=100000)


class UserServerBody(BaseModel):
    hidden: bool | None = None
    custom_name: str | None = Field(default=None, max_length=200)


class ServerBatchItem(BaseModel):
    server_id: str = Field(min_length=1, max_length=200)
    custom_name: str | None = Field(default=None, max_length=200)
    hidden: bool
    sort_order: int = Field(ge=-100000, le=100000)


class ServerBatchBody(BaseModel):
    servers: list[ServerBatchItem] = Field(min_length=1, max_length=500)


class UserServerBatchItem(BaseModel):
    server_id: str = Field(min_length=1, max_length=200)
    hidden: bool | None = None
    custom_name: str | None = Field(default=None, max_length=200)


class UserServerBatchBody(BaseModel):
    servers: list[UserServerBatchItem] = Field(min_length=1, max_length=500)


class SettingsBody(BaseModel):
    values: dict


def _expiry_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _serialize(row):
    if row is None:
        return None
    result = dict(row)
    # The install code is needed only for server-side rebuilds. Never expose it
    # through the generic admin serializer (the encrypted link is user-facing).
    result.pop("happ_install_code", None)
    return result


async def _settings(conn) -> dict:
    rows = await conn.fetch("SELECT key,value FROM settings")
    values = dict(DEFAULT_SETTINGS)
    for row in rows:
        v = row["value"]
        if isinstance(v, str):
            try: v = json.loads(v)
            except Exception: pass
        values[row["key"]] = v
    return values


def _russian_day_word(value: int) -> str:
    value = abs(int(value))
    last_two = value % 100
    if 11 <= last_two <= 14:
        return 'дней'
    last = value % 10
    if last == 1:
        return 'день'
    if 2 <= last <= 4:
        return 'дня'
    return 'дней'


def _subscription_announcement(expires_at: datetime | None, public_id: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    expires_at = _expiry_utc(expires_at)
    if expires_at is None:
        remaining = 'Срок действия: без ограничения'
    else:
        days = max(0, math.ceil((expires_at - now).total_seconds() / 86400))
        remaining = f'Осталось: {days} {_russian_day_word(days)}'
    return '\n'.join((remaining, f'ID: {public_id}', 'Спасибо за покупку!', 'С вами команда SecureX.'))


async def _release_expired_public_ids(conn) -> int:
    """Forget public IDs as soon as a profile is expired, disabled, or revoked."""
    result = await conn.execute("""UPDATE users SET public_id=NULL
        WHERE public_id IS NOT NULL AND
          (status<>'active' OR enabled=FALSE OR (expires_at IS NOT NULL AND expires_at<=now()))""")
    try:
        return int(result.rsplit(' ', 1)[-1])
    except (AttributeError, ValueError):
        return 0


async def _ensure_public_id(conn, user_id: int) -> str | None:
    """Allocate a collision-checked, six-character ID for a live profile."""
    async with conn.transaction():
        user = await conn.fetchrow("""SELECT id,enabled,status,expires_at,public_id
            FROM users WHERE id=$1 FOR UPDATE""", user_id)
        if not user:
            return None
        expires_at = _expiry_utc(user['expires_at'])
        active = (bool(user['enabled']) and user['status'] == 'active'
                  and (expires_at is None or expires_at > datetime.now(timezone.utc)))
        if not active:
            if user['public_id'] is not None:
                await conn.execute('UPDATE users SET public_id=NULL WHERE id=$1', user_id)
            return None
        if user['public_id']:
            return str(user['public_id'])

        for _ in range(PUBLIC_ID_ALLOCATION_ATTEMPTS):
            candidate = ''.join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(PUBLIC_ID_LENGTH))
            try:
                # The nested transaction gives each collision its own savepoint;
                # the unique index is the final authority under concurrent syncs.
                async with conn.transaction():
                    allocated = await conn.fetchval("""UPDATE users SET public_id=$2
                        WHERE id=$1 AND public_id IS NULL
                        RETURNING public_id""", user_id, candidate)
            except asyncpg.UniqueViolationError:
                continue
            if allocated:
                return str(allocated)
        raise HTTPException(503, 'Не удалось выделить уникальный ID подписки')

def _userinfo(headers: dict, expire_override: int | None = None) -> str:
    value = headers.get("subscription-userinfo", "")
    parts = {}
    for token in value.split(";"):
        if "=" in token:
            key, val = token.strip().split("=", 1)
            if key.strip().lower() in {"upload", "download", "total", "expire"} and val.strip().isdigit():
                parts[key.strip().lower()] = val.strip()
    if expire_override is not None:
        parts["expire"] = str(expire_override)
    return "; ".join(f"{k}={parts.get(k, '0')}" for k in ("upload", "download", "total", "expire"))


def _server_row_value(row, key, default=None):
    try:
        return row.get(key, default)
    except AttributeError:
        try:
            return row[key]
        except (KeyError, TypeError):
            return default


def _server_rank(row, override_counts: dict[str, int]) -> tuple:
    server_id = str(_server_row_value(row, "id", ""))
    last_seen = _server_row_value(row, "last_seen_at")
    try:
        last_seen_rank = last_seen.timestamp()
    except (AttributeError, OSError, OverflowError):
        last_seen_rank = 0
    return (
        override_counts.get(server_id, 0),
        bool(_server_row_value(row, "custom_name")),
        bool(_server_row_value(row, "hidden", False)),
        last_seen_rank,
    )


def _plan_server_reconciliation(parsed: list[dict], existing: list[dict],
                                override_counts: dict[str, int] | None = None) -> tuple[list[dict], dict[str, list[str]]]:
    """Keep stored IDs and overrides when a provider changes a URI but not its name.

    Exact URI identities are matched first. Remaining entries match by normalized
    provider name. When a name is unique in the incoming snapshot, historical rows
    with that same name are treated as old configurations of the same server.
    """
    override_counts = override_counts or {}
    rows = [dict(row) for row in existing]
    by_id = {str(row["id"]): row for row in rows if row.get("id") is not None}
    assigned: dict[int, str] = {}
    reserved_ids: set[str] = set()

    # An unchanged server configuration is the strongest identity signal, even if
    # its provider label was edited.
    for index, server in enumerate(parsed):
        current_id = str(server["id"])
        if current_id in by_id and current_id not in reserved_ids:
            assigned[index] = current_id
            reserved_ids.add(current_id)

    incoming_name_counts: dict[tuple[str, str], int] = {}
    for server in parsed:
        key = server.get("source_key") or _server_name_key(server.get("name", ""))
        scope = (str(server.get("source", "primary")), key)
        incoming_name_counts[scope] = incoming_name_counts.get(scope, 0) + 1

    def row_key(row: dict) -> str:
        return (_server_row_value(row, "source_key") or
                _server_name_key(_server_row_value(row, "original_name", "")))

    def row_belongs_to_source(row: dict, source: str) -> bool:
        # `unknown` rows are from databases created before source labels existed.
        # Reconcile them once by name, then persist their source on the upsert.
        return _server_row_value(row, "source", "unknown") in {source, "unknown", None}

    for index, server in enumerate(parsed):
        if index in assigned:
            continue
        key = server.get("source_key") or _server_name_key(server.get("name", ""))
        source = str(server.get("source", "primary"))
        candidates = [row for row in rows
                      if str(row.get("id")) not in reserved_ids and row_key(row) == key
                      and row_belongs_to_source(row, source)]
        if candidates:
            candidates.sort(key=lambda row: _server_rank(row, override_counts), reverse=True)
            selected_id = str(candidates[0]["id"])
            assigned[index] = selected_id
            reserved_ids.add(selected_id)

    # Don't merge ambiguous same-name servers. For a single current server with
    # that provider name, merge its accumulated overrides from earlier URI hashes.
    assigned_name: dict[str, set[str]] = {}
    for index, server in enumerate(parsed):
        key = server.get("source_key") or _server_name_key(server.get("name", ""))
        scope = (str(server.get("source", "primary")), key)
        assigned_name.setdefault(scope, set()).add(assigned.get(index, str(server["id"])))

    merge_ids: dict[str, list[str]] = {}
    reconciled = []
    for index, server in enumerate(parsed):
        item = dict(server)
        target_id = assigned.get(index, str(server["id"]))
        item["id"] = target_id
        reconciled.append(item)
        key = item.get("source_key") or _server_name_key(item.get("name", ""))
        source = str(item.get("source", "primary"))
        scope = (source, key)
        if incoming_name_counts.get(scope) != 1:
            continue
        protected = assigned_name.get(scope, set())
        matching_ids = [str(row["id"]) for row in rows
                        if row_key(row) == key and row_belongs_to_source(row, source) and str(row["id"]) not in
                        {str(other["id"]) for other in rows
                         if str(other["id"]) in reserved_ids and
                         (row_key(other) != key or not row_belongs_to_source(other, source))}]
        # Never fold a row which is currently serving a different provider name
        # just because its old label happens to match this server's name.
        matching_ids = [value for value in matching_ids
                        if value == target_id or value not in reserved_ids or value in protected]
        if len(set(matching_ids)) > 1:
            merge_ids[target_id] = sorted(set(matching_ids))
    return reconciled, merge_ids


async def _sync_servers(conn, body: bytes, *, primary_body: bytes | None = None,
                        sas_body: bytes | None = None):
    text = _decode_subscription(body)
    uris = _extract_uris(text)
    if not uris:
        raise HTTPException(502, "В списке апстрима нет VPN-серверов")
    parsed = [parse_server_uri(uri) for uri in uris]
    primary_entries: set[tuple[str, str]] = set()
    sas_entries: set[tuple[str, str]] = set()
    if primary_body is not None:
        primary_entries = {(server["id"], server["source_key"]) for server in
                           (parse_server_uri(uri) for uri in
                            _extract_uris(_decode_subscription(primary_body)))}
    if sas_body is not None:
        sas_entries = {(server["id"], server["source_key"]) for server in
                       (parse_server_uri(uri) for uri in
                        _extract_uris(_decode_subscription(sas_body))
                        if not is_excluded_sas_server(uri))}
    for server in parsed:
        entry = (server["id"], server["source_key"])
        server["source"] = ("sas" if entry in sas_entries and entry not in primary_entries
                            else "primary")

    # A provider can publish several named entries that share the same
    # connection URI. Keep each label as a separate row/profile item.
    identity_groups: dict[str, list[dict]] = {}
    for server in parsed:
        identity_groups.setdefault(server["id"], []).append(server)
    for identity, aliases in identity_groups.items():
        if len(aliases) < 2:
            continue
        for server in aliases:
            alias_key = server.get("source_key") or _server_name_key(server.get("name", ""))
            server["id"] = hashlib.sha256(
                f"provider-alias\0{identity}\0{alias_key}".encode("utf-8")
            ).hexdigest()[:24]

    async with conn.transaction():
        existing = await conn.fetch("""SELECT id,original_name,source_key,source,custom_name,hidden,sort_order,
            provider_order,present_upstream,first_seen_at,last_seen_at FROM servers""")
        counts = await conn.fetch("SELECT server_id,COUNT(*) AS override_count FROM user_servers GROUP BY server_id")
        override_counts = {str(row["server_id"]): int(row["override_count"]) for row in counts}
        parsed, merge_ids = _plan_server_reconciliation(parsed, existing, override_counts)
        existing_by_id = {str(row["id"]): row for row in existing}
        next_sort_order = max(
            (int(_server_row_value(row, "sort_order", -1)) for row in existing),
            default=-1,
        ) + 1
        source_positions: dict[str, int] = {}
        provider_order_by_id: dict[str, int] = {}
        reset_sources: set[str] = set()
        incoming_ids = {str(server["id"]) for server in parsed}
        for server in parsed:
            source = "sas" if server.get("source") == "sas" else "primary"
            position = source_positions.get(source, 0)
            source_positions[source] = position + 1
            server_id = str(server["id"])
            provider_order_by_id[server_id] = position
            old = existing_by_id.get(server_id)
            if old is None or not bool(_server_row_value(old, "present_upstream", False)):
                reset_sources.add(source)
            elif _server_row_value(old, "source", "unknown") not in {source, "unknown", None}:
                reset_sources.add(source)
                reset_sources.add("sas" if _server_row_value(old, "source") == "sas" else "primary")
            elif int(_server_row_value(old, "provider_order", 0) or 0) != position:
                reset_sources.add(source)
        for old in existing:
            if bool(_server_row_value(old, "present_upstream", False)) and str(old["id"]) not in incoming_ids:
                reset_sources.add("sas" if _server_row_value(old, "source") == "sas" else "primary")

        await conn.execute("UPDATE servers SET present_upstream=FALSE")
        for server in parsed:
            server_id = str(server["id"])
            provider_order = provider_order_by_id[server_id]
            source = "sas" if server.get("source") == "sas" else "primary"
            existing_row = existing_by_id.get(server_id)
            if existing_row is None:
                sort_order = provider_order if source in reset_sources else next_sort_order
                if source not in reset_sources:
                    next_sort_order += 1
            elif source in reset_sources:
                sort_order = provider_order
            else:
                sort_order = _server_row_value(existing_row, "sort_order", provider_order)
                if sort_order is None:
                    sort_order = provider_order
            old_ids = merge_ids.get(server["id"], [])
            if len(old_ids) > 1:
                await conn.execute("""UPDATE servers AS target SET
                    custom_name=COALESCE(target.custom_name,(
                        SELECT old.custom_name FROM servers AS old WHERE old.id=ANY($2::text[])
                          AND old.id<>target.id AND old.custom_name IS NOT NULL
                        ORDER BY old.last_seen_at DESC LIMIT 1)),
                    hidden=target.hidden OR EXISTS(SELECT 1 FROM servers AS old
                        WHERE old.id=ANY($2::text[]) AND old.id<>target.id AND old.hidden=TRUE)
                    WHERE target.id=$1""", server["id"], old_ids)
                await conn.execute("""INSERT INTO user_servers(user_id,server_id,hidden,custom_name)
                    SELECT DISTINCT ON (us.user_id) us.user_id,$1,us.hidden,us.custom_name
                    FROM user_servers AS us JOIN servers AS old ON old.id=us.server_id
                    WHERE us.server_id=ANY($2::text[])
                    ORDER BY us.user_id,(us.server_id=$1) DESC,
                        (us.hidden IS NOT NULL OR us.custom_name IS NOT NULL) DESC,old.last_seen_at DESC
                    ON CONFLICT(user_id,server_id) DO UPDATE SET
                        hidden=COALESCE(user_servers.hidden,EXCLUDED.hidden),
                        custom_name=COALESCE(user_servers.custom_name,EXCLUDED.custom_name)""",
                    server["id"], old_ids)
                # After overrides have been copied, discard obsolete URI-hash rows
                # for this stable provider name. This prevents old aliases from
                # resurfacing if an admin later clears or changes a current name.
                await conn.execute("DELETE FROM servers WHERE id=ANY($2::text[]) AND id<>$1",
                                   server["id"], old_ids)
            await conn.execute("""INSERT INTO servers(id,original_name,source_key,source,sort_order,provider_order,
                present_upstream,last_seen_at) VALUES($1,$2,$3,$4,$5,$6,TRUE,now())
                ON CONFLICT(id) DO UPDATE SET original_name=EXCLUDED.original_name,
                source_key=EXCLUDED.source_key,source=EXCLUDED.source,present_upstream=TRUE,
                last_seen_at=now(),provider_order=EXCLUDED.provider_order""",
                server["id"], server["name"], server["source_key"], server["source"], sort_order,
                provider_order_by_id[server_id])
        if reset_sources:
            await _reset_server_order(conn, parsed, sources=reset_sources)
        # Remove long-gone provider entries and their personal overrides only
        # after a grace period, allowing temporary provider omissions to recover.
        await conn.execute("""DELETE FROM servers WHERE present_upstream=FALSE
            AND last_seen_at < now() - ($1::integer * INTERVAL '1 day')""", SERVER_STALE_RETENTION_DAYS)
    return parsed


async def _reset_server_order(conn, servers: list[dict], sources: set[str] | None = None) -> None:
    """Restore each provider's incoming order without changing server settings."""
    groups = {
        "primary": list(dict.fromkeys(str(server["id"]) for server in servers
                                       if server.get("source") != "sas")),
        "sas": list(dict.fromkeys(str(server["id"]) for server in servers
                                  if server.get("source") == "sas")),
    }
    async with conn.transaction():
        for source, ids in groups.items():
            if sources is not None and source not in sources:
                continue
            if not ids:
                continue
            await conn.execute("""UPDATE servers AS server
                SET sort_order=ordered.ordinality::INTEGER-1
                FROM unnest($1::text[]) WITH ORDINALITY AS ordered(id, ordinality)
                WHERE server.id=ordered.id AND server.present_upstream=TRUE""", ids)


def _test_geo_signature(user_id: int) -> str:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "Тестовый доступ к геофайлам не настроен")
    return hmac.new(secret.encode(), f"secure-x-test-geo:{user_id}".encode(), hashlib.sha256).hexdigest()


def _test_activation_secret() -> bytes:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "Активация не настроена: SESSION_SECRET должен содержать не менее 32 символов")
    return secret.encode()


def _new_test_activation_credentials(user_id: int) -> tuple[str, str]:
    nonce = secrets.token_urlsafe(24)
    digest = hmac.new(_test_activation_secret(),
                      f"secure-x-test-activation:{user_id}:{nonce}".encode(), hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return nonce, hashlib.sha256(token.encode()).hexdigest()


def _test_activation_token(user_id: int, nonce: str) -> str:
    digest = hmac.new(_test_activation_secret(),
                      f"secure-x-test-activation:{user_id}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _test_activation_id(user_id: int) -> str:
    return f"T-{int(user_id):06d}"


def _test_activation_url(user_id: int, nonce: str, fallback_base_url: str | None = None) -> str:
    token = _test_activation_token(user_id, nonce)
    base_url = _public_service_base_url(fallback_base_url)
    return f"{base_url}/test/activate/{_test_activation_id(user_id)}/{token}"


def _test_slot_token(user_id: int, cookie_value: str) -> str:
    digest = hmac.new(_test_activation_secret(),
                      f"secure-x-test-slot:{int(user_id)}:{cookie_value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _test_slot_subscription_url(user_id: int, slot_token: str,
                                fallback_base_url: str | None = None) -> str:
    base_url = _public_service_base_url(fallback_base_url)
    return f"{base_url}/test/sub/{_test_activation_id(user_id)}/{slot_token}"


def _test_activation_browser_hash(cookie_value: str) -> str:
    return hmac.new(_test_activation_secret(), f"secure-x-test-browser:{cookie_value}".encode(), hashlib.sha256).hexdigest()


def _test_activation_csrf(token_hash: str, cookie_value: str) -> str:
    message = f"secure-x-test-activation-csrf:{token_hash}:{cookie_value}"
    return hmac.new(_test_activation_secret(), message.encode(), hashlib.sha256).hexdigest()


def _activation_scope() -> dict[str, str | bool]:
    test_mode = _test_environment.get()
    return {
        "test_mode": test_mode,
        "activation_table": "test_subscription_activation" if test_mode else "subscription_activation",
        "devices_table": "test_activation_devices" if test_mode else "activation_devices",
        "id_prefix": "T" if test_mode else "M",
        "route_prefix": "/test" if test_mode else "",
        "cookie_name": "test_activation_browser" if test_mode else "subscription_activation_browser",
        "cookie_path": "/test/activate" if test_mode else "/activate",
        "namespace": "test" if test_mode else "main",
    }


def _main_activation_credentials(user_id: int, nonce: str | None = None) -> tuple[str, str]:
    nonce = nonce or secrets.token_urlsafe(24)
    digest = hmac.new(_test_activation_secret(),
                      f"secure-x-main-activation:{int(user_id)}:{nonce}".encode(), hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return nonce, hashlib.sha256(token.encode()).hexdigest()


def _main_activation_token(user_id: int, nonce: str) -> str:
    digest = hmac.new(_test_activation_secret(),
                      f"secure-x-main-activation:{int(user_id)}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _main_activation_id(user_id: int) -> str:
    return f"M-{int(user_id):06d}"


def _main_activation_url(user_id: int, nonce: str, fallback_base_url: str | None = None) -> str:
    token = _main_activation_token(user_id, nonce)
    base_url = _public_service_base_url(fallback_base_url)
    return f"{base_url}/activate/{_main_activation_id(user_id)}/{token}"


def _main_slot_token(user_id: int, cookie_value: str) -> str:
    digest = hmac.new(_test_activation_secret(),
                      f"secure-x-main-slot:{int(user_id)}:{cookie_value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _main_slot_subscription_url(user_id: int, slot_token: str,
                                fallback_base_url: str | None = None) -> str:
    base_url = _public_service_base_url(fallback_base_url)
    return f"{base_url}/sub/{_main_activation_id(user_id)}/{slot_token}"


def _main_activation_browser_hash(cookie_value: str) -> str:
    return hmac.new(_test_activation_secret(),
                    f"secure-x-main-browser:{cookie_value}".encode(), hashlib.sha256).hexdigest()


def _main_activation_csrf(token_hash: str, cookie_value: str) -> str:
    message = f"secure-x-main-activation-csrf:{token_hash}:{cookie_value}"
    return hmac.new(_test_activation_secret(), message.encode(), hashlib.sha256).hexdigest()


def _activation_user_id(activation_id: str, *, test_mode: bool) -> int | None:
    prefix = "T" if test_mode else "M"
    match = re.fullmatch(rf"{prefix}-([0-9]{{6,}})", activation_id)
    return int(match.group(1)) if match else None


def _activation_id(user_id: int, *, test_mode: bool) -> str:
    return _test_activation_id(user_id) if test_mode else _main_activation_id(user_id)


def _activation_url(user_id: int, nonce: str, fallback_base_url: str | None = None,
                    *, test_mode: bool) -> str:
    return (_test_activation_url(user_id, nonce, fallback_base_url) if test_mode
            else _main_activation_url(user_id, nonce, fallback_base_url))


def _activation_device_hash(cookie_value: str, *, test_mode: bool) -> str:
    return (_test_activation_browser_hash(cookie_value) if test_mode
            else _main_activation_browser_hash(cookie_value))


def _activation_csrf(token_hash: str, cookie_value: str, *, test_mode: bool) -> str:
    return (_test_activation_csrf(token_hash, cookie_value) if test_mode
            else _main_activation_csrf(token_hash, cookie_value))


def _activation_slot_token(user_id: int, cookie_value: str, *, test_mode: bool) -> str:
    return _test_slot_token(user_id, cookie_value) if test_mode else _main_slot_token(user_id, cookie_value)


def _activation_slot_url(user_id: int, slot_token: str, fallback_base_url: str | None = None,
                         *, test_mode: bool) -> str:
    return (_test_slot_subscription_url(user_id, slot_token, fallback_base_url) if test_mode
            else _main_slot_subscription_url(user_id, slot_token, fallback_base_url))


TEST_ACTIVATION_PLATFORMS = {
    "ios": "iPhone / iPad", "android": "Android", "windows": "Windows",
    "macos": "macOS", "linux": "Linux", "android_tv": "Android TV",
    "appletv": "Apple TV / tvOS", "visionos": "Apple Vision", "chromeos": "ChromeOS",
    "other": "Другое устройство", "unknown": "Не определено",
}
TEST_ACTIVATION_INSTALL_LINKS = {
    "ios": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
    "android": "https://github.com/Happ-proxy/happ-android/releases/latest",
    "windows": "https://github.com/Happ-proxy/happ-desktop/releases/latest",
    "macos": "https://github.com/Happ-proxy/happ-desktop/releases/latest",
    "linux": "https://github.com/Happ-proxy/happ-desktop/releases/latest",
    "android_tv": "https://github.com/Happ-proxy/happ-android/releases/latest",
    "appletv": "https://apps.apple.com/us/app/happ-proxy-utility-for-tv/id6748297274",
    "visionos": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
    "chromeos": "https://github.com/Happ-proxy/happ-android/releases/latest",
    "other": "https://github.com/Happ-proxy/happ-desktop",
    "unknown": "https://github.com/Happ-proxy/happ-desktop",
}


def _test_activation_browser_name(user_agent: str) -> str:
    """Store a coarse browser label only; never retain the raw user agent."""
    value = user_agent or ""
    match = re.search(r"(Edg|Edge|OPR|CriOS|Chrome|Firefox|FxiOS|Version)/([0-9]{1,3})", value, re.I)
    if match:
        family = {"edg": "Edge", "edge": "Edge", "opr": "Opera", "crios": "Chrome iOS",
                  "chrome": "Chrome", "firefox": "Firefox", "fxios": "Firefox iOS",
                  "version": "Safari"}.get(match.group(1).lower(), "Browser")
        return f"{family} {match.group(2)}"
    if "safari" in value.lower():
        return "Safari"
    return "unknown"


def _public_service_base_url(fallback: str | None = None) -> str:
    candidate = (os.getenv("RENDER_EXTERNAL_URL", "").strip() or str(fallback or "").strip()).rstrip("/")
    if candidate and "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(503, "Не удалось определить публичный адрес тестового сервиса")
    return candidate


def _test_activation_page(title: str, message: str, activation_id: str, token: str,
                          csrf: str | None = None, *, limit: int = 1, registered: int = 0,
                          selected_platform: str = "", device_registered: bool = False,
                          happ_link: str | None = None, auto_open: bool = False,
                          tone: str = "normal", test_mode: bool = True,
                          pending_import: bool = False, status_url: str = "") -> str:
    route_prefix = "/test" if test_mode else ""
    mode_suffix = " · ТЕСТ" if test_mode else ""
    page_tag = "ТЕСТОВАЯ СРЕДА · SECURE X" if test_mode else "SECURE X · ПОДТВЕРЖДЕНИЕ ДОСТУПА"
    title_html = html.escape(title)
    message_html = html.escape(message)
    safe_selected = selected_platform if selected_platform in TEST_ACTIVATION_PLATFORMS else ""
    options = ''.join(
        f'<option value="{html.escape(key, quote=True)}"{(" selected" if key == safe_selected else "")}>{html.escape(label)}</option>'
        for key, label in TEST_ACTIVATION_PLATFORMS.items() if key != "unknown")
    activation_form = ""
    if csrf:
        action_url = f'{route_prefix}/activate/{html.escape(activation_id, quote=True)}/{html.escape(token, quote=True)}'
        activation_form = (f'<form method="post" action="{action_url}" id="activationForm">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
            f'<div class="device-detect" id="deviceDetect">Определяем устройство…</div>'
            f'<button type="button" class="back-button" id="manualDevice">Это другое устройство</button>'
            f'<label class="device-picker" id="devicePicker" hidden>Выберите устройство<select name="platform" required>{options}</select></label>'
            f'<button type="submit" id="confirmDevice">{("Продолжить на этом устройстве" if device_registered else "Подтвердить устройство")}</button>'
            f'</form>')
    happ_action = ""
    if pending_import:
        happ_action = ('<section class="tv-import" id="activationPending" role="status" aria-live="polite">'
                       '<b>Готовим подписку</b><p class="foot">Подтверждение сохранено. Ссылка появится здесь автоматически.</p></section>'
                       '<div id="activationReady" hidden></div>')
    elif happ_link:
        if safe_selected == "appletv":
            happ_action = (f'<section class="tv-import"><b>Перенос на Apple TV{mode_suffix}</b>'
                '<p class="foot">На Apple TV откройте Happ → Web Import. На телефоне откройте tv.happ.su, введите временный код с экрана телевизора и отправьте туда эту зашифрованную ссылку.</p>'
                f'<label>Зашифрованная ссылка для переноса<textarea id="subscriptionLink" readonly rows="3">{html.escape(happ_link)}</textarea></label>'
                '<button type="button" id="copySubscription">Скопировать ссылку</button></section>')
        else:
            happ_action = (f'<a class="app-button" id="openHapp" href="{html.escape(happ_link, quote=True)}" rel="noreferrer">'
                           'Открыть и добавить в Happ</a><p class="foot">Откройте приложение по кнопке. Если браузер спросит подтверждение перехода — разрешите его.</p>')
    install_url = TEST_ACTIVATION_INSTALL_LINKS.get(safe_selected or "unknown", TEST_ACTIVATION_INSTALL_LINKS["unknown"])
    install_action = (f'<a class="install-link" id="installLink" href="{install_url}" target="_blank" rel="noopener noreferrer">'
                      'Установить Happ для этого устройства</a>')
    links_json = json.dumps(TEST_ACTIVATION_INSTALL_LINKS, ensure_ascii=True)
    poll_script = ""
    if pending_import and status_url:
        poll_script = ("(()=>{const node=document.getElementById('activationPending');const result=document.getElementById('activationReady');"
            f"const url={json.dumps(status_url)};let attempts=0;async function check(){{try{{const response=await fetch(url,{{cache:'no-store',credentials:'same-origin',headers:{{Accept:'application/json'}}}});"
            "const data=await response.json();if(data.ready&&data.happ_link){node.hidden=true;const link=document.createElement('a');link.className='app-button';"
            "link.href=data.happ_link;link.rel='noreferrer';link.textContent='Открыть и добавить в Happ';result.append(link);"
            "const note=document.createElement('p');note.className='foot';note.textContent='Подписка подготовлена. Нажмите кнопку, чтобы открыть Happ.';result.append(note);result.hidden=false;return;}"
            "if(data.error){node.textContent=data.error;return;}}catch{}attempts++;if(attempts<90)setTimeout(check,2000);"
            "else node.textContent='Подготовка занимает больше обычного. Обновите страницу через минуту.';}check();})();")
    meter = f'<div class="slot-meter"><b>{min(registered, limit)} / {limit}</b><span>занято устройств</span></div>'
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><meta name="robots" content="noindex,nofollow">
<link rel="icon" href="/static/favicon.ico?v=20261119-1" sizes="any">
<title>{title_html}</title><style>
:root{{color-scheme:dark;background:#0b100e;color:#edf2ed;font:16px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px;background:linear-gradient(145deg,#111c17,#090d0c 65%)}}
main{{width:min(100%,520px);border:1px solid #34453c;background:#111a16;padding:clamp(24px,6vw,40px);box-shadow:0 24px 70px #0008}}
.tag{{margin:0 0 18px;color:#e8b967;font-size:12px;font-weight:800;letter-spacing:.16em}}h1{{margin:0 0 14px;font-size:clamp(25px,6vw,34px);line-height:1.15;letter-spacing:-.035em}}p{{margin:0;color:#bbc8bf}}.notice{{border-left:3px solid {('#de8276' if tone == 'error' else '#dfb45d')};padding:12px 14px;margin:22px 0;background:#ffffff08}}
button{{width:100%;min-height:50px;margin-top:16px;border:1px solid #d6a84e;background:#d6a84e;color:#15140f;font:700 15px system-ui;cursor:pointer;padding:12px 18px}}button:hover{{background:#edc572}}button:focus-visible,a:focus-visible,select:focus-visible{{outline:3px solid #f4d991;outline-offset:3px}}
select{{width:100%;min-height:48px;margin-top:8px;padding:10px 12px;border:1px solid #43564b;background:#0b100e;color:#edf2ed;font:inherit}}textarea{{width:100%;margin-top:8px;padding:10px 12px;border:1px solid #43564b;background:#0b100e;color:#edf2ed;font:12px/1.5 ui-monospace,monospace;overflow-wrap:anywhere;resize:vertical}}label{{display:block;margin-top:14px;color:#c9d4cd;font-size:13px;font-weight:700}}.device-detect{{padding:12px 14px;border:1px solid #33443a;background:#0d1511;color:#c7d5cc;font-size:14px}}.back-button{{min-height:40px;border:0;background:transparent;color:#c8d7ce;text-decoration:underline}}.back-button:hover{{background:#ffffff0a}}.slot-meter{{display:flex;align-items:baseline;gap:9px;margin:15px 0;color:#a9b8ae}}.slot-meter b{{color:#e8bd72;font:600 18px ui-monospace,monospace}}.app-button{{display:block;margin-top:16px;padding:14px 18px;background:#d6a84e;color:#15140f;text-align:center;text-decoration:none;font-weight:800}}.install-link{{display:inline-block;margin-top:16px;color:#b8dfc4}}.foot{{margin-top:12px;color:#829087;font-size:13px}}.privacy{{margin-top:22px;padding-top:14px;border-top:1px solid #293a31;color:#829087;font-size:12px}}[hidden]{{display:none!important}}
@media(max-width:520px){{body{{padding:12px}}main{{padding:24px 20px}}}}
@media(prefers-reduced-motion:no-preference){{main{{animation:appear .28s ease-out}}@keyframes appear{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}}}
</style></head><body><main><p class="tag">{html.escape(page_tag)}</p><h1>{title_html}</h1>
<p class="notice">{message_html}</p>{meter}{activation_form}{install_action}{happ_action}
<p class="privacy">Для запоминания регистрации используется случайный cookie этого браузера. В панели сохраняются выбранная платформа, язык браузера и его тип/версия. IP-адрес и скрытый отпечаток устройства не сохраняются. Очистка cookies или другой браузер считаются новым устройством.</p>
<script>(()=>{{const names={json.dumps(TEST_ACTIVATION_PLATFORMS, ensure_ascii=False)};const links={links_json};const select=document.querySelector('select[name="platform"]');const picker=document.getElementById('devicePicker');const detect=document.getElementById('deviceDetect');const manual=document.getElementById('manualDevice');const install=document.getElementById('installLink');const openHapp=document.getElementById('openHapp');if(openHapp&&{json.dumps(bool(auto_open))})setTimeout(()=>{{window.location.href=openHapp.href;}},350);if(!select)return;const copyButton=document.getElementById('copySubscription');if(copyButton)copyButton.addEventListener('click',async()=>{{const input=document.getElementById('subscriptionLink');try{{if(!navigator.clipboard?.writeText)throw new Error('clipboard');await navigator.clipboard.writeText(input.value);copyButton.textContent='Скопировано';}}catch{{input.focus();input.select();copyButton.textContent='Ссылка выделена для копирования';}}}});let ua=navigator.userAgent||'';let platform='';if(/AppleTV|tvOS/i.test(ua))platform='appletv';else if(/Vision/i.test(ua))platform='visionos';else if(/CrOS/i.test(ua))platform='chromeos';else if(/Android/i.test(ua)&&/TV|Leanback/i.test(ua))platform='android_tv';else if(/iPhone|iPad|iPod/i.test(ua)||(/Macintosh/i.test(ua)&&navigator.maxTouchPoints>1))platform='ios';else if(/Android/i.test(ua))platform='android';else if(/Windows/i.test(ua))platform='windows';else if(/Macintosh|Mac OS X/i.test(ua))platform='macos';else if(/Linux|X11/i.test(ua))platform='linux';const preset={json.dumps(safe_selected)};if(preset)platform=preset;if(platform&&[...select.options].some(o=>o.value===platform)){{select.value=platform;detect.textContent='Устройство: '+names[platform]+(preset?' · это устройство уже подтверждено':' · определено браузером');picker.hidden=true;manual.textContent='Определено неверно? Выбрать вручную';}}else{{select.value='';picker.hidden=false;detect.textContent='Не удалось определить устройство. Выберите платформу вручную.';manual.hidden=true;}}function syncLink(){{const key=select.value||'unknown';install.href=links[key]||links.unknown;install.textContent='Установить Happ · '+(names[key]||names.unknown);}}manual.addEventListener('click',()=>{{picker.hidden=false;select.focus();manual.hidden=true;detect.textContent='Выберите платформу вручную. Это не меняет лимит регистрации.';}});select.addEventListener('change',syncLink);syncLink();}})();{poll_script}</script></main></body></html>"""


def _test_activation_gate_profile(activation_url: str, user_name: str = "", *, test_mode: bool = True) -> str:
    title = ("ТЕСТ · " if test_mode else "") + "Подтверждение устройства" + (f" · {user_name}" if user_name else "")
    announcement = "Откройте информацию профиля и подтвердите устройство на странице Secure X."
    marker = "ТЕСТ · " if test_mode else ""
    lines = [
        "#profile-title: base64:" + base64.b64encode(title.encode()).decode(),
        "#profile-update-interval: 1",
        "#announce: base64:" + base64.b64encode(announcement.encode()).decode(),
        "#sub-info-text: " + marker + "Подтвердите устройство, затем импортируйте индивидуальную подписку.",
    ]
    if activation_url:
        lines.extend(["#sub-info-button-text: Подтвердить устройство",
                      "#sub-info-button-link: " + activation_url])
    lines.extend(["vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?type=tcp&encryption=none#"
                  + quote("Подтвердите устройство" + (" · ТЕСТ" if test_mode else ""), safe=""), ""])
    return "\n".join(lines)


def _request_wants_subscription(request: Request) -> bool:
    """Subscription fetches get plain profile data; browser navigation gets the activation page."""
    user_agent = request.headers.get("user-agent", "").lower()
    if "happ" in user_agent:
        return True
    if request.headers.get("sec-fetch-mode", "").lower() == "navigate":
        return False
    return "text/html" not in request.headers.get("accept", "").lower()


def _xray_placeholder_outbound(reason: str) -> dict:
    return {
        "tag": "securex-placeholder",
        "protocol": "vless",
        "settings": {"vnext": [{"address": "127.0.0.1", "port": 1,
                                  "users": [{"id": "00000000-0000-0000-0000-000000000000",
                                             "encryption": "none"}]}]},
        "streamSettings": {"network": "tcp", "security": "none"},
        "remarks": reason,
    }


def _serialize_xray_document(document: dict) -> str:
    serialized = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    parsed = json.loads(serialized, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("outbounds"), list):
        raise ValueError("Сформирован некорректный Xray JSON")
    if not _xray_json_uris(serialized):
        raise ValueError("Сформированный Xray JSON не содержит поддерживаемого сервера")
    return serialized


async def _build_manifest_json(conn, user, body: bytes, servers: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    expires_at = user.get("expires_at")
    expired = expires_at is not None and expires_at <= now
    active = bool(user.get("enabled", True)) and not expired and user.get("status") == "active"
    test_mode = _test_environment.get()
    reason = "Время подписки закончилось" if expired else "Подписка отключена"
    if test_mode:
        reason += " · ТЕСТ"

    document = _parse_xray_json_body(body, "Объединённая подписка") if body else {"outbounds": []}
    _set_xray_server_description(document)

    if active:
        public_id = await _ensure_public_id(conn, int(user["id"]))
        if public_id is None:
            active = False
            reason = "Подписка отключена, обратитесь в поддержку"
            if test_mode:
                reason += " · ТЕСТ"

    if active:
        activation_table = str(_activation_scope()["activation_table"])
        activation_state = await conn.fetchrow(
            f"SELECT activated_at FROM {activation_table} WHERE user_id=$1", user["id"])
        if activation_state and activation_state["activated_at"] is None:
            # JSON cannot contain Happ's text-profile directives; keep the
            # activation gate by publishing only a non-working outbound.
            reason = "Подтвердите устройство" + (" · ТЕСТ" if test_mode else "")
            meta = copy.deepcopy(document.get("meta")) if isinstance(document.get("meta"), dict) else None
            document = {"outbounds": [_xray_placeholder_outbound(reason)]}
            if meta is not None:
                document["meta"] = meta
            return _serialize_xray_document(document)

    if not active:
        meta = copy.deepcopy(document.get("meta")) if isinstance(document.get("meta"), dict) else None
        document = {"outbounds": [_xray_placeholder_outbound(reason)]}
        if meta is not None:
            document["meta"] = meta
        return _serialize_xray_document(document)

    order_sql = "CASE WHEN source='sas' THEN 1 ELSE 0 END,sort_order,id"
    global_rows = await conn.fetch(
        f"SELECT id,original_name,custom_name,hidden,sort_order,source FROM servers "
        f"WHERE present_upstream=TRUE ORDER BY {order_sql}")
    overrides = await conn.fetch(
        "SELECT server_id,hidden,custom_name FROM user_servers WHERE user_id=$1", user["id"])
    overrides_by_id = {str(row["server_id"]): row for row in overrides}
    rows_by_id = {str(row["id"]): row for row in global_rows}

    records_by_key: dict[tuple[str, str, str], list[dict]] = {}
    for server in servers:
        try:
            parsed = parse_server_uri(server["uri"])
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        key = (parsed["id"], parsed["source_key"], str(server.get("source", "primary")))
        records_by_key.setdefault(key, []).append(server)

    def find_server(uri: str, source: str):
        parsed = parse_server_uri(uri)
        matches = records_by_key.get((parsed["id"], parsed["source_key"], source), [])
        if len(matches) == 1:
            record = matches[0]
        else:
            named = [item for item in matches if item.get("name") == parsed["name"]]
            if len(named) != 1:
                return None, None
            record = named[0]
        return rows_by_id.get(str(record["id"])), record

    output_outbounds: list[dict] = []
    first_visible_outbound: dict | None = None
    visible_count = 0
    for outbound in document["outbounds"]:
        if not isinstance(outbound, dict):
            continue
        records = _xray_outbound_records(document, outbound)
        if not records:
            output_outbounds.append(copy.deepcopy(outbound))
            continue

        selected: list[tuple[dict, dict, str]] = []
        keep_original = True
        for uri, node in records:
            parsed = parse_server_uri(uri)
            source = "primary"
            if not records_by_key.get((parsed["id"], parsed["source_key"], source)):
                source = "sas"
            row, server = find_server(uri, source)
            if row is None or server is None:
                keep_original = False
                continue
            if source == "sas" and not bool(user.get("include_sas", True)):
                keep_original = False
                continue
            override = overrides_by_id.get(str(row["id"]))
            if override and override["hidden"] is True:
                keep_original = False
                continue
            if (override and override["hidden"] is None and row["hidden"]) or (not override and row["hidden"]):
                keep_original = False
                continue
            name = ((override["custom_name"] if override and override["custom_name"] else None)
                    or row["custom_name"] or server["name"])
            if test_mode:
                name = re.sub(r"(?: · ТЕСТ)+$", "", name, flags=re.I) + " · ТЕСТ"
            if name != server["name"]:
                keep_original = False
            selected.append((node, server, name))

        if len(selected) == len(records) and keep_original:
            output_outbounds.append(copy.deepcopy(outbound))
            visible_count += len(records)
            if first_visible_outbound is None:
                first_visible_outbound = copy.deepcopy(records[0][1])
            continue
        original_tag = str(outbound.get("tag") or "proxy")
        for index, (node, server, name) in enumerate(selected):
            if name != server["name"]:
                node["remarks"] = name
            if len(selected) > 1 and index > 0:
                node["tag"] = f"{original_tag}-securex-{index + 1}"
            output_outbounds.append(node)
            visible_count += 1
            if first_visible_outbound is None:
                first_visible_outbound = copy.deepcopy(node)

    if not visible_count:
        raise HTTPException(502, "Для этой подписки нет доступных серверов. Обновите список серверов и проверьте, что они не скрыты.")

    if test_mode:
        geo_state = await conn.fetchrow(
            "SELECT activated_at,geo_device_limit FROM test_geo_state WHERE user_id=$1", user["id"])
        if geo_state and geo_state["geo_device_limit"] is not None and geo_state["activated_at"] is None:
            first_visible_outbound["remarks"] = "Скачать geo-файлы · ТЕСТ"
            output_outbounds = [first_visible_outbound]

    document["outbounds"] = output_outbounds
    return _serialize_xray_document(document)


async def build_manifest_content(conn, user, body: bytes, headers: dict, settings: dict,
                                 servers: list[dict] | None = None,
                                 public_base_url: str | None = None) -> str:
    if settings.get("profile_format", "url") == "json":
        if servers is None:
            servers = await _sync_servers(conn, body)
        return await _build_manifest_json(conn, user, body, servers)
    if servers is None:
        servers = await _sync_servers(conn, body)
    now = datetime.now(timezone.utc)
    expired = user["expires_at"] is not None and user["expires_at"] <= now
    active = bool(user["enabled"]) and not expired and user["status"] == "active"
    if not active:
        announcement = "⛔ Время подписки закончилось, обратитесь в поддержку" if expired else "⛔ Подписка отключена, обратитесь в поддержку"
    else:
        public_id = await _ensure_public_id(conn, int(user["id"]))
        if public_id is None:
            active = False
            announcement = "⛔ Подписка отключена, обратитесь в поддержку"
        else:
            announcement = _subscription_announcement(user["expires_at"], public_id, now)
    # Give every generated Drive profile a distinct default name in Happ. The
    # underlying server credentials remain those from the shared upstream.
    title = user["title_override"] or f"{settings.get('title', 'Secure X')} · {user.get('name', user.get('slug', 'Пользователь'))}"
    if expired:
        title = f"Время подписки закончилось · {user.get('name', user.get('slug', 'Пользователь'))}"
    elif not user["enabled"] or user["status"] != "active":
        title = f"Подписка отключена · {user.get('name', user.get('slug', 'Пользователь'))}"
    test_mode = _test_environment.get()
    if test_mode:
        title = "🧪 ТЕСТ · " + title
        announcement = "🧪 ТЕСТОВАЯ ПОДПИСКА · " + (announcement or "Тестовый профиль")
    lines = ["#profile-title: base64:" + base64.b64encode(title.encode()).decode(),
             f"#profile-update-interval: {int(settings.get('update_interval', 1))}",
             "#announce: base64:" + base64.b64encode(announcement.encode()).decode(),
             "#support-url: " + str(settings.get("support_url", "https://t.me/moserrr1"))]
    if test_mode:
        # Keep the test theme and pin bound only to the test Happ account.
        provider_id = os.getenv("TEST_HAPP_PROVIDER_ID", "").strip()
    else:
        provider_id = os.getenv("HAPP_PROVIDER_ID", "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{8}", provider_id):
        lines.append("#providerid " + provider_id)
        if test_mode:
            lines.append("#color-profile: " + json.dumps(TEST_HAPP_COLOR_PROFILE, separators=(",", ":")))
            lines.append("#subscription-pin: true")
    is_legacy = user.get("slug") == "legacy" or user.get("drive_file_id") == LEGACY_DRIVE_FILE_ID
    if is_legacy or not bool(user.get("hide_traffic_usage", False)):
        expiry = int(user["expires_at"].timestamp()) if user.get("expires_at") else None
        lines.append("#subscription-userinfo: " + _userinfo(headers, expire_override=expiry))
    web_url = str(settings.get("web_page_url", "")).strip()
    if web_url:
        lines.append("#profile-web-page-url: " + web_url)
    if active:
        activation_state = None
        if test_mode:
            activation_state = await conn.fetchrow("""SELECT token_nonce,token_hash,activated_at
                FROM test_subscription_activation WHERE user_id=$1""", user["id"])
        else:
            activation_state = await conn.fetchrow("""SELECT token_nonce,token_hash,activated_at
                FROM subscription_activation WHERE user_id=$1""", user["id"])
        if activation_state and activation_state["activated_at"] is None:
            activation_url = _activation_url(int(user["id"]), activation_state["token_nonce"], public_base_url,
                                             test_mode=test_mode)
            activation_page_url = activation_url + "?view=page"
            action_text = ("🧪 Подтвердите браузер и обновите Happ. Если включён Geo-лимит, скачайте оба geo-файла."
                           if test_mode else "Подтвердите браузер на странице Secure X, чтобы открыть серверы.")
            lines.extend([
                "#sub-info-text: " + ("🧪 ТЕСТ · " if test_mode else "") + action_text,
                "#sub-info-button-text: Подключить устройство",
                "#sub-info-button-link: " + activation_page_url,
                "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?type=tcp&encryption=none#" +
                quote("Подтвердите устройство" + (" · ТЕСТ" if test_mode else ""), safe=""),
            ])
            return "\n".join(lines) + "\n"
        geo_locked = False
        if test_mode:
            geo_state = await conn.fetchrow("SELECT activated_at,geo_device_limit FROM test_geo_state WHERE user_id=$1", user["id"])
            geo_enabled = bool(geo_state and geo_state["geo_device_limit"] is not None)
            geo_locked = geo_enabled and geo_state["activated_at"] is None
            if geo_enabled:
                base_url = (public_base_url or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
                if not base_url:
                    raise HTTPException(503, "Не удалось определить адрес тестового сервиса для геофайлов")
                signature = _test_geo_signature(int(user["id"]))
                geo_root = f"{base_url}/test/geo/{int(user['id'])}/{signature}"
                routing = {
                    "Name": f"Secure X · ТЕСТ · geo {user['id']}",
                    "Geoipurl": geo_root + "/geoip.dat",
                    "Geositeurl": geo_root + "/geosite.dat",
                    "LastUpdated": str(int(time.time())),
                    "DnsHosts": {}, "DirectSites": [], "DirectIp": [],
                    "ProxySites": [], "ProxyIp": [], "BlockSites": [], "BlockIp": [],
                    "DomainStrategy": "AsIs", "FakeDNS": "false",
                }
                routing64 = base64.urlsafe_b64encode(json.dumps(routing, ensure_ascii=False).encode()).decode().rstrip("=")
                lines.append("happ://routing/add/" + routing64)
        order_sql = "CASE WHEN source='sas' THEN 1 ELSE 0 END,sort_order,id"
        global_rows = await conn.fetch(f"SELECT id,original_name,custom_name,hidden,sort_order,source FROM servers WHERE present_upstream=TRUE ORDER BY {order_sql}")
        upstream_by_id = {server["id"]: server for server in servers}
        overrides = await conn.fetch("SELECT server_id,hidden,custom_name FROM user_servers WHERE user_id=$1", user["id"])
        overrides_by_id = {row["server_id"]: row for row in overrides}
        server_lines = []
        first_visible_uri = None
        for global_row in global_rows:
            server = upstream_by_id.get(global_row["id"])
            if not server:
                continue
            source = server.get("source", global_row.get("source", "primary"))
            if source == "sas" and not bool(user.get("include_sas", True)):
                continue
            override = overrides_by_id.get(server["id"])
            if override and override["hidden"] is True: continue
            if override and override["hidden"] is None and global_row["hidden"]: continue
            if not override and global_row["hidden"]: continue
            name = (override["custom_name"] if override and override["custom_name"] else None) or global_row["custom_name"] or server["name"]
            # Always rebuild the fragment, even when the provider's display
            # name is unchanged. Some providers send raw spaces in `#name`;
            # emitting those as-is makes otherwise valid nodes disappear in
            # clients that expect a URL-encoded fragment.
            if test_mode:
                name = re.sub(r"(?: · ТЕСТ)+$", "", name, flags=re.I) + " · ТЕСТ"
            server_lines.append(rename_server_uri(server["uri"], name))
            if first_visible_uri is None:
                first_visible_uri = server["uri"]
        if not server_lines:
            # Do not let Apps Script replace a working Drive profile with a
            # metadata-only file that Happ rejects as having no server links.
            raise HTTPException(502, "Для этой подписки нет доступных серверов. Обновите список серверов и проверьте, что они не скрыты.")
        if geo_locked:
            if not first_visible_uri:
                raise HTTPException(502, "Нет рабочего тестового сервера для начальной подписки")
            server_lines = [rename_server_uri(first_visible_uri, "Скачать geo-файлы · ТЕСТ")]
        lines.extend(server_lines)
    else:
        reason = "Время подписки закончилось" if expired else "Подписка отключена"
        if test_mode:
            reason += " · ТЕСТ"
        lines.append("vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?type=tcp&encryption=none#" + quote(reason, safe=""))
    return "\n".join(lines) + "\n"


def _user_needs_sas(user, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    expiry = _server_row_value(user, "expires_at")
    active = (_server_row_value(user, "status", "active") == "active"
              and bool(_server_row_value(user, "enabled", True))
              and (expiry is None or expiry > now))
    return active and bool(_server_row_value(user, "include_sas", True))


def _secure_headers(response: Response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    # Admin HTML and API responses contain changing user/config data. Prevent
    # browsers and intermediary caches from showing an older panel revision.
    response.headers["Cache-Control"] = "no-store"


class PanelSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = _test_environment.set(request.url.path == "/test" or request.url.path.startswith("/test/"))
        try:
            response = await call_next(request)
            if request.url.path.startswith(("/admin", "/api/", "/test", "/activate/", "/sub/")):
                _secure_headers(response)
            elif request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
                response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        finally:
            _test_environment.reset(token)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page():
    # Select a purpose-built page before loading the admin app bundle. Use
    # viewport and pointer capabilities so mobile browsers in desktop mode
    # still receive the phone layout when their viewport is narrow.
    return HTMLResponse("""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">
<meta name=\"color-scheme\" content=\"dark\"><meta name=\"theme-color\" content=\"#0c1110\"><link rel=\"icon\" href=\"/static/favicon.ico?v=20261119-1\" sizes=\"any\"><title>Secure X — панель</title></head>
<body style=\"margin:0;background:#0c1110;color:#e8efea;font:15px 'Segoe UI',system-ui;display:grid;min-height:100dvh;place-items:center\">
<p>Открываем панель…</p><script>(()=>{const mobile=matchMedia('(max-width: 760px), (pointer: coarse)').matches;
location.replace('/admin/'+(mobile?'mobile':'desktop')+location.search+location.hash)})()</script>
<noscript><p><a href=\"/admin/mobile\">Телефон</a> · <a href=\"/admin/desktop\">Компьютер</a></p></noscript>
</body></html>""")


async def test_page():
    return HTMLResponse("""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">
<meta name=\"color-scheme\" content=\"dark\"><meta name=\"theme-color\" content=\"#0c1110\"><link rel=\"icon\" href=\"/static/favicon.ico?v=20261119-1\" sizes=\"any\"><title>Secure X — ТЕСТ</title></head>
<body style=\"margin:0;background:#0c1110;color:#e8efea;font:15px 'Segoe UI',system-ui;display:grid;min-height:100dvh;place-items:center\">
<p>Открываем тестовую панель…</p><script>(()=>{const mobile=matchMedia('(max-width: 760px), (pointer: coarse)').matches;
location.replace('/test/'+(mobile?'mobile':'desktop')+location.search+location.hash)})()</script>
<noscript><p><a href=\"/test/mobile\">Телефон</a> · <a href=\"/test/desktop\">Компьютер</a></p></noscript>
</body></html>""")


async def test_desktop_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "admin_desktop.html"),
                        headers={"Cache-Control": "no-store"})


async def test_mobile_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "admin_mobile.html"),
                        headers={"Cache-Control": "no-store"})


@router.get("/admin/desktop", response_class=HTMLResponse, include_in_schema=False)
async def admin_desktop_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "admin_desktop.html"),
                        headers={"Cache-Control": "no-store"})


@router.get("/admin/mobile", response_class=HTMLResponse, include_in_schema=False)
async def admin_mobile_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "admin_mobile.html"),
                        headers={"Cache-Control": "no-store"})


@router.post("/api/admin/auth/login")
async def login(data: LoginBody, request: Request, response: Response):
    if not state.admin_configured:
        raise HTTPException(503, "Админ-панель отключена: задайте ADMIN_USERNAME, пароль и SESSION_SECRET длиной не менее 32 символов")
    ip = request.client.host if request.client else "unknown"
    failures, blocked_until = state.login_fails.get(ip, (0, 0))
    if blocked_until > time.monotonic():
        raise HTTPException(429, "Слишком много попыток. Попробуйте позже.")
    valid_user = hmac.compare_digest(data.username.encode(), os.getenv("ADMIN_USERNAME", "").encode())
    valid_password = _password_ok(data.password)
    if not (valid_user and valid_password):
        failures += 1
        delay = min(300, 2 ** max(0, failures - 4)) if failures >= 5 else 0
        state.login_fails[ip] = (failures, time.monotonic() + delay)
        raise HTTPException(401, "Неверные данные входа")
    state.login_fails.pop(ip, None)
    csrf = secrets.token_urlsafe(24)
    test_mode = request.url.path.startswith("/test/")
    token = _sign_session({"u": os.getenv("ADMIN_USERNAME"), "c": csrf,
                           "environment": "test" if test_mode else "main",
                           "exp": int(time.time()) + SESSION_SECONDS})
    cookie_name = "test_admin_session" if test_mode else "admin_session"
    response.set_cookie(cookie_name, token, max_age=SESSION_SECONDS, httponly=True, secure=True,
                        samesite="strict", path="/test" if test_mode else "/")
    return {"ok": True, "csrf_token": csrf, "username": os.getenv("ADMIN_USERNAME")}


@router.post("/api/admin/auth/logout")
async def logout(request: Request, response: Response):
    _auth(request, csrf=True)
    test_mode = request.url.path.startswith("/test/")
    response.delete_cookie("test_admin_session" if test_mode else "admin_session",
                          path="/test" if test_mode else "/")
    return {"ok": True}


@router.get("/api/admin/auth/me")
async def me(request: Request):
    payload = _auth(request)
    return {"username": payload["u"], "csrf_token": payload["c"]}


@router.get("/api/admin/dashboard")
async def dashboard(request: Request):
    _auth(request)
    # MCP asks for a lightweight status snapshot. Source refreshes are available
    # explicitly through the server refresh tool and should not make a status
    # read wait on two sequential provider requests.
    if request.query_params.get("cached") != "1":
        try:
            await state.fetch_upstream()
        except HTTPException:
            # The overview should remain accessible even while a source is down.
            pass
    pool = await state.db(required=False)
    db_error = state.test_db_error if _test_environment.get() else state.db_error
    db_info = {"connected": pool is not None, "error": db_error if pool is None else None}
    user_count = {"active": 0, "disabled": 0, "total": 0}
    device_limit_count = 0
    device_limit_errors = 0
    errors = []
    last_script = None
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT enabled,status,expires_at,slug,drive_file_id,device_limit,device_limit_status FROM users")
            user_count["total"] = len(rows)
            for row in rows:
                if row["enabled"] and row["status"] == "active" and (row["expires_at"] is None or row["expires_at"] > datetime.now(timezone.utc)): user_count["active"] += 1
                else: user_count["disabled"] += 1
                if row["device_limit"] is not None and row["slug"] != "legacy" and row["drive_file_id"] != LEGACY_DRIVE_FILE_ID:
                    device_limit_count += 1
                    if row["device_limit_status"] == "error":
                        device_limit_errors += 1
            errors = [dict(r) for r in await conn.fetch("SELECT id,name,last_sync_error,last_synced_at FROM users WHERE last_sync_error IS NOT NULL ORDER BY updated_at DESC")]
            row = await conn.fetchrow("SELECT value FROM app_state WHERE key='last_script_request'")
            if row: last_script = row["value"]
            info = _userinfo(state.upstream_headers)
    else:
        info = _userinfo(state.upstream_headers)
    parts = dict(token.strip().split("=", 1) for token in info.split(";") if "=" in token)
    data = {k: int(v) if str(v).isdigit() else 0 for k, v in parts.items()}
    primary_server_count = 0
    if state.upstream_body is not None:
        try:
            primary_server_count = len(_extract_uris(_decode_subscription(state.upstream_body)))
        except (ValueError, UnicodeDecodeError):
            primary_server_count = 0
    sas_server_count = 0
    if state.sas_body is not None:
        try:
            sas_server_count = sum(not is_excluded_sas_server(uri)
                                   for uri in _extract_uris(_decode_subscription(state.sas_body)))
        except (ValueError, UnicodeDecodeError):
            sas_server_count = 0
    return {"database": db_info, "upstream": {"cached": state.upstream_body is not None,
            "last_success_at": state.combined_success_at or state.upstream_success_at,
            "server_count": len(_extract_uris(_decode_subscription(state.combined_body or state.upstream_body))) if (state.combined_body or state.upstream_body) else 0,
            "warning": state.upstream_warning}, "traffic": {"upload": data.get("upload", 0), "download": data.get("download", 0),
            "total": data.get("total", 0), "expire": data.get("expire", 0)}, "users": user_count,
            "device_limits": {"count": device_limit_count, "errors": device_limit_errors},
            "sources": {"primary": {"configured": bool(os.getenv("SUBSCRIPTION_URL", "").strip()),
                                     "server_count": primary_server_count},
                        "sas": {"configured": bool(os.getenv("SAS_SUBSCRIPTION_URL", "").strip()),
                                "server_count": sas_server_count,
                                "last_success_at": state.sas_success_at,
                                "warning": state.sas_warning}},
            "last_script_request": last_script, "sync_errors": errors, "sync_enabled": state.sync_configured,
            "drive_delivery": {"configured": state.drive_push_configured,
                               "last_success_at": state.drive_push_last or None,
                               "warning": state.drive_push_error,
                               "pending": (not _test_environment.get() and
                                           (state._main_drive_sync_task is not None
                                            or state._main_drive_sync_all
                                            or bool(state._main_drive_sync_targets)))}}


@router.get("/api/admin/users")
async def users_list(request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        if _test_environment.get():
            rows = await conn.fetch("""SELECT u.*,g.geo_device_limit,g.geoip_requests,g.geosite_requests,
                g.download_requests AS geo_download_requests,
                g.geoip_downloaded_at,g.geosite_downloaded_at,g.activated_at AS geo_activated_at,
                (a.user_id IS NOT NULL) AS test_activation_enabled,a.token_nonce AS test_activation_nonce,
                a.token_hash AS test_activation_hash,a.activated_at AS test_activation_at,
                a.device_limit AS test_activation_limit,
                (SELECT count(*) FROM test_activation_devices d WHERE d.user_id=a.user_id) AS test_activation_devices_count,
                a.browser_hash AS test_activation_browser_hash,
                (SELECT count(*) FROM test_activation_devices d WHERE d.user_id=a.user_id
                    AND d.encrypted_link IS NOT NULL) AS test_activation_links_count
                FROM users u LEFT JOIN test_geo_state g ON g.user_id=u.id
                LEFT JOIN test_subscription_activation a ON a.user_id=u.id ORDER BY u.created_at,u.id""")
        else:
            rows = await conn.fetch("""SELECT u.*, (a.user_id IS NOT NULL) AS activation_enabled,
                a.token_nonce AS activation_nonce,a.activated_at AS activation_at,
                a.device_limit AS activation_limit,
                (SELECT count(*) FROM activation_devices d WHERE d.user_id=a.user_id) AS activation_devices_count,
                (SELECT count(*) FROM activation_devices d WHERE d.user_id=a.user_id
                    AND d.encrypted_link IS NOT NULL) AS activation_links_count
                FROM users u LEFT JOIN subscription_activation a ON a.user_id=u.id
                ORDER BY u.created_at,u.id""")
    api_key_configured = bool(os.getenv("DRIVE_API_KEY", ""))
    users = []
    for row in rows:
        user = _serialize(row)
        # Preload the already-authorized download URL so mobile clipboard access
        # can happen directly during the user's tap, without an awaited fetch.
        user["drive_link"] = (_drive_subscription_link(user["drive_file_id"])
                              if user.get("drive_file_id") and api_key_configured else None)
        if _test_environment.get():
            nonce = user.pop("test_activation_nonce", None)
            user.pop("test_activation_hash", None)
            user["test_activation_id"] = _test_activation_id(int(user["id"])) if user.get("test_activation_enabled") else None
            user["test_activation_url"] = None
            if user.get("test_activation_enabled") and nonce:
                user["test_activation_url"] = _test_activation_url(
                    int(user["id"]), nonce, str(request.base_url).rstrip("/"))
            user.pop("test_activation_browser_hash", None)
        else:
            nonce = user.pop("activation_nonce", None)
            user["activation_id"] = _main_activation_id(int(user["id"])) if user.get("activation_enabled") else None
            user["activation_url"] = (_main_activation_url(int(user["id"]), nonce, str(request.base_url).rstrip("/"))
                                      if user.get("activation_enabled") and nonce else None)
        users.append(user)
    return {"users": users, "device_limit_bounds": {"min": happ_limit.MIN_LIMIT, "max": happ_limit.MAX_LIMIT}}


@router.get("/api/admin/users/by-public-id/{public_id}")
async def users_get_by_public_id(public_id: str, request: Request):
    """Look up one current primary subscription by the six-character profile ID."""
    _auth(request)
    if _test_environment.get():
        raise HTTPException(404, "Not found")
    code = public_id.strip().upper()
    if not re.fullmatch(r"[A-HJ-NP-Z2-9]{6}", code):
        raise HTTPException(422, "Введите шестисимвольный ID подписки")

    pool = await state.db()
    async with pool.acquire() as conn:
        # Release expired or disabled IDs before lookup, so an old profile ID
        # cannot continue to resolve after its subscription has ended.
        await _release_expired_public_ids(conn)
        row = await conn.fetchrow("""SELECT u.id,u.public_id,u.name,u.note,u.status,u.enabled,
                u.created_at,u.expires_at,u.last_synced_at,u.last_sync_error,
                (a.user_id IS NOT NULL) AS activation_enabled,a.device_limit AS activation_limit,
                a.activated_at AS activation_at,
                (SELECT count(*) FROM activation_devices d WHERE d.user_id=u.id) AS activation_devices_count
            FROM users u LEFT JOIN subscription_activation a ON a.user_id=u.id
            WHERE u.public_id=$1""", code)
    if not row:
        raise HTTPException(404, "Активная подписка с таким ID не найдена")
    user = dict(row)
    expires_at = user["expires_at"]
    user["remaining_days"] = (None if expires_at is None else
        max(0, math.ceil((_expiry_utc(expires_at) - datetime.now(timezone.utc)).total_seconds() / 86400)))
    return {"user": user}


@router.get("/api/admin/test-activations")
async def test_activations_list(request: Request):
    _auth(request)
    if not _test_environment.get():
        raise HTTPException(404, "Not found")
    pool = await state.db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT u.id,u.name,u.status,u.enabled,u.expires_at,u.drive_file_id,
            a.token_nonce,a.device_limit,a.activated_at,
            (SELECT count(*) FROM test_activation_devices d WHERE d.user_id=a.user_id
                AND d.encrypted_link IS NOT NULL) AS import_link_count
            FROM test_subscription_activation a JOIN users u ON u.id=a.user_id
            ORDER BY u.created_at,u.id""")
        devices = await conn.fetch("""SELECT d.user_id,d.device_hash,d.platform,d.browser,d.language,
            (d.encrypted_link IS NOT NULL) AS import_link_ready,
            d.registered_at,d.last_seen_at FROM test_activation_devices d
            JOIN test_subscription_activation a ON a.user_id=d.user_id
            ORDER BY d.registered_at,d.device_hash""")
    devices_by_user: dict[int, list[dict]] = {}
    for row in devices:
        item = _serialize(row)
        item.pop("device_hash", None)
        item["platform_label"] = TEST_ACTIVATION_PLATFORMS.get(row["platform"], "Другое устройство")
        devices_by_user.setdefault(int(row["user_id"]), []).append(item)
    result = []
    base = str(request.base_url).rstrip("/")
    for row in rows:
        user = _serialize(row)
        user["activation_id"] = _test_activation_id(int(row["id"]))
        user["activation_url"] = _test_activation_url(int(row["id"]), row["token_nonce"], base)
        user["devices"] = devices_by_user.get(int(row["id"]), [])
        user["registered_count"] = len(user["devices"])
        user["render_links_ready"] = int(row["import_link_count"] or 0)
        user.pop("token_nonce", None)
        result.append(user)
    return {"activations": result, "limit": {"min": 1, "max": 20}}


@router.put("/api/admin/users/{user_id}/test-activation/limit")
async def test_activation_limit_update(user_id: int, data: TestActivationLimitBody, request: Request):
    _auth(request, csrf=True)
    if not _test_environment.get():
        raise HTTPException(404, "Not found")
    pool = await state.db()
    changed = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            activation = await conn.fetchrow("""SELECT device_limit FROM test_subscription_activation
                WHERE user_id=$1 FOR UPDATE""", user_id)
            if not activation:
                raise HTTPException(404, "Тестовая активация не включена")
            changed = int(activation["device_limit"]) != data.limit
            if changed:
                # If the administrator lowers the cap, retain the oldest registrations
                # up to the new limit so a previously registered browser cannot bypass it.
                await conn.execute("""DELETE FROM test_activation_devices WHERE user_id=$1 AND device_hash IN (
                    SELECT device_hash FROM test_activation_devices WHERE user_id=$1
                    ORDER BY registered_at,device_hash OFFSET $2)""", user_id, data.limit)
                await conn.execute("""UPDATE test_subscription_activation SET device_limit=$2,
                    updated_at=now() WHERE user_id=$1""", user_id, data.limit)
    return {"ok": True, "limit": data.limit, "changed": changed}


@router.get("/api/admin/activations")
async def activations_list(request: Request):
    _auth(request)
    scope = _activation_scope()
    activation_table = str(scope["activation_table"])
    devices_table = str(scope["devices_table"])
    pool = await state.db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""SELECT u.id,u.name,u.status,u.enabled,u.expires_at,u.drive_file_id,
            a.token_nonce,a.device_limit,a.activated_at,a.drive_ready_at,
            (SELECT count(*) FROM {devices_table} d WHERE d.user_id=a.user_id
                AND d.encrypted_link IS NOT NULL) AS import_link_count
            FROM {activation_table} a JOIN users u ON u.id=a.user_id
            ORDER BY u.created_at,u.id""")
        devices = await conn.fetch(f"""SELECT d.user_id,d.device_hash,d.platform,d.browser,d.language,
            (d.encrypted_link IS NOT NULL) AS import_link_ready,d.registered_at,d.last_seen_at
            FROM {devices_table} d JOIN {activation_table} a ON a.user_id=d.user_id
            ORDER BY d.registered_at,d.device_hash""")
    devices_by_user: dict[int, list[dict]] = {}
    test_mode = bool(scope["test_mode"])
    for row in devices:
        item = _serialize(row)
        if test_mode:
            item.pop("device_hash", None)
        else:
            item["device_id"] = item.pop("device_hash", None)
        item["platform_label"] = TEST_ACTIVATION_PLATFORMS.get(row["platform"], "Другое устройство")
        devices_by_user.setdefault(int(row["user_id"]), []).append(item)
    result = []
    base = str(request.base_url).rstrip("/")
    for row in rows:
        user = _serialize(row)
        user["activation_id"] = _activation_id(int(row["id"]), test_mode=test_mode)
        user["activation_url"] = _activation_url(int(row["id"]), row["token_nonce"], base,
                                                   test_mode=test_mode)
        user["devices"] = devices_by_user.get(int(row["id"]), [])
        user["registered_count"] = len(user["devices"])
        user["render_links_ready"] = int(row["import_link_count"] or 0)
        user.pop("token_nonce", None)
        result.append(user)
    return {"activations": result, "limit": {"min": 1, "max": 20},
            "test_mode": test_mode}


@router.put("/api/admin/users/{user_id}/activation/limit")
async def activation_limit_update(user_id: int, data: TestActivationLimitBody, request: Request):
    _auth(request, csrf=True)
    scope = _activation_scope()
    activation_table = str(scope["activation_table"])
    devices_table = str(scope["devices_table"])
    pool = await state.db()
    changed = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            activation = await conn.fetchrow(
                f"SELECT device_limit FROM {activation_table} WHERE user_id=$1 FOR UPDATE", user_id)
            if not activation:
                raise HTTPException(404, "Render-активация не включена для этой подписки")
            changed = int(activation["device_limit"]) != data.limit
            if changed:
                await conn.execute(f"""DELETE FROM {devices_table} WHERE user_id=$1 AND device_hash IN (
                    SELECT device_hash FROM {devices_table} WHERE user_id=$1
                    ORDER BY registered_at,device_hash OFFSET $2)""", user_id, data.limit)
                await conn.execute(f"UPDATE {activation_table} SET device_limit=$2,updated_at=now() WHERE user_id=$1",
                                   user_id, data.limit)
    return {"ok": True, "limit": data.limit, "changed": changed}


@router.post("/api/admin/users/{user_id}/activation/reset")
async def activation_reset(user_id: int, request: Request, background_tasks: BackgroundTasks):
    _auth(request, csrf=True)
    scope = _activation_scope()
    test_mode = bool(scope["test_mode"])
    activation_table = str(scope["activation_table"])
    devices_table = str(scope["devices_table"])
    if test_mode:
        nonce, token_hash = _new_test_activation_credentials(user_id)
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if test_mode:
                row = await conn.fetchrow(f"""UPDATE {activation_table} SET token_nonce=$2,token_hash=$3,
                    activated_at=NULL,drive_ready_at=NULL,updated_at=now()
                    WHERE user_id=$1 RETURNING user_id""", user_id, nonce, token_hash)
            else:
                row = await conn.fetchrow("""UPDATE subscription_activation SET activated_at=NULL,drive_ready_at=NULL,
                    drive_generation=drive_generation+1,updated_at=now()
                    WHERE user_id=$1 RETURNING user_id,token_nonce""", user_id)
                if row:
                    nonce = row["token_nonce"]
            if row:
                await conn.execute(f"DELETE FROM {devices_table} WHERE user_id=$1", user_id)
                if test_mode:
                    await conn.execute("""UPDATE users SET device_limit=NULL,happ_install_code=NULL,limited_link=NULL,
                        device_limit_status='none',device_limit_error=NULL,updated_at=now() WHERE id=$1""", user_id)
    if not row:
        raise HTTPException(404, "Render-активация не включена для этой подписки")
    activation_url = _activation_url(user_id, nonce, str(request.base_url).rstrip("/"), test_mode=test_mode)
    if test_mode:
        drive_sync = await state.request_drive_sync([user_id])
        return {"ok": True, "activation_id": _activation_id(user_id, test_mode=True),
                "activation_url": activation_url, "drive_sync": drive_sync}
    background_tasks.add_task(state.push_drive_sync, [user_id])
    return {"ok": True, "activation_id": _main_activation_id(user_id),
            "activation_url": activation_url, "registered_count": 0,
            "drive_sync": {"ok": True, "pending": True,
                           "message": "Ссылка сохранена; обновление профиля Google Drive запущено в фоне."}}


async def activation_device_delete(user_id: int, device_hash: str, request: Request,
                                   background_tasks: BackgroundTasks):
    _auth(request, csrf=True)
    if _test_environment.get():
        raise HTTPException(404, "Not found")
    if not re.fullmatch(r"[a-f0-9]{64}", device_hash):
        raise HTTPException(404, "Устройство не найдено")
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            activation = await conn.fetchval(
                "SELECT user_id FROM subscription_activation WHERE user_id=$1 FOR UPDATE", user_id)
            if activation is None:
                raise HTTPException(404, "Активация не найдена")
            deleted = await conn.fetchval("""DELETE FROM activation_devices
                WHERE user_id=$1 AND device_hash=$2 RETURNING device_hash""", user_id, device_hash)
            if deleted is None:
                raise HTTPException(404, "Устройство не найдено")
            remaining = int(await conn.fetchval(
                "SELECT count(*) FROM activation_devices WHERE user_id=$1", user_id) or 0)
            needs_drive_sync = remaining == 0
            if needs_drive_sync:
                await conn.execute("""UPDATE subscription_activation SET activated_at=NULL,drive_ready_at=NULL,
                    drive_generation=drive_generation+1,updated_at=now()
                    WHERE user_id=$1""", user_id)
    if needs_drive_sync:
        background_tasks.add_task(state.push_drive_sync, [user_id])
    return {"ok": True, "removed": True, "registered_count": remaining,
            "drive_sync": {"ok": True, "pending": needs_drive_sync,
                           "message": "Профиль Google Drive обновляется." if needs_drive_sync else None}}


@router.post("/api/admin/users/{user_id}/activation/retry")
async def activation_retry(user_id: int, request: Request, background_tasks: BackgroundTasks):
    _auth(request, csrf=True)
    scope = _activation_scope()
    activation_table = str(scope["activation_table"])
    pool = await state.db()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(f"SELECT EXISTS(SELECT 1 FROM {activation_table} WHERE user_id=$1)", user_id)
    if not exists:
        raise HTTPException(404, "Render-активация не включена для этой подписки")
    if not scope["test_mode"]:
        background_tasks.add_task(state.push_drive_sync, [user_id])
        return {"ok": True, "message": "Синхронизация Google Drive запущена в фоне."}
    delivery = await state.request_drive_sync([user_id])
    if delivery.get("ok") and not delivery.get("pending") and int(delivery.get("processed") or 0) > 0:
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE {activation_table} SET drive_ready_at=COALESCE(drive_ready_at,now()),updated_at=now() WHERE user_id=$1",
                               user_id)
    return {"ok": bool(delivery.get("ok")), "message": delivery.get("message"), "drive_sync": delivery}


@router.get("/api/admin/users/{user_id}/activation-qr.svg")
async def activation_qr(user_id: int, request: Request):
    _auth(request)
    scope = _activation_scope()
    test_mode = bool(scope["test_mode"])
    activation_table = str(scope["activation_table"])
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT token_nonce FROM {activation_table} WHERE user_id=$1", user_id)
    if not row:
        raise HTTPException(404, "Render-активация не включена для этой подписки")
    activation_url = _activation_url(user_id, row["token_nonce"], str(request.base_url).rstrip("/"),
                                     test_mode=test_mode)
    svg = _drive_qr_svg(activation_url)
    return Response(content=svg, media_type="image/svg+xml", headers={
        "Cache-Control": "no-store", "Content-Disposition": "inline; filename=activation-qr.svg"})


@router.post("/api/admin/users")
async def users_create(data: UserBody, request: Request):
    _auth(request, csrf=True)
    test_mode = _test_environment.get()
    if not test_mode and (data.test_activation_enabled is not None or data.test_activation_limit is not None):
        raise HTTPException(422, "Подтверждение браузера доступно только в тестовой панели")
    if test_mode and (data.activation_enabled is not None or data.activation_limit is not None):
        raise HTTPException(422, "Основная Render-активация доступна только в основной панели")
    if data.title_override and any(ord(c) < 32 for c in data.title_override):
        raise HTTPException(422, "Заголовок профиля не должен содержать переносы строк")
    pool = await state.db()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM users WHERE status='active'")
        if count >= 30:
            raise HTTPException(409, "Достигнут лимит панели: максимум 30 пользователей")
        slug = _slugify(data.name)
        settings = await _settings(conn)
        hide_traffic = (data.hide_traffic_usage if data.hide_traffic_usage is not None
                        else bool(settings.get("hide_traffic_for_new_users", False)))
        include_sas = bool(data.include_sas if data.include_sas is not None
                           else settings.get("include_sas_for_new_users", True))
        device_limit = data.device_limit if "device_limit" in data.model_fields_set else settings.get("device_limit_for_new_users")
        if test_mode and data.test_activation_enabled:
            if data.test_activation_limit is None:
                raise HTTPException(422, "Укажите лимит регистрации через Render от 1 до 20")
            # This limit belongs to the isolated Render activation flow. It must
            # not create or mutate a Happ provider counter.
            device_limit = None
        if not test_mode and data.activation_enabled:
            if data.activation_limit is None:
                raise HTTPException(422, "Укажите лимит Render-регистраций от 1 до 20")
            device_limit = None
        if device_limit is not None:
            try:
                if isinstance(device_limit, bool) or (isinstance(device_limit, float) and not device_limit.is_integer()):
                    raise ValueError
                device_limit = int(device_limit)
            except (TypeError, ValueError, OverflowError):
                raise HTTPException(422, f"Лимит устройств должен быть целым числом от {happ_limit.MIN_LIMIT} до {happ_limit.MAX_LIMIT}")
            if not happ_limit.MIN_LIMIT <= device_limit <= happ_limit.MAX_LIMIT:
                raise HTTPException(422, f"Лимит устройств должен быть целым числом от {happ_limit.MIN_LIMIT} до {happ_limit.MAX_LIMIT}")
        device_status = "waiting" if device_limit is not None else "none"
        row = await conn.fetchrow("""INSERT INTO users(name,slug,note,enabled,expires_at,title_override,traffic_limit_bytes,hide_traffic_usage,include_sas,device_limit,device_limit_status)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""", data.name.strip(), slug, data.note, data.enabled,
            _expiry_utc(data.expires_at), data.title_override, data.traffic_limit_bytes, hide_traffic, include_sas,
            device_limit, device_status)
        if _test_environment.get() and data.geo_device_limit is not None:
            await conn.execute("INSERT INTO test_geo_state(user_id,geo_device_limit) VALUES($1,$2)", row["id"], data.geo_device_limit)
        if test_mode and data.test_activation_enabled:
            nonce, token_hash = _new_test_activation_credentials(int(row["id"]))
            await conn.execute("""INSERT INTO test_subscription_activation(user_id,token_nonce,token_hash,device_limit)
                VALUES($1,$2,$3,$4)""", row["id"], nonce, token_hash, data.test_activation_limit)
        if not test_mode and data.activation_enabled:
            nonce, token_hash = _main_activation_credentials(int(row["id"]))
            await conn.execute("""INSERT INTO subscription_activation(user_id,token_nonce,token_hash,device_limit)
                VALUES($1,$2,$3,$4)""", row["id"], nonce, token_hash, data.activation_limit)
    if row["device_limit"] is not None and not (test_mode and data.test_activation_enabled):
        _schedule_limited_link(row["id"])
    drive_sync = await state.request_drive_sync([row["id"]])
    return {"user": _serialize(row), "drive_sync": drive_sync}


@router.put("/api/admin/users/{user_id}")
async def users_update(user_id: int, data: UserBody, request: Request):
    _auth(request, csrf=True)
    test_mode = _test_environment.get()
    if not test_mode and (data.test_activation_enabled is not None or data.test_activation_limit is not None):
        raise HTTPException(422, "Подтверждение браузера доступно только в тестовой панели")
    if test_mode and (data.activation_enabled is not None or data.activation_limit is not None):
        raise HTTPException(422, "Основная Render-активация доступна только в основной панели")
    if data.title_override and any(ord(c) < 32 for c in data.title_override):
        raise HTTPException(422, "Заголовок профиля не должен содержать переносы строк")
    pool = await state.db()
    async with pool.acquire() as conn:
        await _release_expired_public_ids(conn)
        old_limit = await conn.fetchrow("SELECT slug,drive_file_id,device_limit FROM users WHERE id=$1 AND status='active'", user_id)
        if not old_limit:
            raise HTTPException(404, "Пользователь не найден")
        limit_supplied = "device_limit" in data.model_fields_set
        effective_device_limit = data.device_limit
        if test_mode and data.test_activation_enabled:
            if data.test_activation_limit is None:
                raise HTTPException(422, "Укажите лимит регистрации через Render от 1 до 20")
            effective_device_limit = None
            limit_supplied = True
        if not test_mode and data.activation_enabled:
            if data.activation_limit is None:
                raise HTTPException(422, "Укажите лимит Render-регистраций от 1 до 20")
            effective_device_limit = None
            limit_supplied = True
        if (data.test_activation_enabled if test_mode else data.activation_enabled) and (
                old_limit["slug"] == "legacy" or old_limit["drive_file_id"] == LEGACY_DRIVE_FILE_ID):
            raise HTTPException(409, "Для основной (legacy) подписки Render-активация недоступна")
        if limit_supplied and (old_limit["slug"] == "legacy" or old_limit["drive_file_id"] == LEGACY_DRIVE_FILE_ID) and effective_device_limit is not None:
            raise HTTPException(409, "Основная (legacy) подписка всегда остаётся без лимита устройств")
        limit_changed = limit_supplied and effective_device_limit != old_limit["device_limit"]
        clear_provider_limit_state = (limit_changed or
            bool(test_mode and data.test_activation_enabled) or
            bool(not test_mode and data.activation_enabled))
        row = await conn.fetchrow("""UPDATE users SET name=$2,note=$3,enabled=$4,expires_at=$5,
            public_id=CASE WHEN $4=FALSE OR ($5 IS NOT NULL AND $5<=now()) THEN NULL ELSE public_id END,
            title_override=$6,
            traffic_limit_bytes=$7,
            hide_traffic_usage=CASE WHEN slug='legacy' OR drive_file_id=$9 THEN FALSE ELSE COALESCE($8,hide_traffic_usage) END,
            include_sas=COALESCE($10,include_sas),
            device_limit=CASE WHEN $11 THEN $12 ELSE device_limit END,
            happ_install_code=CASE WHEN $13 THEN NULL ELSE happ_install_code END,
            limited_link=CASE WHEN $13 THEN NULL ELSE limited_link END,
            device_limit_status=CASE WHEN $13 THEN CASE WHEN $12 IS NULL THEN 'none' ELSE 'waiting' END ELSE device_limit_status END,
            device_limit_error=CASE WHEN $13 THEN NULL ELSE device_limit_error END,
            updated_at=now() WHERE id=$1 AND status='active' RETURNING *""", user_id,
            data.name.strip(), data.note, data.enabled, _expiry_utc(data.expires_at), data.title_override,
            data.traffic_limit_bytes, data.hide_traffic_usage, LEGACY_DRIVE_FILE_ID, data.include_sas,
            limit_supplied, effective_device_limit, clear_provider_limit_state)
        if _test_environment.get() and "geo_device_limit" in data.model_fields_set:
            geo_state = await conn.fetchrow("SELECT geo_device_limit FROM test_geo_state WHERE user_id=$1", user_id)
            previous_geo_limit = geo_state["geo_device_limit"] if geo_state else None
            if previous_geo_limit != data.geo_device_limit:
                await conn.execute("DELETE FROM test_geo_state WHERE user_id=$1", user_id)
                if data.geo_device_limit is not None:
                    await conn.execute("INSERT INTO test_geo_state(user_id,geo_device_limit) VALUES($1,$2)",
                                       user_id, data.geo_device_limit)
        if test_mode and "test_activation_enabled" in data.model_fields_set:
            if data.test_activation_enabled:
                if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM test_subscription_activation WHERE user_id=$1)", user_id):
                    nonce, token_hash = _new_test_activation_credentials(user_id)
                    await conn.execute("""INSERT INTO test_subscription_activation(user_id,token_nonce,token_hash,device_limit)
                        VALUES($1,$2,$3,$4)""", user_id, nonce, token_hash, data.test_activation_limit)
                else:
                    if "test_activation_limit" in data.model_fields_set:
                        current_limit = await conn.fetchval(
                            "SELECT device_limit FROM test_subscription_activation WHERE user_id=$1 FOR UPDATE", user_id)
                        if current_limit is not None and data.test_activation_limit < int(current_limit):
                            await conn.execute("""DELETE FROM test_activation_devices WHERE user_id=$1 AND device_hash IN (
                                SELECT device_hash FROM test_activation_devices WHERE user_id=$1
                                ORDER BY registered_at,device_hash OFFSET $2)""", user_id, data.test_activation_limit)
                        await conn.execute("""UPDATE test_subscription_activation SET device_limit=$2,updated_at=now()
                            WHERE user_id=$1""", user_id, data.test_activation_limit)
            else:
                await conn.execute("DELETE FROM test_subscription_activation WHERE user_id=$1", user_id)
        if not test_mode and "activation_enabled" in data.model_fields_set:
            if data.activation_enabled:
                current_activation = await conn.fetchrow(
                    "SELECT device_limit FROM subscription_activation WHERE user_id=$1 FOR UPDATE", user_id)
                if not current_activation:
                    nonce, token_hash = _main_activation_credentials(user_id)
                    await conn.execute("""INSERT INTO subscription_activation(user_id,token_nonce,token_hash,device_limit)
                        VALUES($1,$2,$3,$4)""", user_id, nonce, token_hash, data.activation_limit)
                else:
                    if data.activation_limit < int(current_activation["device_limit"]):
                        await conn.execute("""DELETE FROM activation_devices WHERE user_id=$1 AND device_hash IN (
                            SELECT device_hash FROM activation_devices WHERE user_id=$1
                            ORDER BY registered_at,device_hash OFFSET $2)""", user_id, data.activation_limit)
                    await conn.execute("""UPDATE subscription_activation SET device_limit=$2,updated_at=now()
                        WHERE user_id=$1""", user_id, data.activation_limit)
            else:
                await conn.execute("DELETE FROM subscription_activation WHERE user_id=$1", user_id)
    if not row: raise HTTPException(404, "Пользователь не найден")
    if row["device_limit"] is not None and not (test_mode and data.test_activation_enabled):
        _schedule_limited_link(user_id)
    drive_sync = await state.request_drive_sync([user_id])
    return {"user": _serialize(row), "drive_sync": drive_sync}


@router.delete("/api/admin/users/{user_id}")
async def users_delete(user_id: int, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE users SET status='pending_delete',enabled=FALSE,public_id=NULL,updated_at=now() WHERE id=$1 RETURNING id", user_id)
    if not row: raise HTTPException(404, "Пользователь не найден")
    drive_sync = await state.request_drive_sync([user_id])
    return {"ok": True, "status": "pending_delete", "drive_sync": drive_sync}


@router.get("/api/admin/users/{user_id}/preview")
async def users_preview(user_id: int, request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        settings = await _settings(conn)
        user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        if not user: raise HTTPException(404, "Пользователь не найден")
    body, headers, _ = await state.fetch_upstream(allow_sas_failure=not _user_needs_sas(user))
    async with pool.acquire() as conn:
        servers = await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
        content = await build_manifest_content(conn, user, body, headers, settings, servers=servers,
                                               public_base_url=str(request.base_url).rstrip("/"))
    return PlainTextResponse(content, headers={"Cache-Control": "no-store"})


def _drive_subscription_link(file_id: str) -> str:
    api_key = os.getenv("DRIVE_API_KEY", "")
    if not api_key:
        raise HTTPException(503, "DRIVE_API_KEY не задан")
    from urllib.parse import urlencode
    query = urlencode({"key": api_key, "alt": "media"})
    return f"https://www.googleapis.com/drive/v3/files/{file_id}?{query}"


_happ_user_locks: dict[int, asyncio.Lock] = {}


async def _set_device_limit_error(user_id: int, limit: int, message: str) -> None:
    pool = await state.db()
    async with pool.acquire() as conn:
        await conn.execute("""UPDATE users SET device_limit_status='error',device_limit_error=$3,updated_at=now()
            WHERE id=$1 AND device_limit=$2 AND status='active' AND enabled=TRUE""", user_id, limit, message[:500])


async def _create_limited_link(user_id: int, *, rebuild: bool = False) -> dict:
    lock = _happ_user_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        pool = await state.db()
        async with pool.acquire() as conn:
            user = await conn.fetchrow("""SELECT id,slug,status,enabled,expires_at,drive_file_id,device_limit,
                device_limit_status,happ_install_code,limited_link FROM users WHERE id=$1""", user_id)
        if not user:
            raise HTTPException(404, "Пользователь не найден")
        if user["slug"] == "legacy" or user["drive_file_id"] == LEGACY_DRIVE_FILE_ID:
            raise HTTPException(409, "Основная (legacy) подписка всегда остаётся без лимита устройств")
        limit = user["device_limit"]
        if limit is None:
            raise HTTPException(409, "Для подписки не задан лимит устройств")
        if user["status"] != "active" or not user["enabled"]:
            raise HTTPException(409, "Отключённая или удаляемая подписка не получает новую ссылку")
        expires_at = user["expires_at"]
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            raise HTTPException(409, "Срок подписки истёк; новая ссылка не создавалась")
        if not user["drive_file_id"]:
            async with pool.acquire() as conn:
                await conn.execute("""UPDATE users SET device_limit_status='waiting',
                    device_limit_error='Ожидается создание файла подписки в Google Drive'
                    WHERE id=$1 AND device_limit=$2 AND status='active'""", user_id, limit)
            raise HTTPException(409, "Файл подписки ещё не создан в Google Drive")

        existing_code = user["happ_install_code"]
        if rebuild and not existing_code:
            raise HTTPException(409, "Счётчик Happ ещё не создан; сначала повторите создание ссылки")
        provider_code = os.getenv("HAPP_PROVIDER_CODE", "").strip()
        auth_key = os.getenv("HAPP_AUTH_KEY", "").strip()
        if not happ_limit.is_configured(provider_code, auth_key):
            error = "Задайте HAPP_PROVIDER_CODE и HAPP_AUTH_KEY в переменных Render"
            await _set_device_limit_error(user_id, limit, error)
            raise HTTPException(503, error)

        try:
            drive_url = _drive_subscription_link(user["drive_file_id"])
            async with pool.acquire() as conn:
                await conn.execute("""UPDATE users SET device_limit_status='waiting',device_limit_error=NULL
                    WHERE id=$1 AND device_limit=$2 AND status='active' AND enabled=TRUE""", user_id, limit)
            async with _happ_client() as client:
                code = existing_code
                if not code:
                    code = await happ_limit.create_install_code(client, provider_code, auth_key, int(limit))
                    async with pool.acquire() as conn:
                        saved = await conn.fetchrow("""UPDATE users SET happ_install_code=$3,device_limit_status='waiting',
                            device_limit_error=NULL,updated_at=now() WHERE id=$1 AND device_limit=$2 AND status='active'
                            AND enabled=TRUE AND drive_file_id=$4 RETURNING id""", user_id, limit, code, user["drive_file_id"])
                    if not saved:
                        raise HTTPException(409, "Настройки подписки изменились во время создания ссылки")
                encrypted = await happ_limit.encrypt_link(client, happ_limit.limited_source_url(drive_url, code))
            async with pool.acquire() as conn:
                saved = await conn.fetchrow("""UPDATE users SET happ_install_code=$3,limited_link=$4,
                    device_limit_status='ready',device_limit_error=NULL,updated_at=now()
                    WHERE id=$1 AND device_limit=$2 AND status='active' AND enabled=TRUE AND drive_file_id=$5
                    RETURNING id""", user_id, limit, code, encrypted, user["drive_file_id"])
            if not saved:
                raise HTTPException(409, "Настройки подписки изменились во время создания ссылки")
            log.info("Happ limited link ready: user=%d limit=%d reused-counter=%s", user_id, limit,
                     "yes" if existing_code else "no")
            return {"ok": True, "device_limit": limit, "limited_link": encrypted,
                    "device_limit_status": "ready", "device_limit_error": None}
        except (happ_limit.HappLimitError, HTTPException) as exc:
            message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            await _set_device_limit_error(user_id, limit, message)
            if isinstance(exc, happ_limit.HappLimitError):
                raise HTTPException(502, message) from None
            raise


async def _background_limited_link(user_id: int) -> None:
    try:
        await _create_limited_link(user_id)
    except (HTTPException, happ_limit.HappLimitError) as exc:
        # The status and safe reason are stored for the administrator; do not
        # log credentials, provider URLs, subscription links, or response bodies.
        log.info("Happ link is not ready: user=%d reason=%s", user_id, type(exc).__name__)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Happ background attempt failed: user=%d error=%s", user_id, type(exc).__name__)


def _schedule_limited_link(user_id: int) -> None:
    task = asyncio.create_task(_background_limited_link(user_id))
    state.happ_tasks.add(task)
    task.add_done_callback(state.happ_tasks.discard)


def _drive_qr_svg(link: str) -> str:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    code.add_data(link)
    code.make(fit=True)
    image = code.make_image(image_factory=SvgPathImage, fill_color="#111111", back_color="#ffffff")
    return image.to_string(encoding="unicode")


@router.get("/api/admin/users/{user_id}/drive-link")
async def drive_link(user_id: int, request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT drive_file_id FROM users WHERE id=$1", user_id)
    if not user: raise HTTPException(404, "Пользователь не найден")
    if not user["drive_file_id"]: return {"link": None, "pending": True}
    return {"link": _drive_subscription_link(user["drive_file_id"]), "pending": False}


@router.get("/api/admin/users/{user_id}/drive-qr.svg")
async def drive_qr(user_id: int, request: Request):
    """Return an authenticated SVG QR code for this user's ready Drive subscription."""
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT drive_file_id FROM users WHERE id=$1", user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if not user["drive_file_id"]:
        raise HTTPException(409, "Файл подписки ещё не создан в Drive")
    svg = _drive_qr_svg(_drive_subscription_link(user["drive_file_id"]))
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store", "Content-Disposition": "inline; filename=subscription-qr.svg"})


def _happ_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=happ_limit.REQUEST_TIMEOUT, follow_redirects=False)


async def _ensure_activation_import_link(user_id: int, cookie_value: str,
                                         fallback_base_url: str | None = None,
                                         *, test_mode: bool | None = None) -> tuple[str | None, str | None]:
    """Encrypt the registered slot's Drive (primary) or Render (test) URL."""
    context = _test_environment.set(_test_environment.get() if test_mode is None else test_mode)
    try:
        scope = _activation_scope()
        activation_table = str(scope["activation_table"])
        devices_table = str(scope["devices_table"])
        is_test = bool(scope["test_mode"])
        pool = await state.db()
        browser_hash = _activation_device_hash(cookie_value, test_mode=is_test)
        slot_token = _activation_slot_token(user_id, cookie_value, test_mode=is_test)
        # The primary activation flow now gives Happ the Drive URL directly.
        # Version the persisted link key so an already-cached Render proxy URL
        # is regenerated once; keep test activation's token behavior unchanged.
        link_token = f"drive-v1:{slot_token}" if not is_test else slot_token
        slot_token_hash = hashlib.sha256(link_token.encode()).hexdigest()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT d.subscription_token_hash,d.encrypted_link,
                u.drive_file_id,u.status,u.enabled,u.expires_at,a.drive_ready_at
                FROM {devices_table} d JOIN users u ON u.id=d.user_id
                JOIN {activation_table} a ON a.user_id=d.user_id
                WHERE d.user_id=$1 AND d.device_hash=$2""".format(
                    devices_table=devices_table, activation_table=activation_table), user_id, browser_hash)
        if not row:
            return None, "Сначала подтвердите это устройство на странице."
        if row["status"] != "active" or not row["enabled"] or (
                row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)):
            return None, ("Тестовая подписка отключена или срок её действия закончился."
                          if is_test else "Подписка отключена или срок её действия закончился.")
        if not row["drive_file_id"]:
            return None, "Подписка ещё готовится. Повторите подтверждение позже."
        if not row["drive_ready_at"]:
            return None, "Полная подписка ещё готовится. Повторите подтверждение позже."
        if row["subscription_token_hash"] == slot_token_hash and row["encrypted_link"]:
            if not is_test:
                async with pool.acquire() as conn:
                    await conn.execute("""UPDATE subscription_activation SET drive_ready_at=COALESCE(drive_ready_at,now())
                        WHERE user_id=$1""", user_id)
            return str(row["encrypted_link"]), None

        source_url = (_activation_slot_url(user_id, slot_token, fallback_base_url, test_mode=True)
                      if is_test else _drive_subscription_link(str(row["drive_file_id"])))
        try:
            async with _happ_client() as client:
                encrypted_link = await happ_limit.encrypt_link(client, source_url)
        except happ_limit.HappLimitError as exc:
            log.info("Activation link encryption unavailable: user=%d reason=%s", user_id, str(exc)[:120])
            return None, "Не удалось зашифровать индивидуальную ссылку. Нажмите подтверждение ещё раз позже."

        async with pool.acquire() as conn:
            saved = await conn.fetchrow(f"""UPDATE {devices_table} SET
                subscription_token_hash=$3,encrypted_link=$4,last_seen_at=now()
                WHERE user_id=$1 AND device_hash=$2 RETURNING user_id""",
                user_id, browser_hash, slot_token_hash, encrypted_link)
            if saved and not is_test:
                await conn.execute("""UPDATE subscription_activation SET drive_ready_at=COALESCE(drive_ready_at,now()),
                    updated_at=now() WHERE user_id=$1""", user_id)
        if not saved:
            return None, "Слот регистрации изменился. Обновите страницу активации."
        return encrypted_link, None
    finally:
        _test_environment.reset(context)


async def _ensure_test_device_import_link(user_id: int, cookie_value: str,
                                          fallback_base_url: str | None = None) -> tuple[str | None, str | None]:
    return await _ensure_activation_import_link(user_id, cookie_value, fallback_base_url, test_mode=True)


_main_activation_link_jobs: set[tuple[int, str]] = set()
_main_activation_link_errors: dict[tuple[int, str], str] = {}
_main_activation_drive_jobs: dict[tuple[int, int], asyncio.Task] = {}


def _schedule_main_activation_link(background_tasks: BackgroundTasks, user_id: int, cookie_value: str) -> None:
    device_hash = _main_activation_browser_hash(cookie_value)
    job_key = (int(user_id), device_hash)
    if job_key in _main_activation_link_jobs:
        return
    _main_activation_link_errors.pop(job_key, None)
    while len(_main_activation_link_errors) > 2048:
        _main_activation_link_errors.pop(next(iter(_main_activation_link_errors)))
    _main_activation_link_jobs.add(job_key)
    background_tasks.add_task(_prepare_main_activation_link, int(user_id), cookie_value, job_key)


async def _deliver_main_activation_profile(user_id: int, generation: int) -> str | None:
    """Push one activation generation to Drive; concurrent devices share this delivery."""
    context = _test_environment.set(False)
    try:
        pool = await state.db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT activated_at,drive_ready_at,drive_generation
                FROM subscription_activation WHERE user_id=$1""", user_id)
        if not row or row["activated_at"] is None or int(row["drive_generation"]) != generation:
            return "Подтверждение устройства изменилось. Откройте страницу активации заново."
        if row["drive_ready_at"] is not None:
            return None
        if not state.drive_push_configured:
            return "Не настроена синхронизация Google Drive. Обратитесь к администратору."

        # Share the primary queue with admin edits and the scheduled sync. A
        # direct Apps Script call here can collide with updateSub's Drive lock
        # and leave the confirmed profile stale until the next timer run.
        delivery_task = asyncio.create_task(state.request_drive_sync([user_id]))
        state.happ_tasks.add(delivery_task)
        delivery_task.add_done_callback(state.happ_tasks.discard)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""SELECT activated_at,drive_ready_at,drive_generation
                    FROM subscription_activation WHERE user_id=$1""", user_id)
            if not row or row["activated_at"] is None or int(row["drive_generation"]) != generation:
                return "Подтверждение устройства изменилось. Откройте страницу активации заново."
            if row["drive_ready_at"] is not None:
                return None
            if delivery_task.done():
                try:
                    delivery = delivery_task.result()
                except Exception as exc:
                    log.info("Activation Drive delivery failed: user=%d reason=%s", user_id, type(exc).__name__)
                    return "Не удалось обновить файл Google Drive. Повторите подтверждение позже."
                if not delivery.get("ok") and not delivery.get("pending"):
                    return "Не удалось обновить файл Google Drive. Проверьте синхронизацию и повторите попытку."
                if delivery.get("ok") and not delivery.get("pending"):
                    return "Google Drive не подтвердил обновление профиля. Обновите и повторно разверните Code.gs в Apps Script."
        return "Google Drive пока не подтвердил обновление профиля. Попробуйте снова через минуту."
    except Exception as exc:
        log.info("Activation Drive preparation failed: user=%d reason=%s", user_id, type(exc).__name__)
        return "Не удалось подготовить Google Drive. Повторите подтверждение позже."
    finally:
        _test_environment.reset(context)


async def _ensure_main_activation_profile(user_id: int, generation: int) -> str | None:
    job_key = (int(user_id), int(generation))
    task = _main_activation_drive_jobs.get(job_key)
    if task is None or task.done():
        task = asyncio.create_task(_deliver_main_activation_profile(*job_key))
        _main_activation_drive_jobs[job_key] = task
        state.happ_tasks.add(task)
        task.add_done_callback(state.happ_tasks.discard)

        def clear_finished(done_task: asyncio.Task) -> None:
            if _main_activation_drive_jobs.get(job_key) is done_task:
                _main_activation_drive_jobs.pop(job_key, None)

        task.add_done_callback(clear_finished)
    return await asyncio.shield(task)


async def _prepare_main_activation_link(user_id: int, cookie_value: str,
                                        job_key: tuple[int, str]) -> None:
    context = _test_environment.set(False)
    try:
        pool = await state.db()
        device_hash = _main_activation_browser_hash(cookie_value)

        async def current_slot():
            async with pool.acquire() as conn:
                return await conn.fetchrow("""SELECT a.activated_at,a.drive_ready_at,a.drive_generation,
                    u.status,u.enabled,u.expires_at,u.drive_file_id
                    FROM subscription_activation a
                    JOIN activation_devices d ON d.user_id=a.user_id
                    JOIN users u ON u.id=a.user_id
                    WHERE a.user_id=$1 AND d.device_hash=$2""", user_id, device_hash)

        row = await current_slot()
        if not row or row["activated_at"] is None:
            _main_activation_link_errors[job_key] = "Подтверждение устройства больше не активно. Откройте страницу активации заново."
            return
        if row["status"] != "active" or not row["enabled"] or (
                row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)):
            _main_activation_link_errors[job_key] = "Подписка отключена или срок её действия закончился."
            return
        if not row["drive_file_id"]:
            _main_activation_link_errors[job_key] = "Файл подписки ещё не создан. Обновите страницу позже."
            return

        if row["drive_ready_at"] is None:
            error = await _ensure_main_activation_profile(user_id, int(row["drive_generation"]))
            if error:
                _main_activation_link_errors[job_key] = error
                return
            row = await current_slot()
            if not row or row["activated_at"] is None or row["drive_ready_at"] is None:
                _main_activation_link_errors[job_key] = "Подтверждение устройства было сброшено. Откройте страницу активации заново."
                return

        _, error = await _ensure_activation_import_link(user_id, cookie_value, test_mode=False)
        if error:
            _main_activation_link_errors[job_key] = error
            log.info("Activation import link is not ready yet: user=%d reason=%s", user_id, error[:120])
    except Exception as exc:
        _main_activation_link_errors[job_key] = "Не удалось подготовить ссылку. Обновите страницу, чтобы повторить попытку."
        log.warning("Activation import link preparation failed: user=%d error=%s", user_id, type(exc).__name__)
    finally:
        _main_activation_link_jobs.discard(job_key)
        _test_environment.reset(context)


async def test_activation_reset(user_id: int, request: Request):
    _auth(request, csrf=True)
    if not _test_environment.get():
        raise HTTPException(404, "Not found")
    nonce, token_hash = _new_test_activation_credentials(user_id)
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""UPDATE test_subscription_activation SET token_nonce=$2,token_hash=$3,
                browser_hash=NULL,activated_at=NULL,drive_ready_at=NULL,updated_at=now() WHERE user_id=$1 RETURNING user_id""",
                user_id, nonce, token_hash)
            if row:
                await conn.execute("DELETE FROM test_activation_devices WHERE user_id=$1", user_id)
                await conn.execute("""UPDATE users SET device_limit=NULL,happ_install_code=NULL,limited_link=NULL,
                    device_limit_status='none',device_limit_error=NULL,updated_at=now() WHERE id=$1""", user_id)
    if not row:
        raise HTTPException(404, "Тестовое подтверждение не включено для этой подписки")
    drive_sync = await state.request_drive_sync([user_id])
    return {"ok": True, "test_activation_id": _test_activation_id(user_id),
            "test_activation_url": _test_activation_url(user_id, nonce, str(request.base_url).rstrip("/")),
            "drive_sync": drive_sync}


async def test_activation_retry(user_id: int, request: Request, background_tasks: BackgroundTasks):
    _auth(request, csrf=True)
    if not _test_environment.get():
        raise HTTPException(404, "Not found")
    pool = await state.db()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM test_subscription_activation WHERE user_id=$1)", user_id)
    if not exists:
        raise HTTPException(404, "Тестовое подтверждение не включено для этой подписки")
    background_tasks.add_task(state.push_drive_sync, [user_id])
    return {"ok": True, "message": "Повторная синхронизация тестового профиля с Google Drive запущена."}


async def test_activation_qr(user_id: int, request: Request):
    _auth(request)
    if not _test_environment.get():
        raise HTTPException(404, "Not found")
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""SELECT a.token_nonce FROM test_subscription_activation a
            JOIN users u ON u.id=a.user_id WHERE a.user_id=$1""", user_id)
    if not row:
        raise HTTPException(404, "Тестовое подтверждение не включено для этой подписки")
    activation_url = _test_activation_url(user_id, row["token_nonce"], str(request.base_url).rstrip("/"))
    svg = _drive_qr_svg(activation_url)
    return Response(content=svg, media_type="image/svg+xml", headers={
        "Cache-Control": "no-store", "Content-Disposition": "inline; filename=test-activation-qr.svg"})


@router.post("/api/admin/users/{user_id}/device-limit")
async def device_limit_action(user_id: int, data: DeviceLimitActionBody, request: Request):
    _auth(request, csrf=True)
    activation_table = str(_activation_scope()["activation_table"])
    pool = await state.db()
    async with pool.acquire() as conn:
        is_activation = await conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM {activation_table} WHERE user_id=$1)", user_id)
    if is_activation:
        section = "Активация · ТЕСТ" if _test_environment.get() else "Активация"
        raise HTTPException(409, f"Управляйте этим лимитом в разделе «{section}»")
    return await _create_limited_link(user_id, rebuild=data.action == "rebuild")


@router.delete("/api/admin/users/{user_id}/device-limit")
async def device_limit_clear(user_id: int, request: Request):
    _auth(request, csrf=True)
    activation_table = str(_activation_scope()["activation_table"])
    pool = await state.db()
    async with pool.acquire() as conn:
        if await conn.fetchval(f"SELECT EXISTS(SELECT 1 FROM {activation_table} WHERE user_id=$1)", user_id):
            section = "тестовой подписки" if _test_environment.get() else "подписки"
            raise HTTPException(409, f"Сначала отключите Render-активацию в настройках {section}")
        row = await conn.fetchrow("""UPDATE users SET device_limit=NULL,happ_install_code=NULL,limited_link=NULL,
            device_limit_status='none',device_limit_error=NULL,updated_at=now() WHERE id=$1 RETURNING id""", user_id)
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    # This table stores the install code but not Happ's numeric counter ID, so
    # clearing it locally cannot disable a link that has already been issued.
    return {"ok": True, "device_limit": None, "device_limit_status": "none"}


@router.get("/api/admin/users/{user_id}/limited-qr.svg")
async def limited_qr(user_id: int, request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT limited_link FROM users WHERE id=$1", user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if not user["limited_link"]:
        raise HTTPException(409, "Ссылка с лимитом ещё не создана")
    svg = _drive_qr_svg(user["limited_link"])
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store", "Content-Disposition": "inline; filename=limited-subscription-qr.svg"})


@router.post("/api/admin/drive/sync")
async def drive_sync_now(request: Request):
    _auth(request, csrf=True)
    return await state.request_drive_sync()


@router.get("/api/admin/servers")
async def servers_list(request: Request):
    _auth(request)
    if request.query_params.get("cached") == "1":
        body = state.combined_body or state.upstream_body
        if body is None:
            raise HTTPException(503, "Снимок источников ещё не загружен. Сначала обнови источники.")
    else:
        body, _, stale = await state.fetch_upstream()
        if stale:
            raise HTTPException(503, "Не удалось обновить список серверов из апстрима. Повторите позже.")
    pool = await state.db()
    async with pool.acquire() as conn:
        await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
        order_sql = "CASE WHEN source='sas' THEN 1 ELSE 0 END,sort_order,id"
        rows = await conn.fetch(f"SELECT * FROM servers WHERE present_upstream=TRUE ORDER BY {order_sql}")
    return {"servers": [_serialize(r) for r in rows]}


async def _refresh_server_catalog() -> tuple[list[dict], dict[str, int]]:
    fields = state._FEED_CACHE_FIELDS
    environment = "test" if _test_environment.get() else "main"
    cache = state._feed_caches[environment]
    previous = {key: cache[key] for key in fields}
    try:
        body, _, stale = await state.fetch_upstream(force=True, force_sas=True)
        if stale:
            raise HTTPException(502, "Не удалось обновить один из источников. Сохранён последний рабочий кэш; каталог и подписки не изменены.")
        pool = await state.db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                servers = await _sync_servers(
                    conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
                await _reset_server_order(conn, servers)
    except Exception:
        warnings = {key: cache[key] for key in (
            "upstream_attempt_at", "upstream_warning", "sas_attempt_at", "sas_warning")}
        cache.update(previous)
        cache.update(warnings)
        raise

    source_counts = {
        "secure_x": sum(server.get("source") != "sas" for server in servers),
        "sas": sum(server.get("source") == "sas" for server in servers),
    }
    log.info("Admin server catalog refreshed (environment=%s; Secure X=%d; SAS=%d)",
             environment, source_counts["secure_x"], source_counts["sas"])
    return servers, source_counts


@router.put("/api/admin/servers/{server_id}")
async def servers_update(server_id: str, data: ServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE servers SET custom_name=$2,hidden=$3,sort_order=$4 WHERE id=$1 AND present_upstream=TRUE RETURNING *", server_id, data.custom_name, data.hidden, data.sort_order)
    if not row: raise HTTPException(404, "Сервер не найден")
    return {"server": _serialize(row), "drive_sync": await state.request_drive_sync()}


class BulkServerBody(BaseModel):
    server_ids: list[str] = Field(min_length=1, max_length=500)
    hidden: bool


@router.post("/api/admin/servers/batch")
async def servers_batch(data: ServerBatchBody, request: Request):
    _auth(request, csrf=True)
    ids = [item.server_id for item in data.servers]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "В пакете есть повторяющиеся серверы")
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in data.servers:
                row = await conn.fetchrow("""UPDATE servers SET custom_name=$2,hidden=$3,sort_order=$4
                    WHERE id=$1 AND present_upstream=TRUE RETURNING id""",
                    item.server_id, item.custom_name, item.hidden, item.sort_order)
                if not row:
                    raise HTTPException(409, "Один из серверов уже отсутствует в источнике. Обновите каталог и повторите.")
    return {"ok": True, "updated": len(data.servers), "drive_sync": await state.request_drive_sync()}


@router.post("/api/admin/servers/bulk")
async def servers_bulk(data: BulkServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE servers SET hidden=$1 WHERE id=ANY($2::text[]) AND present_upstream=TRUE", data.hidden, data.server_ids)
    return {"ok": True, "updated": int(result.rsplit(" ", 1)[-1]), "drive_sync": await state.request_drive_sync()}


@router.post("/api/admin/servers/refresh")
async def servers_refresh(request: Request):
    _auth(request, csrf=True)
    servers, source_counts = await _refresh_server_catalog()
    return {"count": len(servers), "source_counts": source_counts, "order_reset": True,
            "drive_sync": await state.request_drive_sync()}


@router.post("/api/admin/cache/refresh")
async def admin_cache_refresh(request: Request):
    _auth(request, csrf=True)
    servers, source_counts = await _refresh_server_catalog()
    return {"ok": True, "count": len(servers), "source_counts": source_counts, "order_reset": True,
            "drive_sync": await state.request_drive_sync()}


@router.put("/api/admin/users/{user_id}/servers/{server_id}")
async def user_server_update(user_id: int, server_id: str, data: UserServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", user_id)
        if not exists: raise HTTPException(404, "Пользователь не найден")
        server_exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM servers WHERE id=$1 AND present_upstream=TRUE)", server_id)
        if not server_exists: raise HTTPException(404, "Сервер больше не находится в апстриме")
        await conn.execute("""INSERT INTO user_servers(user_id,server_id,hidden,custom_name) VALUES($1,$2,$3,$4)
            ON CONFLICT(user_id,server_id) DO UPDATE SET hidden=EXCLUDED.hidden,custom_name=EXCLUDED.custom_name""", user_id, server_id, data.hidden, data.custom_name)
    return {"ok": True, "drive_sync": await state.request_drive_sync([user_id])}


@router.post("/api/admin/users/{user_id}/servers/batch")
async def user_servers_batch(user_id: int, data: UserServerBatchBody, request: Request):
    _auth(request, csrf=True)
    ids = [item.server_id for item in data.servers]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "В пакете есть повторяющиеся серверы")
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", user_id):
                raise HTTPException(404, "Пользователь не найден")
            for item in data.servers:
                exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM servers WHERE id=$1 AND present_upstream=TRUE)", item.server_id)
                if not exists:
                    raise HTTPException(409, "Один из серверов отсутствует в источнике. Обновите список и повторите.")
                await conn.execute("""INSERT INTO user_servers(user_id,server_id,hidden,custom_name) VALUES($1,$2,$3,$4)
                    ON CONFLICT(user_id,server_id) DO UPDATE SET hidden=EXCLUDED.hidden,custom_name=EXCLUDED.custom_name""",
                    user_id, item.server_id, item.hidden, item.custom_name)
    return {"ok": True, "updated": len(data.servers), "drive_sync": await state.request_drive_sync([user_id])}


@router.get("/api/admin/users/{user_id}/servers")
async def user_servers_list(user_id: int, request: Request):
    _auth(request)
    body, _, stale = await state.fetch_upstream()
    if stale:
        raise HTTPException(503, "Не удалось обновить список серверов из апстрима. Повторите позже.")
    pool = await state.db()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", user_id):
            raise HTTPException(404, "Пользователь не найден")
        await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
        order_prefix = "CASE WHEN s.source='sas' THEN 1 ELSE 0 END,"
        rows = await conn.fetch(f"""SELECT s.id,s.original_name,s.custom_name AS global_name,s.hidden AS global_hidden,
            s.source,s.sort_order,s.present_upstream,us.hidden AS user_hidden,us.custom_name AS user_name
            FROM servers s LEFT JOIN user_servers us ON us.server_id=s.id AND us.user_id=$1
            WHERE s.present_upstream=TRUE ORDER BY {order_prefix}s.sort_order,s.id""", user_id)
    return {"servers": [_serialize(row) for row in rows]}


@router.get("/api/admin/settings")
async def settings_get(request: Request):
    _auth(request)
    test_mode = _test_environment.get()
    provider_id_env = "TEST_HAPP_PROVIDER_ID" if test_mode else "HAPP_PROVIDER_ID"
    profile_provider_id = os.getenv(provider_id_env, "").strip()
    test_profile_host = (urlsplit(_public_service_base_url(str(request.base_url).rstrip("/"))).hostname
                         if test_mode else None)
    if request.query_params.get("cached") != "1":
        try:
            await state.fetch_upstream()
        except HTTPException:
            # Keep all admin controls available during a temporary source outage.
            pass
    pool = await state.db()
    async with pool.acquire() as conn:
        sas_uris = []
        if state.sas_body is not None:
            try:
                sas_uris = [uri for uri in _extract_uris(_decode_subscription(state.sas_body))
                            if not is_excluded_sas_server(uri)]
            except (ValueError, UnicodeDecodeError):
                sas_uris = []
        return {"settings": await _settings(conn),
            "device_limit_bounds": {"min": happ_limit.MIN_LIMIT, "max": happ_limit.MAX_LIMIT},
            "happ": {"configured": happ_limit.is_configured(os.getenv("HAPP_PROVIDER_CODE"), os.getenv("HAPP_AUTH_KEY")),
                     "provider_id_configured": bool(re.fullmatch(r"[A-Za-z0-9]{8}", profile_provider_id)),
                     **({"subscription_host": test_profile_host} if test_mode else {})},
            "sas": {
            "configured": bool(os.getenv("SAS_SUBSCRIPTION_URL", "").strip()),
            "server_count": len(sas_uris),
            "last_success_at": state.sas_success_at,
            "warning": state.sas_warning,
        }}


@router.put("/api/admin/settings")
async def settings_put(data: SettingsBody, request: Request):
    _auth(request, csrf=True)
    values = dict(data.values)
    if "update_interval" in values:
        try: values["update_interval"] = max(1, min(168, int(values["update_interval"])))
        except Exception: raise HTTPException(422, "Интервал профиля должен быть числом от 1 до 168")
    if "default_traffic_limit_bytes" in values:
        traffic_limit = values["default_traffic_limit_bytes"]
        if traffic_limit is not None:
            try:
                traffic_limit = int(traffic_limit)
            except Exception:
                raise HTTPException(422, "Лимит трафика должен быть от 1 МБ до 1 ТБ или общим от провайдера")
            if traffic_limit < 1_000_000 or traffic_limit > 1_000_000_000_000:
                raise HTTPException(422, "Лимит трафика должен быть от 1 МБ до 1 ТБ")
            values["default_traffic_limit_bytes"] = traffic_limit
    if "profile_format" in values and (
            not isinstance(values["profile_format"], str) or values["profile_format"] not in {"url", "json"}):
        raise HTTPException(422, "Формат подписки должен быть url или json")
    if "device_limit_for_new_users" in values:
        limit = values["device_limit_for_new_users"]
        if limit in (None, ""):
            values["device_limit_for_new_users"] = None
        else:
            try:
                if isinstance(limit, bool) or (isinstance(limit, float) and not limit.is_integer()):
                    raise ValueError
                limit = int(limit)
            except (TypeError, ValueError):
                raise HTTPException(422, f"Лимит по умолчанию должен быть пустым или числом от {happ_limit.MIN_LIMIT} до {happ_limit.MAX_LIMIT}")
            if not happ_limit.MIN_LIMIT <= limit <= happ_limit.MAX_LIMIT:
                raise HTTPException(422, f"Лимит по умолчанию должен быть пустым или числом от {happ_limit.MIN_LIMIT} до {happ_limit.MAX_LIMIT}")
            values["device_limit_for_new_users"] = limit
    allowed = set(DEFAULT_SETTINGS)
    if set(values) - allowed: raise HTTPException(422, "Неизвестные настройки")
    string_limits = {"title": 200, "support_url": 2048, "web_page_url": 2048, "timezone": 128}
    for key, limit in string_limits.items():
        if key in values and (not isinstance(values[key], str) or len(values[key]) > limit or any(ord(c) < 32 for c in values[key])):
            raise HTTPException(422, f"Некорректное поле {key}")
    if values.get("timezone"):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(values["timezone"])
        except Exception:
            raise HTTPException(422, "Неизвестный часовой пояс")
    for key in ("support_url", "web_page_url"):
        if values.get(key):
            parsed = urlsplit(values[key])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise HTTPException(422, f"{key} должен быть HTTP(S)-адресом")
    if "hide_traffic_for_new_users" in values and not isinstance(values["hide_traffic_for_new_users"], bool):
        raise HTTPException(422, "hide_traffic_for_new_users должен быть логическим значением")
    if "include_sas_for_new_users" in values and not isinstance(values["include_sas_for_new_users"], bool):
        raise HTTPException(422, "include_sas_for_new_users должен быть логическим значением")
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in values.items():
                await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, json.dumps(value, ensure_ascii=False))
    drive_sync = await state.request_drive_sync() if set(values) - {
        "default_traffic_limit_bytes", "hide_traffic_for_new_users", "include_sas_for_new_users",
        "device_limit_for_new_users"
    } else None
    return {"settings": values, "drive_sync": drive_sync}


def _check_sync(request: Request):
    configured = os.getenv("SYNC_KEY", "")
    supplied = request.headers.get("x-sync-key", "")
    if not configured:
        raise HTTPException(503, "Синхронизация отключена: SYNC_KEY не задан")
    if not hmac.compare_digest(supplied.encode(), configured.encode()):
        raise HTTPException(401, "Не авторизовано")


def _safe_sync_error(message: str | None) -> str | None:
    if not message:
        return None
    value = str(message)
    for name in ("DATABASE_URL", "ADMIN_PASSWORD", "SESSION_SECRET", "SYNC_KEY", "DRIVE_API_KEY",
                 "SUBSCRIPTION_URL", "SAS_SUBSCRIPTION_URL", "HAPP_PROVIDER_CODE", "HAPP_AUTH_KEY"):
        secret = os.getenv(name, "")
        if secret:
            value = value.replace(secret, "[секрет скрыт]")
    value = re.sub(r"https?://\S+", "[адрес скрыт]", value, flags=re.I)
    value = re.sub(r"(?i)happ://crypt\S+", "[ссылка Happ скрыта]", value)
    value = re.sub(r"(?i)installid=[A-Za-z0-9]{12}", "installid=[скрыто]", value)
    value = re.sub(r"(?i)(?:vless|vmess|trojan|ss|hysteria2?|tuic)://\S+", "[сервер скрыт]", value)
    return value[:1000]


@router.get("/api/sync/manifest")
async def sync_manifest(request: Request):
    _check_sync(request)
    selected_ids = None
    selected = request.query_params.get("user_ids", "").strip()
    if selected:
        try:
            selected_ids = list(dict.fromkeys(int(part) for part in selected.split(",") if part))
        except ValueError:
            raise HTTPException(422, "Некорректный список пользователей")
        max_selected = 30 if _test_environment.get() else 500
        if not selected_ids or len(selected_ids) > max_selected or any(value <= 0 for value in selected_ids):
            raise HTTPException(422, "Некорректный список пользователей")

    pool = await state.db()
    query = "SELECT * FROM users WHERE id=ANY($1::bigint[]) ORDER BY id" if selected_ids else "SELECT * FROM users ORDER BY id"
    async with pool.acquire() as conn:
        await _release_expired_public_ids(conn)
        rows_before_fetch = await conn.fetch(query, selected_ids) if selected_ids else await conn.fetch(query)
        await conn.execute("INSERT INTO app_state(key,value) VALUES('last_script_request',to_jsonb(now())) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")

    # Revocation does not depend on the provider. In particular, a provider
    # outage must not leave a deleted user's old Drive profile alive.
    if not any(user["status"] != "pending_delete" for user in rows_before_fetch):
        manifest = [{"id": user["id"], "slug": user["slug"], "status": user["status"],
                     "enabled": user["enabled"], "drive_file_id": user["drive_file_id"],
                     "name": user["name"], "content": ""} for user in rows_before_fetch]
        log.info("Apps Script revocation manifest generated without upstream: users=%d", len(manifest))
        return {"users": manifest, "generated_at": datetime.now(timezone.utc).isoformat()}

    # A cold start or expired cache makes this request fetch the upstream itself.
    # If that fails, return only pending deletions so they can still be revoked;
    # active profiles remain untouched and the regular retry will refresh them.
    refresh_sources = request.query_params.get("refresh_sources") == "1"
    try:
        # The five-minute Apps Script poll bypasses the SAS cache so every
        # scheduled manifest request checks for newly posted Telegram servers.
        # Main admin edits use the warm provider snapshots instead of forcing
        # two external subscription downloads for every Drive write.
        needs_sas = any(_user_needs_sas(user) for user in rows_before_fetch)
        body, headers, _stale = await state.fetch_upstream(
            force_sas=bool(_test_environment.get() or refresh_sources), allow_sas_failure=not needs_sas)
    except HTTPException:
        activation_table = str(_activation_scope()["activation_table"])
        async with pool.acquire() as conn:
            if selected_ids:
                rows = await conn.fetch("SELECT * FROM users WHERE id=ANY($1::bigint[]) AND status='pending_delete' ORDER BY id", selected_ids)
                activation_rows = await conn.fetch(f"""SELECT u.* FROM users u
                    JOIN {activation_table} a ON a.user_id=u.id
                    WHERE u.id=ANY($1::bigint[]) AND u.status='active' AND u.enabled=TRUE
                      AND (u.expires_at IS NULL OR u.expires_at>now()) AND a.activated_at IS NULL ORDER BY u.id""", selected_ids)
            else:
                rows = await conn.fetch("SELECT * FROM users WHERE status='pending_delete' ORDER BY id")
                activation_rows = await conn.fetch(f"""SELECT u.* FROM users u
                    JOIN {activation_table} a ON a.user_id=u.id
                    WHERE u.status='active' AND u.enabled=TRUE AND (u.expires_at IS NULL OR u.expires_at>now())
                      AND a.activated_at IS NULL ORDER BY u.id""")
            activation_manifest = []
            if activation_rows:
                settings = await _settings(conn)
                for user in activation_rows:
                    content = await build_manifest_content(conn, user, b"", {}, settings, servers=[],
                        public_base_url=str(request.base_url).rstrip("/"))
                    activation_manifest.append({"id": user["id"], "slug": user["slug"], "status": user["status"],
                        "enabled": user["enabled"], "drive_file_id": user["drive_file_id"],
                        "name": user["name"], "content": content})
        if not rows and not activation_manifest:
            raise
        manifest = [{"id": user["id"], "slug": user["slug"], "status": user["status"],
                     "enabled": user["enabled"], "drive_file_id": user["drive_file_id"],
                     "name": user["name"], "content": ""} for user in rows]
        manifest.extend(activation_manifest)
        log.warning("Upstream unavailable; returning deletion/activation-only manifest: users=%d", len(manifest))
        return {"users": manifest, "partial": True, "generated_at": datetime.now(timezone.utc).isoformat()}

    async with pool.acquire() as conn:
        settings = await _settings(conn)
        servers = await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
        if selected_ids:
            rows = await conn.fetch("SELECT * FROM users WHERE id=ANY($1::bigint[]) ORDER BY id", selected_ids)
        else:
            rows = await conn.fetch("SELECT * FROM users ORDER BY id")
        activation_generations: dict[int, int] = {}
        if not _test_environment.get() and rows:
            generation_rows = await conn.fetch("""SELECT user_id,drive_generation FROM subscription_activation
                WHERE user_id=ANY($1::bigint[])""", [int(user["id"]) for user in rows])
            activation_generations = {int(item["user_id"]): int(item["drive_generation"])
                                      for item in generation_rows}
        manifest = []
        for user in rows:
            content = "" if user["status"] == "pending_delete" else await build_manifest_content(
                conn, user, body, headers, settings, servers,
                public_base_url=str(request.base_url).rstrip("/"))
            item = {"id": user["id"], "slug": user["slug"], "status": user["status"], "enabled": user["enabled"],
                    "drive_file_id": user["drive_file_id"], "name": user["name"], "content": content}
            generation = activation_generations.get(int(user["id"]))
            if generation is not None:
                item["activation_generation"] = generation
            manifest.append(item)
    log.info("Apps Script manifest generated: users=%d stale-cache=%s", len(manifest), "yes" if _stale else "no")
    return {"users": manifest, "generated_at": datetime.now(timezone.utc).isoformat()}


class SyncReport(BaseModel):
    user_id: int
    drive_file_id: str | None = Field(default=None, max_length=256)
    activation_generation: int | None = Field(default=None, ge=0)
    ok: bool
    error: str | None = Field(default=None, max_length=1000)


@router.post("/api/sync/report")
async def sync_report(data: SyncReport, request: Request, background_tasks: BackgroundTasks):
    _check_sync(request)
    pool = await state.db()
    activation_table = str(_activation_scope()["activation_table"])
    safe_error = _safe_sync_error(data.error) if not data.ok else None
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO app_state(key,value) VALUES('last_script_request',to_jsonb(now())) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")
        user = await conn.fetchrow(f"""SELECT id,status,device_limit,device_limit_status,
            (limited_link IS NOT NULL) AS limited_link_ready,
            EXISTS(SELECT 1 FROM {activation_table} a WHERE a.user_id=users.id) AS render_activation
            FROM users WHERE id=$1""", data.user_id)
        if not user: return {"ok": True, "ignored": True}
        if user["status"] == "pending_delete" and data.ok:
            await conn.execute("DELETE FROM users WHERE id=$1", data.user_id)
            return {"ok": True, "deleted": True}
        await conn.execute("""UPDATE users SET drive_file_id=COALESCE($2,drive_file_id),last_synced_at=CASE WHEN $3 THEN now() ELSE last_synced_at END,
            last_sync_error=$4,updated_at=now() WHERE id=$1""", data.user_id, data.drive_file_id, data.ok, safe_error)
        if (data.ok and not _test_environment.get() and user["render_activation"]
                and data.activation_generation is not None):
            await conn.execute("""UPDATE subscription_activation SET drive_ready_at=COALESCE(drive_ready_at,now())
                WHERE user_id=$1 AND activated_at IS NOT NULL AND drive_generation=$2""",
                data.user_id, data.activation_generation)
        elif (data.ok and not _test_environment.get() and user["render_activation"]
              and data.activation_generation is None):
            log.warning("Drive report omitted activation generation: user_id=%d", data.user_id)
        if not _test_environment.get():
            if data.ok:
                reported_at = time.time()
                state._drive_sync_reported_at[data.user_id] = reported_at
                if len(state._drive_sync_reported_at) > 2000:
                    cutoff = reported_at - 24 * 60 * 60
                    state._drive_sync_reported_at = {
                        user_id: timestamp for user_id, timestamp in state._drive_sync_reported_at.items()
                        if timestamp >= cutoff
                    }
                state.drive_push_last = max(state.drive_push_last or 0.0, reported_at)
                error_count = int(await conn.fetchval(
                    "SELECT count(*) FROM users WHERE last_sync_error IS NOT NULL AND status<>'pending_delete'"
                ) or 0)
                state.drive_push_error = (
                    f"У {int(error_count)} подписок есть ошибки синхронизации Google Drive"
                    if error_count else None
                )
            else:
                state.drive_push_error = (
                    f"Google Drive не обновил профиль пользователя {data.user_id}"
                    + (f": {safe_error}" if safe_error else "")
                )
    if (data.ok and user["device_limit"] is not None and not user["render_activation"]
            and (not user["limited_link_ready"] or user["device_limit_status"] != "ready")):
        _schedule_limited_link(data.user_id)
    return {"ok": True}


GEO_FILE_SOURCES = {
    "geoip.dat": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
    "geosite.dat": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
}


async def _sync_activated_test_subscription(user_id: int):
    token = _test_environment.set(True)
    try:
        delivery = await state.request_drive_sync([user_id])
        log.info("Test subscription geo activation: user_id=%d, drive_sync_ok=%s", user_id, bool(delivery.get("ok")))
    finally:
        _test_environment.reset(token)


async def _activation_get_current(activation_id: str, token: str, request: Request,
                                 background_tasks: BackgroundTasks | None = None):
    scope = _activation_scope()
    test_mode = bool(scope["test_mode"])
    activation_table = str(scope["activation_table"])
    devices_table = str(scope["devices_table"])
    cookie_name = str(scope["cookie_name"])
    cookie_path = str(scope["cookie_path"])
    wants_subscription = request.query_params.get("view") != "page" and _request_wants_subscription(request)
    activation_user_id = _activation_user_id(activation_id, test_mode=test_mode)
    valid_token = bool(re.fullmatch(r"[A-Za-z0-9_-]{40,48}", token))
    if not valid_token or activation_user_id is None:
        if wants_subscription:
            return PlainTextResponse(_test_activation_gate_profile(str(request.url), test_mode=test_mode),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
        return HTMLResponse(_test_activation_page("Ссылка активации недействительна",
            "Проверьте адрес или запросите новую ссылку у администратора.", "", "", tone="error", test_mode=test_mode),
            status_code=404, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        pool = await state.db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"""SELECT a.user_id,a.token_hash,a.token_nonce,a.activated_at,a.drive_ready_at,a.device_limit,
                u.name,u.status,u.enabled,u.expires_at,u.drive_file_id
                FROM {activation_table} a JOIN users u ON u.id=a.user_id
                WHERE a.token_hash=$1 AND a.user_id=$2""", token_hash, activation_user_id)
    except Exception as exc:
        log.info("Render activation lookup unavailable: user=%d reason=%s", activation_user_id, type(exc).__name__)
        if wants_subscription:
            return PlainTextResponse(_test_activation_gate_profile(str(request.url), "Профиль временно недоступен",
                                                                   test_mode=test_mode),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
        return HTMLResponse(_test_activation_page(
            "Тестовый профиль временно недоступен" if test_mode else "Профиль временно недоступен",
            "Не удалось проверить тестовую регистрацию. Обновите страницу через минуту." if test_mode
            else "Не удалось проверить регистрацию. Обновите страницу через минуту.",
            activation_id, token, tone="error", test_mode=test_mode), status_code=503,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    if not row:
        if wants_subscription:
            return PlainTextResponse(_test_activation_gate_profile(str(request.url), test_mode=test_mode),
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
        return HTMLResponse(_test_activation_page("Ссылка активации устарела",
            "Попросите администратора выдать новую ссылку.", activation_id, token, tone="error", test_mode=test_mode),
            status_code=404, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    # The same unique per-user URL is safe to paste into Happ: subscription
    # requests receive a valid gate profile, while normal browser navigation
    # receives the HTML confirmation page.
    if wants_subscription:
        activation_url = _activation_url(activation_user_id, row["token_nonce"], str(request.base_url).rstrip("/"),
                                         test_mode=test_mode)
        return PlainTextResponse(_test_activation_gate_profile(activation_url + "?view=page", row["name"],
                                                               test_mode=test_mode),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    cookie_value = request.cookies.get(cookie_name, "")
    set_cookie = not bool(re.fullmatch(r"[A-Za-z0-9_-]{30,64}", cookie_value))
    if set_cookie:
        cookie_value = secrets.token_urlsafe(32)
    browser_hash = _activation_device_hash(cookie_value, test_mode=test_mode)
    response_cookie = set_cookie
    known_device = None
    async with pool.acquire() as conn:
        if not set_cookie:
            known_device = await conn.fetchrow(f"""SELECT platform,subscription_token_hash,encrypted_link FROM {devices_table}
                WHERE user_id=$1 AND device_hash=$2""", activation_user_id, browser_hash)
        registered = int(await conn.fetchval(f"SELECT count(*) FROM {devices_table} WHERE user_id=$1",
                                             activation_user_id) or 0)
    known = bool(known_device)
    full = not known and registered >= int(row["device_limit"])
    if row["status"] != "active" or not row["enabled"] or (row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)):
        page = _test_activation_page("Подписка недоступна", "Эта подписка отключена или срок её действия закончился.",
                                     activation_id, token, tone="error", test_mode=test_mode)
    elif not row["drive_file_id"]:
        page = _test_activation_page("Профиль готовится", "Файл подписки ещё не создан. Откройте эту ссылку позже.",
                                     activation_id, token, tone="error", test_mode=test_mode)
    elif full:
        page = _test_activation_page("Лимит устройств достигнут",
            f"Зарегистрировано {registered} из {row['device_limit']} устройств. Новое устройство не подключено. Обратитесь к администратору, если нужно освободить слот.",
            activation_id, token, limit=int(row["device_limit"]), registered=registered, tone="error", test_mode=test_mode)
    else:
        csrf = _activation_csrf(token_hash, cookie_value, test_mode=test_mode)
        selected = known_device["platform"] if known_device else ""
        import_link = None
        import_error = None
        import_pending = False
        if known:
            if test_mode:
                import_link, import_error = await _ensure_activation_import_link(
                    activation_user_id, cookie_value, str(request.base_url).rstrip("/"), test_mode=True)
            else:
                slot_token = _main_slot_token(activation_user_id, cookie_value)
                current_link_hash = hashlib.sha256(f"drive-v1:{slot_token}".encode()).hexdigest()
                if (row["drive_ready_at"] is not None
                        and known_device["subscription_token_hash"] == current_link_hash
                        and known_device["encrypted_link"]):
                    import_link = str(known_device["encrypted_link"])
                elif background_tasks is not None:
                    _schedule_main_activation_link(background_tasks, activation_user_id, cookie_value)
                    import_pending = True
                else:
                    import_error = "Персональная ссылка готовится. Обновите страницу через несколько секунд."
        message = (f"Это устройство уже зарегистрировано. Лимит устройств: {row['device_limit']}."
                   if known else f"Выберите платформу и подтвердите браузер. Лимит устройств: {row['device_limit']}.")
        if import_error:
            message += " " + import_error
        page = _test_activation_page(
            ("Добавить Secure X в Happ" if not known else "Устройство подтверждено") + (" · ТЕСТ" if test_mode else ""),
            message, activation_id, token, csrf, limit=int(row["device_limit"]), registered=registered,
            selected_platform=selected, device_registered=known, happ_link=import_link, test_mode=test_mode,
            pending_import=import_pending,
            status_url=f"/activate/{activation_id}/{token}/status" if import_pending and not test_mode else "")
    response = HTMLResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    if response_cookie:
        response.set_cookie(cookie_name, cookie_value, max_age=365 * 24 * 60 * 60,
                            httponly=True, secure=True, samesite="lax", path=cookie_path)
    return response


async def _test_activation_get(activation_id: str, token: str, request: Request):
    context = _test_environment.set(True)
    try:
        return await _activation_get_current(activation_id, token, request)
    finally:
        _test_environment.reset(context)


async def _main_activation_get(activation_id: str, token: str, request: Request,
                               background_tasks: BackgroundTasks):
    context = _test_environment.set(False)
    try:
        return await _activation_get_current(activation_id, token, request, background_tasks)
    finally:
        _test_environment.reset(context)


async def _main_activation_status(activation_id: str, token: str, request: Request,
                                  background_tasks: BackgroundTasks):
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,48}", token):
        return JSONResponse({"error": "Ссылка активации недействительна."}, status_code=404)
    user_id = _activation_user_id(activation_id, test_mode=False)
    if user_id is None:
        return JSONResponse({"error": "Ссылка активации недействительна."}, status_code=404)
    cookie_value = request.cookies.get("subscription_activation_browser", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,64}", cookie_value):
        return JSONResponse({"error": "Откройте страницу активации в этом же браузере."}, status_code=403)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    browser_hash = _main_activation_browser_hash(cookie_value)
    try:
        pool = await state.db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT a.device_limit,a.activated_at,a.drive_ready_at,
                u.status,u.enabled,u.expires_at,u.drive_file_id,
                d.device_hash,d.subscription_token_hash,d.encrypted_link
                FROM subscription_activation a JOIN users u ON u.id=a.user_id
                LEFT JOIN activation_devices d ON d.user_id=a.user_id AND d.device_hash=$3
                WHERE a.user_id=$1 AND a.token_hash=$2""", user_id, token_hash, browser_hash)
    except Exception as exc:
        log.info("Activation status lookup unavailable: user=%d reason=%s", user_id, type(exc).__name__)
        return JSONResponse({"error": "Не удалось проверить подтверждение. Повторите попытку."}, status_code=503)
    if not row:
        return JSONResponse({"error": "Ссылка активации недействительна."}, status_code=404)
    if row["device_hash"] is None:
        return JSONResponse({"error": "Это устройство не зарегистрировано. Обновите страницу активации."},
                            status_code=403)
    if row["status"] != "active" or not row["enabled"] or (
            row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)):
        return JSONResponse({"error": "Подписка отключена или срок её действия закончился."}, status_code=409)
    if not row["drive_file_id"]:
        return JSONResponse({"error": "Файл подписки ещё готовится. Обновите страницу позже."}, status_code=409)
    slot_token = _main_slot_token(user_id, cookie_value)
    expected_hash = hashlib.sha256(f"drive-v1:{slot_token}".encode()).hexdigest()
    if (row["drive_ready_at"] is not None and row["subscription_token_hash"] == expected_hash
            and row["encrypted_link"]):
        return JSONResponse({"ready": True, "happ_link": str(row["encrypted_link"])},
                            headers={"Cache-Control": "no-store"})
    job_key = (user_id, browser_hash)
    if job_key in _main_activation_link_errors:
        return JSONResponse({"error": _main_activation_link_errors[job_key]},
                            headers={"Cache-Control": "no-store"})
    _schedule_main_activation_link(background_tasks, user_id, cookie_value)
    return JSONResponse({"ready": False, "pending": True}, headers={"Cache-Control": "no-store"})


async def _activation_post_current(activation_id: str, token: str, request: Request,
                                  background_tasks: BackgroundTasks | None = None):
    scope = _activation_scope()
    test_mode = bool(scope["test_mode"])
    activation_table = str(scope["activation_table"])
    devices_table = str(scope["devices_table"])
    cookie_name = str(scope["cookie_name"])
    cookie_path = str(scope["cookie_path"])
    activation_user_id = _activation_user_id(activation_id, test_mode=test_mode)
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,48}", token) or activation_user_id is None:
        return HTMLResponse(_test_activation_page("Ссылка активации недействительна",
            "Проверьте адрес или запросите новую ссылку у администратора.", "", "", tone="error", test_mode=test_mode),
            status_code=404, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    def form_error(title: str, message: str, status: int):
        return HTMLResponse(_test_activation_page(title, message, activation_id, token, tone="error", test_mode=test_mode),
            status_code=status, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        raise HTTPException(400, "Некорректный запрос") from None
    if content_length > 8192:
        raise HTTPException(413, "Слишком большой запрос")
    body = await request.body()
    if len(body) > 8192:
        raise HTTPException(413, "Слишком большой запрос")
    try:
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=6)
    except ValueError:
        raise HTTPException(400, "Некорректная форма подтверждения") from None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cookie_value = request.cookies.get(cookie_name, "")
    supplied_csrf = (form.get("csrf") or [""])[0]
    platform = (form.get("platform") or [""])[0]
    if platform not in TEST_ACTIVATION_PLATFORMS or platform == "unknown":
        return form_error("Выберите устройство", "Вернитесь к форме и выберите платформу вручную.", 422)
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,64}", cookie_value) or not hmac.compare_digest(
            supplied_csrf, _activation_csrf(token_hash, cookie_value, test_mode=test_mode)):
        return form_error("Подтверждение устарело", "Откройте ссылку в этом же браузере и подтвердите устройство ещё раз.", 403)
    browser_hash = _activation_device_hash(cookie_value, test_mode=test_mode)
    browser = _test_activation_browser_name(request.headers.get("user-agent", ""))
    language = (request.headers.get("accept-language", "").split(",", 1)[0].split(";", 1)[0])[:24]
    pool = await state.db()
    limit = 1
    registered = 0
    already_registered = False
    needs_drive_sync = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(f"""SELECT a.user_id,a.activated_at,a.drive_ready_at,a.device_limit,
                u.name,u.status,u.enabled,u.expires_at,u.drive_file_id
                FROM {activation_table} a JOIN users u ON u.id=a.user_id
                WHERE a.token_hash=$1 AND a.user_id=$2 FOR UPDATE OF a,u""", token_hash, activation_user_id)
            if not row:
                return form_error("Ссылка активации устарела", "Попросите администратора выдать новую ссылку.", 404)
            needs_drive_sync = row["drive_ready_at"] is None or row["activated_at"] is None
            if row["status"] != "active" or not row["enabled"] or (row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)):
                return form_error("Подписка недоступна",
                    "Тестовая подписка отключена или срок её действия закончился." if test_mode
                    else "Подписка отключена или срок её действия закончился.", 409)
            if not row["drive_file_id"]:
                return form_error("Профиль готовится", "Файл подписки ещё не создан. Повторите попытку позже.", 409)
            limit = int(row["device_limit"])
            existing = await conn.fetchrow(f"""SELECT device_hash FROM {devices_table}
                WHERE user_id=$1 AND device_hash=$2 FOR UPDATE""", activation_user_id, browser_hash)
            registered = int(await conn.fetchval(f"SELECT count(*) FROM {devices_table} WHERE user_id=$1",
                                                 activation_user_id) or 0)
            if existing:
                already_registered = True
                await conn.execute(f"""UPDATE {devices_table} SET platform=$3,browser=$4,language=$5,
                    last_seen_at=now() WHERE user_id=$1 AND device_hash=$2""",
                    activation_user_id, browser_hash, platform, browser, language)
            else:
                if registered >= limit:
                    denied_page = _test_activation_page("Лимит устройств достигнут",
                        f"Зарегистрировано {registered} из {limit} устройств. Это устройство не подключено.",
                        activation_id, token, limit=limit, registered=registered, tone="error", test_mode=test_mode)
                    response = HTMLResponse(denied_page, status_code=409,
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
                    response.set_cookie(cookie_name, cookie_value, max_age=365 * 24 * 60 * 60,
                        httponly=True, secure=True, samesite="lax", path=cookie_path)
                    return response
                await conn.execute(f"""INSERT INTO {devices_table}(user_id,device_hash,platform,browser,language)
                    VALUES($1,$2,$3,$4,$5)""", activation_user_id, browser_hash, platform, browser, language)
                registered += 1
            if test_mode:
                await conn.execute(f"""UPDATE {activation_table} SET activated_at=COALESCE(activated_at,now()),
                    updated_at=now() WHERE user_id=$1""", activation_user_id)
            else:
                await conn.execute("""UPDATE subscription_activation SET activated_at=COALESCE(activated_at,now()),
                    drive_ready_at=CASE WHEN activated_at IS NULL THEN NULL ELSE drive_ready_at END,
                    drive_generation=drive_generation+CASE WHEN activated_at IS NULL THEN 1 ELSE 0 END,
                    updated_at=now() WHERE user_id=$1""", activation_user_id)
    import_pending = False
    if test_mode:
        delivery = {"ok": True, "pending": False}
        if needs_drive_sync:
            delivery = await state.request_drive_sync([activation_user_id])
            drive_ready = (delivery.get("ok") and not delivery.get("pending")
                           and int(delivery.get("processed") or 0) > 0)
            if drive_ready:
                async with pool.acquire() as conn:
                    await conn.execute(f"""UPDATE {activation_table} SET drive_ready_at=now(),updated_at=now()
                        WHERE user_id=$1 AND activated_at IS NOT NULL""", activation_user_id)
        else:
            drive_ready = True
        share_link, link_error = (await _ensure_activation_import_link(
            activation_user_id, cookie_value, str(request.base_url).rstrip("/"), test_mode=True)
            if drive_ready else
            (None, "Подписка ещё готовится. Повторите подтверждение с этого же браузера позже."))
    else:
        # Drive must receive the activated profile before the encrypted direct
        # Drive link is generated. Run both steps behind the pending page.
        share_link = None
        link_error = None
        import_pending = True
        if background_tasks is not None:
            _schedule_main_activation_link(background_tasks, activation_user_id, cookie_value)
    message = ("Регистрация этого браузера уже была сохранена." if already_registered else
               ("Устройство зарегистрировано. Добавьте индивидуальную тестовую подписку кнопкой ниже."
                if test_mode else "Устройство зарегистрировано. Добавьте индивидуальную подписку кнопкой ниже."))
    tone = "normal"
    if link_error:
        message += " " + link_error
        tone = "error"
    csrf = _activation_csrf(token_hash, cookie_value, test_mode=test_mode)
    page = _test_activation_page("Устройство зарегистрировано" + (" · ТЕСТ" if test_mode else ""),
        message, activation_id, token, csrf,
        limit=limit, registered=registered, selected_platform=platform, device_registered=True,
        happ_link=share_link, auto_open=bool(share_link and platform != "appletv"), tone=tone, test_mode=test_mode,
        pending_import=import_pending,
        status_url=f"/activate/{activation_id}/{token}/status" if import_pending else "")
    response = HTMLResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    response.set_cookie(cookie_name, cookie_value, max_age=365 * 24 * 60 * 60,
        httponly=True, secure=True, samesite="lax", path=cookie_path)
    return response


async def _test_activation_post(activation_id: str, token: str, request: Request):
    context = _test_environment.set(True)
    try:
        return await _activation_post_current(activation_id, token, request)
    finally:
        _test_environment.reset(context)


async def _main_activation_post(activation_id: str, token: str, request: Request,
                               background_tasks: BackgroundTasks):
    context = _test_environment.set(False)
    try:
        return await _activation_post_current(activation_id, token, request, background_tasks)
    finally:
        _test_environment.reset(context)


async def _activation_slot_subscription_get_current(activation_id: str, slot_token: str, request: Request):
    scope = _activation_scope()
    test_mode = bool(scope["test_mode"])
    activation_table = str(scope["activation_table"])
    devices_table = str(scope["devices_table"])
    user_id = _activation_user_id(activation_id, test_mode=test_mode)
    if user_id is None or not re.fullmatch(r"[A-Za-z0-9_-]{40,48}", slot_token):
        return PlainTextResponse(_test_activation_gate_profile("", test_mode=test_mode),
            headers={"Cache-Control": "no-store"})
    slot_hash = hashlib.sha256(slot_token.encode()).hexdigest()
    try:
        pool = await state.db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"""SELECT u.id,u.name,u.status,u.enabled,u.expires_at,u.drive_file_id
                FROM {devices_table} d
                JOIN {activation_table} a ON a.user_id=d.user_id
                JOIN users u ON u.id=d.user_id
                WHERE d.user_id=$1 AND d.subscription_token_hash=$2""", user_id, slot_hash)
            if not row or row["status"] != "active" or not row["enabled"] or (
                    row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)):
                return PlainTextResponse(_test_activation_gate_profile("", "Подписка недоступна", test_mode=test_mode),
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
            await conn.execute(f"""UPDATE {devices_table} SET last_seen_at=now()
                WHERE user_id=$1 AND subscription_token_hash=$2""", user_id, slot_hash)
    except Exception as exc:
        log.info("Render slot validation unavailable: user=%d reason=%s", user_id, type(exc).__name__)
        return PlainTextResponse(_test_activation_gate_profile("", "Подписка временно недоступна", test_mode=test_mode),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    if not row["drive_file_id"]:
        return PlainTextResponse(_test_activation_gate_profile("", "Профиль подписки готовится", test_mode=test_mode),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            drive = await client.get(_drive_subscription_link(row["drive_file_id"]))
        content_type = drive.headers.get("content-type", "").lower()
        prefix = drive.content.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if (drive.status_code >= 400 or "text/html" in content_type or "application/json" in content_type
                or prefix.startswith((b"<!doctype html", b"<html", b"{"))):
            log.info("Render Drive source unavailable: user=%d status=%d content_type=%s",
                     user_id, drive.status_code, content_type or "unspecified")
            content = _test_activation_gate_profile("", "Профиль подписки ещё готовится", test_mode=test_mode)
        elif not drive.content.strip():
            content = _test_activation_gate_profile("", "Профиль подписки пуст", test_mode=test_mode)
        else:
            content = drive.content
    except (HTTPException, httpx.HTTPError) as exc:
        log.info("Render Drive proxy failed: user=%d reason=%s", user_id, type(exc).__name__)
        content = _test_activation_gate_profile("", "Профиль подписки временно недоступен", test_mode=test_mode)
    return Response(content=content, media_type="text/plain; charset=utf-8", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    })


async def _test_slot_subscription_get(activation_id: str, slot_token: str, request: Request):
    context = _test_environment.set(True)
    try:
        return await _activation_slot_subscription_get_current(activation_id, slot_token, request)
    finally:
        _test_environment.reset(context)


async def _main_slot_subscription_get(activation_id: str, slot_token: str, request: Request):
    context = _test_environment.set(False)
    try:
        return await _activation_slot_subscription_get_current(activation_id, slot_token, request)
    finally:
        _test_environment.reset(context)


async def test_geo_file(user_id: int, signature: str, filename: str, request: Request,
                        background_tasks: BackgroundTasks):
    if not _test_environment.get():
        raise HTTPException(404, "Not found")
    if filename not in GEO_FILE_SOURCES or not hmac.compare_digest(signature, _test_geo_signature(user_id)):
        raise HTTPException(404, "Not found")
    pool = await state.db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""SELECT u.id,g.geo_device_limit,g.geoip_requests,g.geosite_requests
            FROM users u JOIN test_geo_state g ON g.user_id=u.id
            WHERE u.id=$1 AND u.status='active' AND u.enabled=TRUE AND g.geo_device_limit IS NOT NULL""", user_id)
    if not user or user["geo_device_limit"] is None:
        raise HTTPException(404, "Not found")
    request_column = "geoip_requests" if filename == "geoip.dat" else "geosite_requests"
    if user[request_column] >= user["geo_device_limit"]:
        raise HTTPException(429, "Тестовый лимит запросов geo-файла исчерпан")
    try:
        async with state.test_geo_file_lock:
            cached = state.test_geo_file_cache.get(filename)
            if cached and time.monotonic() - cached[1] < GEO_FILE_CACHE_TTL_SECONDS:
                content = cached[0]
            else:
                async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                    response = await client.get(GEO_FILE_SOURCES[filename], headers={"User-Agent": "Happ"})
                content = response.content
                if response.status_code >= 400 or len(content) < 1024 or len(content) > 50_000_000:
                    raise ValueError("Источник геофайла вернул некорректный файл")
                if content.lstrip()[:64].lower().startswith((b"<!doctype html", b"<html", b"{\"message\"")):
                    raise ValueError("Источник вместо геофайла вернул текстовую страницу")
                state.test_geo_file_cache[filename] = (content, time.monotonic())
    except Exception as exc:
        log.warning("Test geo-file fetch failed (user_id=%d, file=%s, error=%s)", user_id, filename, type(exc).__name__)
        raise HTTPException(502, "Не удалось получить тестовый геофайл") from exc

    column = "geoip_downloaded_at" if filename == "geoip.dat" else "geosite_downloaded_at"
    activated_now = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            slot = await conn.fetchrow(f"""UPDATE test_geo_state SET {request_column}={request_column}+1,
                {column}=COALESCE({column},now()), download_requests=download_requests+1
                WHERE user_id=$1 AND geo_device_limit IS NOT NULL AND {request_column}<geo_device_limit
                RETURNING user_id""", user_id)
            if not slot:
                raise HTTPException(429, "Тестовый лимит запросов geo-файла исчерпан")
            activated_now = bool(await conn.fetchval("""UPDATE test_geo_state SET activated_at=now()
                WHERE user_id=$1 AND activated_at IS NULL
                  AND geoip_downloaded_at IS NOT NULL AND geosite_downloaded_at IS NOT NULL
                RETURNING TRUE""", user_id))
    if activated_now:
        background_tasks.add_task(_sync_activated_test_subscription, user_id)
    return Response(content=content, media_type="application/octet-stream", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate", "Content-Length": str(len(content)),
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


def install(app: FastAPI):
    app.add_middleware(PanelSecurityMiddleware)
    app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="panel-static")
    app.include_router(router)
    app.include_router(router, prefix="/test")
    app.add_api_route("/test/geo/{user_id}/{signature}/{filename}", test_geo_file,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/test/activate/{activation_id}/{token}", _test_activation_get,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/test/activate/{activation_id}/{token}", _test_activation_post,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/test/sub/{activation_id}/{slot_token}", _test_slot_subscription_get,
                      methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/activate/{activation_id}/{token}", _main_activation_get,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/activate/{activation_id}/{token}", _main_activation_post,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/activate/{activation_id}/{token}/status", _main_activation_status,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/sub/{activation_id}/{slot_token}", _main_slot_subscription_get,
                      methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/api/admin/users/{user_id}/activation/devices/{device_hash}", activation_device_delete,
                      methods=["DELETE"], include_in_schema=False)
    app.add_api_route("/test/api/admin/users/{user_id}/test-activation/reset", test_activation_reset,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/test/api/admin/users/{user_id}/test-activation/retry", test_activation_retry,
                      methods=["POST"], include_in_schema=False)
    app.add_api_route("/test/api/admin/users/{user_id}/test-activation-qr.svg", test_activation_qr,
                      methods=["GET"], include_in_schema=False)
    app.add_api_route("/test", test_page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route("/test/desktop", test_desktop_page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route("/test/mobile", test_mobile_page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(application):
        # Run DB initialization in the background before entering the relay's
        # own lifespan; a connection failure cannot block relay availability.
        task = asyncio.create_task(state.connect())
        try:
            async with previous_lifespan(application):
                yield
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await state.close()

    app.router.lifespan_context = combined_lifespan
