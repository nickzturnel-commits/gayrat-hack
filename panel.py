"""Admin panel, subscription generation and Google Drive sync API.

This module deliberately owns an upstream client/cache separate from the public relay.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, unquote, urlencode, urlsplit

import asyncpg
import httpx
import qrcode
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt
from qrcode.image.svg import SvgPathImage
from starlette.middleware.base import BaseHTTPMiddleware
import happ_limit

log = logging.getLogger("vpn_panel")
router = APIRouter()

LEGACY_DRIVE_FILE_ID = "160lsZ2nDrSXTzA09r9jeGHnd6QyFylw9"
PANEL_UPSTREAM_DEFAULT_USER_AGENT = "Happ"
ASHGABAT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
ASHGABAT_LATITUDE = 37.9601
ASHGABAT_LONGITUDE = 58.3261
ASHGABAT_WEATHER_UNAVAILABLE = "🌡 Ашхабад: текущая погода временно недоступна"
UPSTREAM_TIMEOUT_SECONDS = 20.0
PANEL_CACHE_TTL_SECONDS = 300
SESSION_SECONDS = 12 * 60 * 60
DB_HINT = ("Не удалось подключиться к базе. Если используется прямой хост db.*.supabase.co, "
           "возьмите в Supabase (Connect → Session pooler) строку подключения и поставьте её в DATABASE_URL")
URI_LINE = re.compile(r"(?i)^\s*(?:vless|vmess|trojan|ss|hysteria2?|tuic)://")
URI_ANY = re.compile(r"(?i)(?:vless|vmess|trojan|ss|hysteria2?|tuic)://")
DEVICE_MARKERS = (b"device limit full", b"remove a device first")
SKIP_UPSTREAM_HEADERS = {"host", "content-length", "connection", "accept-encoding"}

DAY_DEFAULTS = ["💪 Понедельник — начинаем бодро", "🔥 Вторник — продуктивного дня", "⚡ Среда — середина недели",
                "😎 Четверг — почти пятница", "🎉 Пятница — выходные близко", "🛋 Суббота — отдыхаем", "🌴 Воскресенье — набираемся сил"]
FOOTER_DEFAULTS = ["✅ Обновлено в {t} • 💬 Поддержка 24/7", "🔄 Свежее обновление: {t} • 💬 Мы на связи",
                   "⏱ Актуально на {t} • 💬 Поддержка 24/7", "🆕 Обновили в {t} • 💬 Пиши, поможем",
                   "✨ Всё свежее с {t} • 💬 Поддержка 24/7"]
QUOTE_DEFAULTS = ["🚀 Скорость — наш конёк", "🔒 Твои данные только твои", "🌍 Интернет без границ", "😴 Включил и забыл",
                  "🎬 Стримы без буферизации", "🎮 Пинг, который радует", "💡 Маленькие шаги ведут далеко", "🌱 Сегодня лучше, чем вчера",
                  "🔥 Не откладывай на завтра", "🧠 Учись — и мир станет шире", "🏔 Каждая вершина начинается с шага", "⏳ Время — лучший вклад",
                  "🌊 Плыви, а не тони", "🎯 Цель ясна — путь найдётся", "😎 Спокойствие — тоже сила", "🍀 Удача любит упорных",
                  "☕ Хороший день начинается с кофе", "🌙 Отдых — часть работы", "📚 Знание — лучший капитал", "🛡 Безопасность — это спокойный сон",
                  "🦁 Будь смелее, чем вчера", "🌅 Новый день — новые шансы", "🧩 Всё получится, шаг за шагом", "🕊 Свобода в мелочах",
                  "🎶 Музыка без границ", "📡 Связь всегда рядом", "🧭 Знай свой курс", "🌟 Верь в себя", "💎 Качество важнее количества",
                  "🤝 Вместе мы сильнее", "🐢 Тише едешь — дальше будешь", "🐇 Но скорость тоже не помешает", "🎈 Лёгкость — залог успеха",
                  "🔭 Смотри шире", "🧗 Трудности делают сильнее", "🍉 Лето бывает круглый год", "🎲 Рискни — и получишь опыт",
                  "📈 Растём каждый день", "🧘 Дыши глубже, всё решаемо", "🛠 Мастерство приходит с практикой", "🌍 Мир открыт — заходи",
                  "⚙️ Всё работает, пока ты спокоен", "🔑 Ключ к успеху — начать", "🏁 Финиш ближе, чем кажется", "🌈 После дождя будет радуга",
                  "🍿 Фильм, сериал и никаких лагов", "🎧 Включи любимое и вперёд", "🕹 Сегодня точно выиграем", "🌐 Любой сайт — в один клик",
                  "🧊 Холодная голова, быстрый интернет"]
DEFAULT_SETTINGS = {
    "title": "Secure X", "support_url": "https://t.me/moserrr1", "web_page_url": "", "update_interval": 1,
    "timezone": "Asia/Ashgabat", "announce_enabled": True, "announce_refresh_minutes": 5,
    # Individual profiles share provider usage. Admins may hide the counter on
    # new user profiles, while the legacy account profile stays visible.
    "default_traffic_limit_bytes": None,
    "hide_traffic_for_new_users": False,
    "include_sas_for_new_users": True,
    "device_limit_for_new_users": None,
    "quotes": QUOTE_DEFAULTS, "footers": FOOTER_DEFAULTS, "days": DAY_DEFAULTS,
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

    by_identity: dict[str, tuple[int, str]] = {}

    def add_link(uri: str, rank: int):
        try:
            identity = parse_server_uri(uri)["id"]
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return
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

            if protocol != "vless":
                continue
            vnext = settings.get("vnext")
            if not isinstance(vnext, list):
                continue
            stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}
            network = str(stream.get("network") or "tcp").lower()
            if network not in {"tcp", "kcp", "ws", "http", "grpc", "httpupgrade", "xhttp"}:
                continue
            security = str(stream.get("security") or "none").lower()
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

    return [uri for _rank, uri in by_identity.values()]


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
        converted = _xray_json_uris(decoded_text)
        if converted:
            return "\n".join(converted)
    except (ValueError, UnicodeDecodeError):
        pass
    return text


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
    converted = _xray_json_uris(text)
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
            server_id = parse_server_uri(candidate)["id"]
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if server_id not in seen:
            seen.add(server_id)
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
    def __init__(self):
        self.pool = None
        self.db_error = "База ещё не проверялась"
        self.db_last_attempt = 0.0
        self.upstream_client: httpx.AsyncClient | None = None
        self.upstream_body: bytes | None = None
        self.upstream_headers: dict[str, str] = {}
        self.upstream_fetched_at = 0.0
        self.upstream_success_at: float | None = None
        self.upstream_attempt_at = 0.0
        self.upstream_warning: str | None = None
        self.sas_body: bytes | None = None
        self.sas_fetched_at = 0.0
        self.sas_success_at: float | None = None
        self.sas_attempt_at = 0.0
        self.sas_warning: str | None = None
        self.combined_body: bytes | None = None
        self.combined_fetched_at = 0.0
        self.combined_success_at: float | None = None
        self.upstream_lock = asyncio.Lock()
        self.announce_cache: tuple[int, str] | None = None
        self.announce_quote: str | None = None
        self.login_fails: dict[str, tuple[int, float]] = {}
        self.drive_push_last: float = 0.0
        self.drive_push_error: str | None = None
        self.happ_tasks: set[asyncio.Task] = set()

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
            return
        if self.pool is not None:
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
                await self.seed(conn)
            self.db_error = ""
            log.info("Admin database connected and schema initialized")
        except Exception as exc:
            if self.pool is not None:
                await self.pool.close()
                self.pool = None
            self.db_error = DB_HINT
            # Error type only; driver messages can contain a hostname/user or connection details.
            log.warning("Admin database unavailable (%s). %s", type(exc).__name__, DB_HINT)

    async def seed(self, conn):
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO NOTHING", key, json.dumps(value, ensure_ascii=False))
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

    async def db(self, required=True):
        await self.connect()
        if self.pool is None and required:
            raise HTTPException(503, self.db_error or DB_HINT)
        return self.pool

    async def push_drive_sync(self, user_ids: list[int] | None = None) -> dict:
        """Ask the authenticated Apps Script web app to update Drive immediately."""
        target = os.getenv("DRIVE_SYNC_URL", "").strip()
        key = os.getenv("SYNC_KEY", "").strip()
        if not target or not key:
            message = "Мгновенная доставка не настроена: добавьте DRIVE_SYNC_URL в Render"
            self.drive_push_error = message
            return {"ok": False, "pending": True, "message": message}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(150.0, connect=12.0)) as client:
                response = await client.post(target, headers={"X-Sync-Key": key, "Accept": "application/json"},
                                             json={"action": "sync", "user_ids": user_ids, "sync_key": key})
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
                self.drive_push_error = None
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
            self.drive_push_last = time.time()
            self.drive_push_error = None
            return {"ok": True, "pending": False,
                    "message": self.drive_push_error or "Изменения сразу отправлены в Google Drive",
                    "processed": processed, "failed": 0}
        except Exception as exc:
            self.drive_push_error = "Не удалось сразу обновить Google Drive; автоматическая попытка повторится позже"
            safe_detail = _safe_sync_error(str(exc))
            log.warning("Immediate Drive update failed (%s%s)", type(exc).__name__,
                        f": {safe_detail}" if safe_detail else "")
            return {"ok": False, "pending": True, "message": self.drive_push_error}

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

    async def fetch_upstream(self, force=False, force_sas=False, allow_sas_failure=False):
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
                    combined_body = primary_body
                else:
                    try:
                        combined_body = merge_subscription_bodies(
                            primary_body,
                            sas_body,
                            self.upstream_headers.get("content-type", ""),
                            "text/plain",
                        )
                    except ValueError as exc:
                        self.sas_warning = "Не удалось сформировать объединённый профиль SAS"
                        log.warning("Combined subscription rejected (%s)", type(exc).__name__)
                        if self.combined_body is None:
                            raise HTTPException(502, self.sas_warning) from exc
                        combined_body = self.combined_body
                        sas_stale = True
            else:
                combined_body = primary_body
                self.sas_warning = None

            self.combined_body = combined_body
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
    token = request.cookies.get("admin_session", "")
    try:
        raw, sig = token.split(".", 1)
        actual = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        expected = hmac.new(os.getenv("SESSION_SECRET", "").encode(), raw.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(actual, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload.get("exp", 0) < time.time() or payload.get("u") != os.getenv("ADMIN_USERNAME"):
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
    announce_override: str | None = Field(default=None, max_length=4000)
    traffic_limit_bytes: int | None = Field(default=None, ge=1_000_000, le=1_000_000_000_000)
    hide_traffic_usage: bool | None = None
    include_sas: bool | None = None
    device_limit: StrictInt | None = Field(default=None, ge=happ_limit.MIN_LIMIT, le=happ_limit.MAX_LIMIT)


class DeviceLimitActionBody(BaseModel):
    action: Literal["retry", "rebuild"] = "retry"


class ServerBody(BaseModel):
    custom_name: str | None = Field(default=None, max_length=200)
    hidden: bool = False
    sort_order: int = Field(default=0, ge=-100000, le=100000)


class UserServerBody(BaseModel):
    hidden: bool | None = None
    custom_name: str | None = Field(default=None, max_length=200)


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


def _now_for_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(name))
    except Exception:
        return datetime.now(timezone.utc)


def _format_ashgabat_weather(data: dict) -> str:
    """Format Open-Meteo current weather; accepts its current and legacy fields."""
    if not isinstance(data, dict):
        raise ValueError("weather response is not an object")
    current = data.get("current") or data.get("current_weather")
    if not isinstance(current, dict):
        raise ValueError("weather response has no current conditions")

    temperature = current.get("temperature_2m", current.get("temperature"))
    weather_code = current.get("weather_code", current.get("weathercode"))
    weather_time = current.get("time")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ValueError("weather temperature is missing")
    if weather_code is not None and (
            isinstance(weather_code, bool) or not isinstance(weather_code, (int, float))):
        raise ValueError("weather code is invalid")
    if not isinstance(weather_time, str):
        raise ValueError("weather valid time is missing")

    temperature = float(temperature)
    if not -100 <= temperature <= 100:
        raise ValueError("weather values are outside valid ranges")

    conditions = {
        0: ("☀️", "ясно"),
        1: ("🌤", "преимущественно ясно"),
        2: ("⛅", "переменная облачность"),
        3: ("☁️", "пасмурно"),
        45: ("🌫", "туман"),
        48: ("🌫", "туман с изморозью"),
        51: ("🌦", "слабая морось"),
        53: ("🌦", "умеренная морось"),
        55: ("🌧", "сильная морось"),
        56: ("🌧", "слабая ледяная морось"),
        57: ("🌧", "сильная ледяная морось"),
        61: ("🌧", "небольшой дождь"),
        63: ("🌧", "умеренный дождь"),
        65: ("🌧", "сильный дождь"),
        66: ("🌧", "слабый ледяной дождь"),
        67: ("🌧", "сильный ледяной дождь"),
        71: ("❄️", "небольшой снегопад"),
        73: ("❄️", "умеренный снегопад"),
        75: ("❄️", "сильный снегопад"),
        77: ("❄️", "снежная крупа"),
        80: ("🌧", "небольшие ливни"),
        81: ("🌧", "умеренные ливни"),
        82: ("🌧", "сильные ливни"),
        85: ("❄️", "слабый снег с перерывами"),
        86: ("❄️", "сильный снег с перерывами"),
        95: ("⛈", "гроза"),
        96: ("⛈", "гроза с небольшим градом"),
        97: ("⛈", "сильная гроза"),
        99: ("⛈", "гроза с сильным градом"),
    }
    condition = None
    if weather_code is not None:
        code = int(weather_code)
        if not 0 <= code <= 99:
            raise ValueError("weather code is outside valid range")
        condition = conditions.get(code)
        if condition is None:
            raise ValueError("weather code is not recognized")

    offset_seconds = data.get("utc_offset_seconds")
    if isinstance(offset_seconds, float) and offset_seconds.is_integer():
        offset_seconds = int(offset_seconds)
    if (isinstance(offset_seconds, int) and not isinstance(offset_seconds, bool)
            and abs(offset_seconds) <= 14 * 60 * 60):
        ashgabat_timezone = timezone(timedelta(seconds=offset_seconds))
    else:
        try:
            from zoneinfo import ZoneInfo
            ashgabat_timezone = ZoneInfo("Asia/Ashgabat")
        except Exception:
            # Open-Meteo normally includes utc_offset_seconds. This fixed
            # offset fallback also lets local Windows runs validate the data.
            ashgabat_timezone = timezone(timedelta(hours=5))

    try:
        observed_at = datetime.fromisoformat(weather_time)
    except ValueError as exc:
        raise ValueError("weather valid time is invalid") from exc
    local_now = datetime.now(ashgabat_timezone)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=ashgabat_timezone)
    else:
        observed_at = observed_at.astimezone(ashgabat_timezone)
    age_seconds = (local_now - observed_at).total_seconds()
    # Open-Meteo's current fields are model-based and may be hourly in this
    # region. Allow one model interval plus margin, but never present an older
    # response as current.
    if age_seconds > 2 * 60 * 60 or age_seconds < -15 * 60:
        raise ValueError("weather valid time is stale or in the future")

    shown_temperature = f"{temperature:g}"
    if condition is None:
        description = f"🌡 Ашхабад сейчас: {shown_temperature}°C"
    else:
        emoji, text = condition
        description = f"{emoji} Ашхабад сейчас: {text}, {shown_temperature}°C"
    return f"{description} · данные на {observed_at:%H:%M}"


async def _current_ashgabat_weather() -> str:
    """Fetch fresh city weather independently from the VPN upstream client/cache."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=5.0,
            headers={
                "User-Agent": "SecureX-Weather/1.0",
                "Cache-Control": "no-cache",
            },
        ) as client:
            response = await client.get(
                ASHGABAT_WEATHER_URL,
                params={
                    "latitude": ASHGABAT_LATITUDE,
                    "longitude": ASHGABAT_LONGITUDE,
                    "current": "temperature_2m",
                    "timezone": "Asia/Ashgabat",
                },
            )
            response.raise_for_status()
            return _format_ashgabat_weather(response.json())
    except httpx.HTTPStatusError as exc:
        response = exc.response
        reason = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                candidate = payload.get("reason")
                if isinstance(candidate, str):
                    # Open-Meteo's error JSON may explain 400/429/5xx responses.
                    # Keep only this bounded diagnostic; never log the URL/body.
                    reason = re.sub(r"[\x00-\x1f\x7f]+", " ", candidate).strip()[:180]
        except Exception:
            pass
        log.warning(
            "Ashgabat weather request failed (Open-Meteo HTTP %d%s)",
            response.status_code,
            f": {reason}" if reason else "",
        )
        return ASHGABAT_WEATHER_UNAVAILABLE
    except Exception as exc:
        # Keep diagnostics useful without logging response bodies or request data.
        log.warning("Ashgabat weather request failed (%s)", type(exc).__name__)
        return ASHGABAT_WEATHER_UNAVAILABLE


def _weather_payload_from_sync_request(request: Request) -> dict | None:
    """Decode the small weather snapshot supplied by the trusted Apps Script."""
    encoded = request.headers.get("x-weather-data")
    if not encoded:
        return None
    if len(encoded) > 4096:
        return {}
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _announce(settings: dict, weather_payload: dict | None = None) -> str:
    # Weather is deliberately fetched for every generated manifest. Other
    # announcement elements rotate on stable time slots, so a five-minute
    # trigger gets a fresh quote/footer even if its execution time drifts.
    if weather_payload is None:
        weather_line = await _current_ashgabat_weather()
    else:
        try:
            weather_line = _format_ashgabat_weather(weather_payload)
        except Exception:
            weather_line = ASHGABAT_WEATHER_UNAVAILABLE
    refresh = max(5, int(settings.get("announce_refresh_minutes", 5))) * 60
    refresh_slot = int(time.time() // refresh)
    if state.announce_cache and refresh_slot == state.announce_cache[0]:
        return state.announce_cache[1].replace("__CURRENT_ASHGABAT_WEATHER__", weather_line)
    local = _now_for_tz(settings.get("timezone", "Asia/Ashgabat"))
    lines = []
    days = settings.get("days") or DAY_DEFAULTS
    quotes = settings.get("quotes") or QUOTE_DEFAULTS
    footers = settings.get("footers") or FOOTER_DEFAULTS
    day_line = str(days[local.isoweekday() - 1]) if len(days) >= 7 else ""
    greeting = "🌙 Доброй ночи!" if local.hour < 5 else "🌅 Доброе утро!" if local.hour < 12 else "☀️ Добрый день!" if local.hour < 18 else "🌆 Добрый вечер!"
    top_lines = [line for line in (day_line, greeting) if line]
    lines.append(secrets.choice(top_lines) if top_lines else greeting)
    lines.append("__CURRENT_ASHGABAT_WEATHER__")
    candidates = [str(q) for q in quotes if str(q)]
    quote_text = secrets.choice(candidates) if candidates else ""
    if len(candidates) > 1 and quote_text == state.announce_quote:
        quote_text = candidates[(candidates.index(quote_text) + 1) % len(candidates)]
    state.announce_quote = quote_text
    footer_tpl = secrets.choice([str(x) for x in footers if str(x)]) if footers else ""
    lines.extend(["", quote_text, "", footer_tpl.replace("{t}", local.strftime("%H:%M:%S"))])
    template = "\n".join(lines).strip()
    state.announce_cache = (refresh_slot, template)
    return template.replace("__CURRENT_ASHGABAT_WEATHER__", weather_line)


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
    primary_ids: set[str] = set()
    sas_ids: set[str] = set()
    if primary_body is not None:
        primary_ids = {parse_server_uri(uri)["id"] for uri in
                       _extract_uris(_decode_subscription(primary_body))}
    if sas_body is not None:
        sas_ids = {parse_server_uri(uri)["id"] for uri in
                   _extract_uris(_decode_subscription(sas_body))
                   if not is_excluded_sas_server(uri)}
    for server in parsed:
        server["source"] = ("sas" if server["id"] in sas_ids and server["id"] not in primary_ids
                            else "primary")
    async with conn.transaction():
        existing = await conn.fetch("""SELECT id,original_name,source_key,source,custom_name,hidden,sort_order,
            present_upstream,first_seen_at,last_seen_at FROM servers""")
        counts = await conn.fetch("SELECT server_id,COUNT(*) AS override_count FROM user_servers GROUP BY server_id")
        override_counts = {str(row["server_id"]): int(row["override_count"]) for row in counts}
        parsed, merge_ids = _plan_server_reconciliation(parsed, existing, override_counts)
        existing_by_id = {str(row["id"]): row for row in existing}
        next_sort_order = max(
            (int(_server_row_value(row, "sort_order", -1)) for row in existing),
            default=-1,
        ) + 1
        await conn.execute("UPDATE servers SET present_upstream=FALSE")
        for index, server in enumerate(parsed):
            existing_row = existing_by_id.get(str(server["id"]))
            if existing_row is None:
                sort_order = next_sort_order
                next_sort_order += 1
            else:
                sort_order = _server_row_value(existing_row, "sort_order", index)
                if sort_order is None:
                    sort_order = index
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
            await conn.execute("""INSERT INTO servers(id,original_name,source_key,source,sort_order,present_upstream,last_seen_at)
                VALUES($1,$2,$3,$4,$5,TRUE,now()) ON CONFLICT(id) DO UPDATE SET original_name=EXCLUDED.original_name,
                source_key=EXCLUDED.source_key,source=EXCLUDED.source,present_upstream=TRUE,last_seen_at=now()""",
                server["id"], server["name"], server["source_key"], server["source"], sort_order)
    return parsed


async def build_manifest_content(conn, user, body: bytes, headers: dict, settings: dict,
                                 announce: str | None = None, servers: list[dict] | None = None) -> str:
    if servers is None:
        servers = await _sync_servers(conn, body)
    now = datetime.now(timezone.utc)
    expired = user["expires_at"] is not None and user["expires_at"] <= now
    active = bool(user["enabled"]) and not expired and user["status"] == "active"
    if not active:
        announcement = "⛔ Время подписки закончилось, обратитесь в поддержку" if expired else "⛔ Подписка отключена, обратитесь в поддержку"
    elif user["announce_override"] is not None:
        announcement = user["announce_override"]
    elif not settings.get("announce_enabled", True):
        announcement = ""
    elif announce is not None:
        announcement = announce
    else:
        announcement = await _announce(settings)
    # Give every generated Drive profile a distinct default name in Happ. The
    # underlying server credentials remain those from the shared upstream.
    title = user["title_override"] or f"{settings.get('title', 'Secure X')} · {user.get('name', user.get('slug', 'Пользователь'))}"
    if expired:
        title = f"Время подписки закончилось · {user.get('name', user.get('slug', 'Пользователь'))}"
    elif not user["enabled"] or user["status"] != "active":
        title = f"Подписка отключена · {user.get('name', user.get('slug', 'Пользователь'))}"
    lines = ["#profile-title: base64:" + base64.b64encode(title.encode()).decode(),
             f"#profile-update-interval: {int(settings.get('update_interval', 1))}",
             "#announce: base64:" + base64.b64encode(announcement.encode()).decode(),
             "#support-url: " + str(settings.get("support_url", "https://t.me/moserrr1"))]
    provider_id = os.getenv("HAPP_PROVIDER_ID", "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{8}", provider_id):
        lines.append("#providerid " + provider_id)
    is_legacy = user.get("slug") == "legacy" or user.get("drive_file_id") == LEGACY_DRIVE_FILE_ID
    if is_legacy or not bool(user.get("hide_traffic_usage", False)):
        expiry = int(user["expires_at"].timestamp()) if user.get("expires_at") else None
        lines.append("#subscription-userinfo: " + _userinfo(headers, expire_override=expiry))
    web_url = str(settings.get("web_page_url", "")).strip()
    if web_url:
        lines.append("#profile-web-page-url: " + web_url)
    if active:
        global_rows = await conn.fetch("SELECT id,original_name,custom_name,hidden,sort_order,source FROM servers WHERE present_upstream=TRUE ORDER BY sort_order,id")
        upstream_by_id = {server["id"]: server for server in servers}
        overrides = await conn.fetch("SELECT server_id,hidden,custom_name FROM user_servers WHERE user_id=$1", user["id"])
        overrides_by_id = {row["server_id"]: row for row in overrides}
        server_lines = []
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
            server_lines.append(rename_server_uri(server["uri"], name))
        if not server_lines:
            # Do not let Apps Script replace a working Drive profile with a
            # metadata-only file that Happ rejects as having no server links.
            raise HTTPException(502, "Для этой подписки нет доступных серверов. Обновите список серверов и проверьте, что они не скрыты.")
        lines.extend(server_lines)
    else:
        reason = "Время подписки закончилось" if expired else "Подписка отключена"
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
        response = await call_next(request)
        if request.url.path.startswith("/admin") or request.url.path.startswith("/api/"):
            _secure_headers(response)
        elif request.url.path.startswith("/static/"):
            # The panel's JS/CSS changes independently from the Python worker.
            # Revalidate assets so mobile browsers do not retain old controls.
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page():
    # Select a purpose-built page before loading the admin app bundle. Use
    # viewport and pointer capabilities so mobile browsers in desktop mode
    # still receive the phone layout when their viewport is narrow.
    return HTMLResponse("""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">
<meta name=\"color-scheme\" content=\"dark\"><title>Secure X — панель</title></head>
<body style=\"margin:0;background:#090a0c;color:#f5f6f8;font:16px system-ui;display:grid;min-height:100dvh;place-items:center\">
<p>Открываем панель…</p><script>(()=>{const mobile=matchMedia('(max-width: 760px), (pointer: coarse)').matches;
location.replace('/admin/'+(mobile?'mobile':'desktop')+location.search+location.hash)})()</script>
<noscript><p><a href=\"/admin/mobile\">Телефон</a> · <a href=\"/admin/desktop\">Компьютер</a></p></noscript>
</body></html>""")


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
    token = _sign_session({"u": os.getenv("ADMIN_USERNAME"), "c": csrf, "exp": int(time.time()) + SESSION_SECONDS})
    response.set_cookie("admin_session", token, max_age=SESSION_SECONDS, httponly=True, secure=True, samesite="strict", path="/")
    return {"ok": True, "csrf_token": csrf, "username": os.getenv("ADMIN_USERNAME")}


@router.post("/api/admin/auth/logout")
async def logout(request: Request, response: Response):
    _auth(request, csrf=True)
    response.delete_cookie("admin_session", path="/")
    return {"ok": True}


@router.get("/api/admin/auth/me")
async def me(request: Request):
    payload = _auth(request)
    return {"username": payload["u"], "csrf_token": payload["c"]}


@router.get("/api/admin/dashboard")
async def dashboard(request: Request):
    _auth(request)
    try:
        await state.fetch_upstream()
    except HTTPException:
        # The overview should remain accessible even while a source is down.
        pass
    pool = await state.db(required=False)
    db_info = {"connected": pool is not None, "error": state.db_error if pool is None else None}
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
                               "warning": state.drive_push_error}}


@router.get("/api/admin/users")
async def users_list(request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users ORDER BY created_at,id")
    api_key_configured = bool(os.getenv("DRIVE_API_KEY", ""))
    users = []
    for row in rows:
        user = _serialize(row)
        # Preload the already-authorized download URL so mobile clipboard access
        # can happen directly during the user's tap, without an awaited fetch.
        user["drive_link"] = (_drive_subscription_link(user["drive_file_id"])
                              if user.get("drive_file_id") and api_key_configured else None)
        users.append(user)
    return {"users": users, "device_limit_bounds": {"min": happ_limit.MIN_LIMIT, "max": happ_limit.MAX_LIMIT}}


@router.post("/api/admin/users")
async def users_create(data: UserBody, request: Request):
    _auth(request, csrf=True)
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
        row = await conn.fetchrow("""INSERT INTO users(name,slug,note,enabled,expires_at,title_override,announce_override,traffic_limit_bytes,hide_traffic_usage,include_sas,device_limit,device_limit_status)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""", data.name.strip(), slug, data.note, data.enabled,
            _expiry_utc(data.expires_at), data.title_override, data.announce_override, data.traffic_limit_bytes, hide_traffic, include_sas,
            device_limit, device_status)
    if row["device_limit"] is not None:
        _schedule_limited_link(row["id"])
    drive_sync = await state.push_drive_sync([row["id"]])
    return {"user": _serialize(row), "drive_sync": drive_sync}


@router.put("/api/admin/users/{user_id}")
async def users_update(user_id: int, data: UserBody, request: Request):
    _auth(request, csrf=True)
    if data.title_override and any(ord(c) < 32 for c in data.title_override):
        raise HTTPException(422, "Заголовок профиля не должен содержать переносы строк")
    pool = await state.db()
    async with pool.acquire() as conn:
        old_limit = await conn.fetchrow("SELECT slug,drive_file_id,device_limit FROM users WHERE id=$1 AND status='active'", user_id)
        if not old_limit:
            raise HTTPException(404, "Пользователь не найден")
        limit_supplied = "device_limit" in data.model_fields_set
        if limit_supplied and (old_limit["slug"] == "legacy" or old_limit["drive_file_id"] == LEGACY_DRIVE_FILE_ID) and data.device_limit is not None:
            raise HTTPException(409, "Основная (legacy) подписка всегда остаётся без лимита устройств")
        limit_changed = limit_supplied and data.device_limit != old_limit["device_limit"]
        row = await conn.fetchrow("""UPDATE users SET name=$2,note=$3,enabled=$4,expires_at=$5,title_override=$6,
            announce_override=$7,traffic_limit_bytes=$8,
            hide_traffic_usage=CASE WHEN slug='legacy' OR drive_file_id=$10 THEN FALSE ELSE COALESCE($9,hide_traffic_usage) END,
            include_sas=COALESCE($11,include_sas),
            device_limit=CASE WHEN $12 THEN $13 ELSE device_limit END,
            happ_install_code=CASE WHEN $14 THEN NULL ELSE happ_install_code END,
            limited_link=CASE WHEN $14 THEN NULL ELSE limited_link END,
            device_limit_status=CASE WHEN $14 THEN CASE WHEN $13 IS NULL THEN 'none' ELSE 'waiting' END ELSE device_limit_status END,
            device_limit_error=CASE WHEN $14 THEN NULL ELSE device_limit_error END,
            updated_at=now() WHERE id=$1 AND status='active' RETURNING *""", user_id,
            data.name.strip(), data.note, data.enabled, _expiry_utc(data.expires_at), data.title_override, data.announce_override,
            data.traffic_limit_bytes, data.hide_traffic_usage, LEGACY_DRIVE_FILE_ID, data.include_sas,
            limit_supplied, data.device_limit, limit_changed)
    if not row: raise HTTPException(404, "Пользователь не найден")
    if row["device_limit"] is not None:
        _schedule_limited_link(user_id)
    drive_sync = await state.push_drive_sync([user_id])
    return {"user": _serialize(row), "drive_sync": drive_sync}


@router.delete("/api/admin/users/{user_id}")
async def users_delete(user_id: int, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE users SET status='pending_delete',enabled=FALSE,updated_at=now() WHERE id=$1 RETURNING id", user_id)
    if not row: raise HTTPException(404, "Пользователь не найден")
    drive_sync = await state.push_drive_sync([user_id])
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
        content = await build_manifest_content(conn, user, body, headers, settings, servers=servers)
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


@router.post("/api/admin/users/{user_id}/device-limit")
async def device_limit_action(user_id: int, data: DeviceLimitActionBody, request: Request):
    _auth(request, csrf=True)
    return await _create_limited_link(user_id, rebuild=data.action == "rebuild")


@router.delete("/api/admin/users/{user_id}/device-limit")
async def device_limit_clear(user_id: int, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
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
    return await state.push_drive_sync()


@router.get("/api/admin/servers")
async def servers_list(request: Request):
    _auth(request)
    body, _, stale = await state.fetch_upstream()
    if stale:
        raise HTTPException(503, "Не удалось обновить список серверов из апстрима. Повторите позже.")
    pool = await state.db()
    async with pool.acquire() as conn:
        await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
        rows = await conn.fetch("SELECT * FROM servers WHERE present_upstream=TRUE ORDER BY sort_order,id")
    return {"servers": [_serialize(r) for r in rows]}


@router.put("/api/admin/servers/{server_id}")
async def servers_update(server_id: str, data: ServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE servers SET custom_name=$2,hidden=$3,sort_order=$4 WHERE id=$1 AND present_upstream=TRUE RETURNING *", server_id, data.custom_name, data.hidden, data.sort_order)
    if not row: raise HTTPException(404, "Сервер не найден")
    return {"server": _serialize(row), "drive_sync": await state.push_drive_sync()}


class BulkServerBody(BaseModel):
    server_ids: list[str] = Field(min_length=1, max_length=500)
    hidden: bool


@router.post("/api/admin/servers/bulk")
async def servers_bulk(data: BulkServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE servers SET hidden=$1 WHERE id=ANY($2::text[]) AND present_upstream=TRUE", data.hidden, data.server_ids)
    return {"ok": True, "updated": int(result.rsplit(" ", 1)[-1]), "drive_sync": await state.push_drive_sync()}


@router.post("/api/admin/servers/refresh")
async def servers_refresh(request: Request):
    _auth(request, csrf=True)
    body, _, _ = await state.fetch_upstream(force=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        servers = await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
    return {"count": len(servers), "drive_sync": await state.push_drive_sync()}


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
    return {"ok": True, "drive_sync": await state.push_drive_sync([user_id])}


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
        rows = await conn.fetch("""SELECT s.id,s.original_name,s.custom_name AS global_name,s.hidden AS global_hidden,
            s.source,s.sort_order,s.present_upstream,us.hidden AS user_hidden,us.custom_name AS user_name
            FROM servers s LEFT JOIN user_servers us ON us.server_id=s.id AND us.user_id=$1
            WHERE s.present_upstream=TRUE ORDER BY s.sort_order,s.id""", user_id)
    return {"servers": [_serialize(row) for row in rows]}


@router.get("/api/admin/settings")
async def settings_get(request: Request):
    _auth(request)
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
                     "provider_id_configured": bool(re.fullmatch(r"[A-Za-z0-9]{8}", os.getenv("HAPP_PROVIDER_ID", "").strip()))},
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
    if "announce_refresh_minutes" in values:
        try: values["announce_refresh_minutes"] = max(5, min(1440, int(values["announce_refresh_minutes"])))
        except Exception: raise HTTPException(422, "announce_refresh_minutes должен быть числом не меньше 5")
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
    if "announce_enabled" in values and not isinstance(values["announce_enabled"], bool):
        raise HTTPException(422, "announce_enabled должен быть логическим значением")
    if "hide_traffic_for_new_users" in values and not isinstance(values["hide_traffic_for_new_users"], bool):
        raise HTTPException(422, "hide_traffic_for_new_users должен быть логическим значением")
    if "include_sas_for_new_users" in values and not isinstance(values["include_sas_for_new_users"], bool):
        raise HTTPException(422, "include_sas_for_new_users должен быть логическим значением")
    for key in ("quotes", "footers", "days"):
        if key in values and (not isinstance(values[key], list) or len(values[key]) > 100 or any(not isinstance(x, str) or len(x) > 500 for x in values[key])):
            raise HTTPException(422, f"Некорректное поле {key}")
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in values.items():
                await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, json.dumps(value, ensure_ascii=False))
    state.announce_cache = None
    drive_sync = await state.push_drive_sync() if set(values) - {
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
        if not selected_ids or len(selected_ids) > 30 or any(value <= 0 for value in selected_ids):
            raise HTTPException(422, "Некорректный список пользователей")

    pool = await state.db()
    query = "SELECT * FROM users WHERE id=ANY($1::bigint[]) ORDER BY id" if selected_ids else "SELECT * FROM users ORDER BY id"
    async with pool.acquire() as conn:
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
    try:
        # The five-minute Apps Script poll bypasses the SAS cache so every
        # scheduled manifest request checks for newly posted Telegram servers.
        needs_sas = any(_user_needs_sas(user) for user in rows_before_fetch)
        body, headers, _stale = await state.fetch_upstream(
            force_sas=True, allow_sas_failure=not needs_sas)
    except HTTPException:
        async with pool.acquire() as conn:
            if selected_ids:
                rows = await conn.fetch("SELECT * FROM users WHERE id=ANY($1::bigint[]) AND status='pending_delete' ORDER BY id", selected_ids)
            else:
                rows = await conn.fetch("SELECT * FROM users WHERE status='pending_delete' ORDER BY id")
        if not rows:
            raise
        manifest = [{"id": user["id"], "slug": user["slug"], "status": user["status"],
                     "enabled": user["enabled"], "drive_file_id": user["drive_file_id"],
                     "name": user["name"], "content": ""} for user in rows]
        log.warning("Upstream unavailable; returning deletion-only manifest: users=%d", len(manifest))
        return {"users": manifest, "partial": True, "generated_at": datetime.now(timezone.utc).isoformat()}

    async with pool.acquire() as conn:
        settings = await _settings(conn)
        servers = await _sync_servers(conn, body, primary_body=state.upstream_body, sas_body=state.sas_body)
        if selected_ids:
            rows = await conn.fetch("SELECT * FROM users WHERE id=ANY($1::bigint[]) ORDER BY id", selected_ids)
        else:
            rows = await conn.fetch("SELECT * FROM users ORDER BY id")
        # Same announcement text shared across every user in this manifest response.
        weather_payload = _weather_payload_from_sync_request(request)
        announce = await _announce(settings, weather_payload) if settings.get("announce_enabled", True) else ""
        manifest = []
        for user in rows:
            content = "" if user["status"] == "pending_delete" else await build_manifest_content(conn, user, body, headers, settings, announce, servers)
            manifest.append({"id": user["id"], "slug": user["slug"], "status": user["status"], "enabled": user["enabled"],
                             "drive_file_id": user["drive_file_id"], "name": user["name"], "content": content})
    log.info("Apps Script manifest generated: users=%d stale-cache=%s", len(manifest), "yes" if _stale else "no")
    return {"users": manifest, "generated_at": datetime.now(timezone.utc).isoformat()}


class SyncReport(BaseModel):
    user_id: int
    drive_file_id: str | None = Field(default=None, max_length=256)
    ok: bool
    error: str | None = Field(default=None, max_length=1000)


@router.post("/api/sync/report")
async def sync_report(data: SyncReport, request: Request):
    _check_sync(request)
    pool = await state.db()
    safe_error = _safe_sync_error(data.error) if not data.ok else None
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO app_state(key,value) VALUES('last_script_request',to_jsonb(now())) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")
        user = await conn.fetchrow("SELECT id,status,device_limit FROM users WHERE id=$1", data.user_id)
        if not user: return {"ok": True, "ignored": True}
        if user["status"] == "pending_delete" and data.ok:
            await conn.execute("DELETE FROM users WHERE id=$1", data.user_id)
            return {"ok": True, "deleted": True}
        await conn.execute("""UPDATE users SET drive_file_id=COALESCE($2,drive_file_id),last_synced_at=CASE WHEN $3 THEN now() ELSE last_synced_at END,
            last_sync_error=$4,updated_at=now() WHERE id=$1""", data.user_id, data.drive_file_id, data.ok, safe_error)
    if data.ok and user["device_limit"] is not None:
        _schedule_limited_link(data.user_id)
    return {"ok": True}


def install(app: FastAPI):
    app.add_middleware(PanelSecurityMiddleware)
    app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="panel-static")
    app.include_router(router)
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
