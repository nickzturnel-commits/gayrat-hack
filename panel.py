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
from urllib.parse import quote, unquote, urlsplit

import asyncpg
import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("vpn_panel")
router = APIRouter()

LEGACY_DRIVE_FILE_ID = "160lsZ2nDrSXTzA09r9jeGHnd6QyFylw9"
PANEL_UPSTREAM_DEFAULT_USER_AGENT = "Happ"
UPSTREAM_TIMEOUT_SECONDS = 20.0
PANEL_CACHE_TTL_SECONDS = 300
SESSION_SECONDS = 12 * 60 * 60
DB_HINT = ("Не удалось подключиться к базе. Если используется прямой хост db.*.supabase.co, "
           "возьмите в Supabase (Connect → Session pooler) строку подключения и поставьте её в DATABASE_URL")
URI_LINE = re.compile(r"(?im)^\s*(?:vless|vmess|trojan|ss|hysteria2?|tuic)://\S+\s*$")
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
    except (ValueError, UnicodeDecodeError):
        pass
    return text


def _vmess_obj(uri: str) -> tuple[dict, str]:
    payload, _, fragment = uri[8:].partition("#")
    raw = base64.b64decode(payload + "=" * ((4 - len(payload) % 4) % 4), altchars=b"-_", validate=False)
    obj = json.loads(raw.decode("utf-8"))
    return obj, unquote(str(obj.get("ps") or fragment))


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
    return {"id": server_id, "uri": value, "name": name or fallback_name, "scheme": scheme}


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
    uris = []
    seen = set()
    for line in text.splitlines():
        match = URI_LINE.match(line)
        if not match:
            continue
        uri = match.group(0).strip()
        server_id = parse_server_uri(uri)["id"]
        if server_id not in seen:
            seen.add(server_id)
            uris.append(uri)
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
    if not any(URI_ANY.search(item.decode("utf-8", "ignore")) for item in candidates):
        return "Апстрим не содержит VPN-серверов"
    try:
        decoded = _decode_subscription(body)
        uris = _extract_uris(decoded)
        if not uris:
            return "Апстрим не содержит серверов по одному URI на строку"
        for uri in uris:
            parse_server_uri(uri)
    except Exception:
        return "Апстрим вернул некорректную запись сервера"
    return None


class PanelState:
    def __init__(self):
        self.pool = None
        self.db_error = "База ещё не проверялась"
        self.db_last_attempt = 0.0
        self.upstream_client: httpx.AsyncClient | None = None
        self.upstream_body: bytes | None = None
        self.upstream_headers: dict[str, str] = {}
        self.upstream_fetched_at = 0.0
        self.upstream_attempt_at = 0.0
        self.upstream_warning: str | None = None
        self.upstream_lock = asyncio.Lock()
        self.announce_cache: tuple[float, str] | None = None
        self.announce_quote: str | None = None
        self.login_fails: dict[str, tuple[int, float]] = {}
        self.drive_push_last: float = 0.0
        self.drive_push_error: str | None = None

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
            if not isinstance(result, dict) or not result.get("ok"):
                raise ValueError("Apps Script не подтвердил доставку")
            self.drive_push_last = time.time()
            failed = int(result.get("failed", 0) or 0)
            self.drive_push_error = f"Не удалось обновить файлов: {failed}" if failed else None
            return {"ok": failed == 0, "pending": False,
                    "message": self.drive_push_error or "Изменения сразу отправлены в Google Drive",
                    "processed": int(result.get("processed", 0) or 0), "failed": failed}
        except Exception as exc:
            self.drive_push_error = "Не удалось сразу обновить Google Drive; автоматическая попытка повторится позже"
            log.warning("Immediate Drive update failed (%s)", type(exc).__name__)
            return {"ok": False, "pending": True, "message": self.drive_push_error}

    async def fetch_upstream(self, force=False):
        now = time.monotonic()
        fresh = self.upstream_body is not None and now - self.upstream_fetched_at < PANEL_CACHE_TTL_SECONDS
        if fresh and not force:
            return self.upstream_body, self.upstream_headers, False
        async with self.upstream_lock:
            now = time.monotonic()
            fresh = self.upstream_body is not None and now - self.upstream_fetched_at < PANEL_CACHE_TTL_SECONDS
            if fresh and not force:
                return self.upstream_body, self.upstream_headers, False
            url = os.getenv("SUBSCRIPTION_URL", "").strip()
            if not url:
                self.upstream_warning = "SUBSCRIPTION_URL не задан"
                if self.upstream_body is not None:
                    return self.upstream_body, self.upstream_headers, True
                raise HTTPException(503, self.upstream_warning)
            if self.upstream_client is None:
                self.upstream_client = httpx.AsyncClient(follow_redirects=True, timeout=UPSTREAM_TIMEOUT_SECONDS)
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
            self.upstream_attempt_at = time.time()
            try:
                response = await self.upstream_client.get(url, headers=headers)
                if response.status_code >= 400:
                    raise ValueError(f"Апстрим ответил HTTP {response.status_code}")
                invalid = _invalid_payload(response.content, response.headers.get("content-type", ""))
                if invalid:
                    self.upstream_warning = invalid
                    log.warning("Panel upstream update rejected: %s; status=%d bytes=%d", invalid, response.status_code, len(response.content))
                    if self.upstream_body is not None:
                        return self.upstream_body, self.upstream_headers, True
                    raise HTTPException(502, invalid)
                self.upstream_body = response.content
                self.upstream_headers = {k.lower(): v for k, v in response.headers.items()}
                self.upstream_fetched_at = time.monotonic()
                self.upstream_warning = None
                log.info("Panel upstream cache updated: bytes=%d; servers=%d", len(response.content), len(_extract_uris(_decode_subscription(response.content))))
                return self.upstream_body, self.upstream_headers, False
            except HTTPException:
                raise
            except Exception as exc:
                self.upstream_warning = "Не удалось получить актуальную подписку от апстрима"
                log.warning("Panel upstream request failed (%s)", type(exc).__name__)
                if self.upstream_body is not None:
                    return self.upstream_body, self.upstream_headers, True
                raise HTTPException(502, self.upstream_warning) from exc


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
    return dict(row) if row is not None else None


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


async def _announce(settings: dict) -> str:
    refresh = max(5, int(settings.get("announce_refresh_minutes", 5))) * 60
    now = time.monotonic()
    if state.announce_cache and now - state.announce_cache[0] < refresh:
        return state.announce_cache[1]
    local = _now_for_tz(settings.get("timezone", "Asia/Ashgabat"))
    lines = []
    days = settings.get("days") or DAY_DEFAULTS
    quotes = settings.get("quotes") or QUOTE_DEFAULTS
    footers = settings.get("footers") or FOOTER_DEFAULTS
    day_line = str(days[local.isoweekday() - 1]) if len(days) >= 7 else ""
    greeting = "🌙 Доброй ночи!" if local.hour < 5 else "🌅 Доброе утро!" if local.hour < 12 else "☀️ Добрый день!" if local.hour < 18 else "🌆 Добрый вечер!"
    weather = ""
    try:
        if state.upstream_client is None:
            state.upstream_client = httpx.AsyncClient(follow_redirects=True, timeout=UPSTREAM_TIMEOUT_SECONDS)
        w = await state.upstream_client.get("https://api.open-meteo.com/v1/forecast?latitude=37.95&longitude=58.38&current_weather=true", headers={"User-Agent": PANEL_UPSTREAM_DEFAULT_USER_AGENT}, timeout=5.0)
        data = w.json().get("current_weather", {})
        code = int(data.get("weathercode", -1)); temp = data.get("temperature")
        emoji = "☀️" if code == 0 else "⛅" if 1 <= code <= 3 else "🌫" if code in (45, 48) else "🌧" if 51 <= code <= 67 or 80 <= code <= 82 else "❄️" if 71 <= code <= 77 else "⛈" if code >= 95 else ""
        if emoji and temp is not None: weather = f"{emoji} Ашхабад сейчас: {temp}°C"
    except Exception:
        pass
    top_lines = [line for line in (day_line, greeting, weather) if line]
    lines.append(secrets.choice(top_lines) if top_lines else greeting)
    candidates = [str(q) for q in quotes if str(q)]
    quote_text = secrets.choice(candidates) if candidates else ""
    if len(candidates) > 1 and quote_text == state.announce_quote:
        quote_text = candidates[(candidates.index(quote_text) + 1) % len(candidates)]
    state.announce_quote = quote_text
    footer_tpl = secrets.choice([str(x) for x in footers if str(x)]) if footers else ""
    lines.extend(["", quote_text, "", footer_tpl.replace("{t}", local.strftime("%H:%M:%S"))])
    value = "\n".join(lines).strip()
    state.announce_cache = (now, value)
    return value


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


async def _sync_servers(conn, body: bytes):
    text = _decode_subscription(body)
    uris = _extract_uris(text)
    if not uris:
        raise HTTPException(502, "В списке апстрима нет VPN-серверов")
    parsed = [parse_server_uri(uri) for uri in uris]
    async with conn.transaction():
        await conn.execute("UPDATE servers SET present_upstream=FALSE")
        for index, server in enumerate(parsed):
            await conn.execute("""INSERT INTO servers(id,original_name,sort_order,present_upstream,last_seen_at)
                VALUES($1,$2,$3,TRUE,now()) ON CONFLICT(id) DO UPDATE SET original_name=EXCLUDED.original_name,
                present_upstream=TRUE,last_seen_at=now()""", server["id"], server["name"], index)
    return parsed


async def build_manifest_content(conn, user, body: bytes, headers: dict, settings: dict,
                                 announce: str | None = None, servers: list[dict] | None = None) -> str:
    if servers is None:
        servers = await _sync_servers(conn, body)
    now = datetime.now(timezone.utc)
    expired = user["expires_at"] is not None and user["expires_at"] <= now
    active = bool(user["enabled"]) and not expired and user["status"] == "active"
    if not active:
        announcement = "⛔ Подписка истекла, обратитесь в поддержку" if expired else "⛔ Подписка отключена, обратитесь в поддержку"
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
        title = f"Подписка истекла · {user.get('name', user.get('slug', 'Пользователь'))}"
    elif not user["enabled"] or user["status"] != "active":
        title = f"Подписка отключена · {user.get('name', user.get('slug', 'Пользователь'))}"
    lines = ["#profile-title: base64:" + base64.b64encode(title.encode()).decode(),
             f"#profile-update-interval: {int(settings.get('update_interval', 1))}",
             "#announce: base64:" + base64.b64encode(announcement.encode()).decode(),
             "#support-url: " + str(settings.get("support_url", "https://t.me/moserrr1"))]
    is_legacy = user.get("slug") == "legacy" or user.get("drive_file_id") == LEGACY_DRIVE_FILE_ID
    if is_legacy or not bool(user.get("hide_traffic_usage", False)):
        expiry = int(user["expires_at"].timestamp()) if user.get("expires_at") else None
        lines.append("#subscription-userinfo: " + _userinfo(headers, expire_override=expiry))
    web_url = str(settings.get("web_page_url", "")).strip()
    if web_url:
        lines.append("#profile-web-page-url: " + web_url)
    if active:
        global_rows = await conn.fetch("SELECT id,original_name,custom_name,hidden,sort_order FROM servers WHERE present_upstream=TRUE ORDER BY sort_order,id")
        upstream_by_id = {server["id"]: server for server in servers}
        overrides = await conn.fetch("SELECT server_id,hidden,custom_name FROM user_servers WHERE user_id=$1", user["id"])
        overrides_by_id = {row["server_id"]: row for row in overrides}
        server_lines = []
        for global_row in global_rows:
            server = upstream_by_id.get(global_row["id"])
            if not server:
                continue
            override = overrides_by_id.get(server["id"])
            if override and override["hidden"] is True: continue
            if override and override["hidden"] is None and global_row["hidden"]: continue
            if not override and global_row["hidden"]: continue
            name = (override["custom_name"] if override and override["custom_name"] else None) or global_row["custom_name"] or server["name"]
            server_lines.append(rename_server_uri(server["uri"], name) if name != server["name"] else server["uri"])
        if not server_lines:
            # Do not let Apps Script replace a working Drive profile with a
            # metadata-only file that Happ rejects as having no server links.
            raise HTTPException(502, "Для этой подписки нет доступных серверов. Обновите список серверов и проверьте, что они не скрыты.")
        lines.extend(server_lines)
    else:
        reason = "Подписка истекла" if expired else "Подписка отключена"
        lines.append("vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?type=tcp&encryption=none#" + quote(reason, safe=""))
    return "\n".join(lines) + "\n"


def _secure_headers(response: Response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    # Admin HTML and API responses contain changing user/config data. Prevent
    # browsers and intermediary caches from showing an older panel revision.
    response.headers["Cache-Control"] = "no-store"


class PanelSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin") or request.url.path.startswith("/api/"):
            _secure_headers(response)
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
    pool = await state.db(required=False)
    db_info = {"connected": pool is not None, "error": state.db_error if pool is None else None}
    user_count = {"active": 0, "disabled": 0, "total": 0}
    errors = []
    last_script = None
    if pool:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT enabled,status,expires_at FROM users")
            user_count["total"] = len(rows)
            for row in rows:
                if row["enabled"] and row["status"] == "active" and (row["expires_at"] is None or row["expires_at"] > datetime.now(timezone.utc)): user_count["active"] += 1
                else: user_count["disabled"] += 1
            errors = [dict(r) for r in await conn.fetch("SELECT id,name,last_sync_error,last_synced_at FROM users WHERE last_sync_error IS NOT NULL ORDER BY updated_at DESC")]
            row = await conn.fetchrow("SELECT value FROM app_state WHERE key='last_script_request'")
            if row: last_script = row["value"]
            info = _userinfo(state.upstream_headers)
    else:
        info = _userinfo(state.upstream_headers)
    parts = dict(token.strip().split("=", 1) for token in info.split(";") if "=" in token)
    data = {k: int(v) if str(v).isdigit() else 0 for k, v in parts.items()}
    return {"database": db_info, "upstream": {"cached": state.upstream_body is not None,
            "last_success_at": state.upstream_fetched_at or None, "server_count": len(_extract_uris(_decode_subscription(state.upstream_body))) if state.upstream_body else 0,
            "warning": state.upstream_warning}, "traffic": {"upload": data.get("upload", 0), "download": data.get("download", 0),
            "total": data.get("total", 0), "expire": data.get("expire", 0)}, "users": user_count,
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
    return {"users": [_serialize(row) for row in rows]}


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
        row = await conn.fetchrow("""INSERT INTO users(name,slug,note,enabled,expires_at,title_override,announce_override,traffic_limit_bytes,hide_traffic_usage)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""", data.name.strip(), slug, data.note, data.enabled,
            _expiry_utc(data.expires_at), data.title_override, data.announce_override, data.traffic_limit_bytes, hide_traffic)
    drive_sync = await state.push_drive_sync([row["id"]])
    return {"user": _serialize(row), "drive_sync": drive_sync}


@router.put("/api/admin/users/{user_id}")
async def users_update(user_id: int, data: UserBody, request: Request):
    _auth(request, csrf=True)
    if data.title_override and any(ord(c) < 32 for c in data.title_override):
        raise HTTPException(422, "Заголовок профиля не должен содержать переносы строк")
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""UPDATE users SET name=$2,note=$3,enabled=$4,expires_at=$5,title_override=$6,
            announce_override=$7,traffic_limit_bytes=$8,
            hide_traffic_usage=CASE WHEN slug='legacy' OR drive_file_id=$10 THEN FALSE ELSE COALESCE($9,hide_traffic_usage) END,
            updated_at=now() WHERE id=$1 AND status='active' RETURNING *""", user_id,
            data.name.strip(), data.note, data.enabled, _expiry_utc(data.expires_at), data.title_override, data.announce_override,
            data.traffic_limit_bytes, data.hide_traffic_usage, LEGACY_DRIVE_FILE_ID)
    if not row: raise HTTPException(404, "Пользователь не найден")
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
    body, headers, _ = await state.fetch_upstream()
    async with pool.acquire() as conn:
        settings = await _settings(conn)
        user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        if not user: raise HTTPException(404, "Пользователь не найден")
        content = await build_manifest_content(conn, user, body, headers, settings)
    return PlainTextResponse(content, headers={"Cache-Control": "no-store"})


@router.get("/api/admin/users/{user_id}/drive-link")
async def drive_link(user_id: int, request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT drive_file_id FROM users WHERE id=$1", user_id)
    if not user: raise HTTPException(404, "Пользователь не найден")
    if not user["drive_file_id"]: return {"link": None, "pending": True}
    api_key = os.getenv("DRIVE_API_KEY", "")
    if not api_key: raise HTTPException(503, "DRIVE_API_KEY не задан")
    from urllib.parse import urlencode
    query = urlencode({"key": api_key, "alt": "media"})
    return {"link": f"https://www.googleapis.com/drive/v3/files/{user['drive_file_id']}?{query}", "pending": False}


@router.post("/api/admin/drive/sync")
async def drive_sync_now(request: Request):
    _auth(request, csrf=True)
    return await state.push_drive_sync()


@router.get("/api/admin/servers")
async def servers_list(request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM servers ORDER BY sort_order,id")
    return {"servers": [_serialize(r) for r in rows]}


@router.put("/api/admin/servers/{server_id}")
async def servers_update(server_id: str, data: ServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE servers SET custom_name=$2,hidden=$3,sort_order=$4 WHERE id=$1 RETURNING *", server_id, data.custom_name, data.hidden, data.sort_order)
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
        result = await conn.execute("UPDATE servers SET hidden=$1 WHERE id=ANY($2::text[])", data.hidden, data.server_ids)
    return {"ok": True, "updated": int(result.rsplit(" ", 1)[-1]), "drive_sync": await state.push_drive_sync()}


@router.post("/api/admin/servers/refresh")
async def servers_refresh(request: Request):
    _auth(request, csrf=True)
    body, _, _ = await state.fetch_upstream(force=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        servers = await _sync_servers(conn, body)
    return {"count": len(servers), "drive_sync": await state.push_drive_sync()}


@router.put("/api/admin/users/{user_id}/servers/{server_id}")
async def user_server_update(user_id: int, server_id: str, data: UserServerBody, request: Request):
    _auth(request, csrf=True)
    pool = await state.db()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", user_id)
        if not exists: raise HTTPException(404, "Пользователь не найден")
        await conn.execute("""INSERT INTO user_servers(user_id,server_id,hidden,custom_name) VALUES($1,$2,$3,$4)
            ON CONFLICT(user_id,server_id) DO UPDATE SET hidden=EXCLUDED.hidden,custom_name=EXCLUDED.custom_name""", user_id, server_id, data.hidden, data.custom_name)
    return {"ok": True, "drive_sync": await state.push_drive_sync([user_id])}


@router.get("/api/admin/users/{user_id}/servers")
async def user_servers_list(user_id: int, request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id=$1)", user_id):
            raise HTTPException(404, "Пользователь не найден")
        rows = await conn.fetch("""SELECT s.id,s.original_name,s.custom_name AS global_name,s.hidden AS global_hidden,
            s.sort_order,s.present_upstream,us.hidden AS user_hidden,us.custom_name AS user_name
            FROM servers s LEFT JOIN user_servers us ON us.server_id=s.id AND us.user_id=$1
            ORDER BY s.sort_order,s.id""", user_id)
    return {"servers": [_serialize(row) for row in rows]}


@router.get("/api/admin/settings")
async def settings_get(request: Request):
    _auth(request)
    pool = await state.db()
    async with pool.acquire() as conn:
        return {"settings": await _settings(conn)}


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
    for key in ("quotes", "footers", "days"):
        if key in values and (not isinstance(values[key], list) or len(values[key]) > 100 or any(not isinstance(x, str) or len(x) > 500 for x in values[key])):
            raise HTTPException(422, f"Некорректное поле {key}")
    pool = await state.db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in values.items():
                await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, json.dumps(value, ensure_ascii=False))
    state.announce_cache = None
    drive_sync = await state.push_drive_sync() if set(values) - {"default_traffic_limit_bytes", "hide_traffic_for_new_users"} else None
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
    for name in ("DATABASE_URL", "ADMIN_PASSWORD", "SESSION_SECRET", "SYNC_KEY", "DRIVE_API_KEY", "SUBSCRIPTION_URL"):
        secret = os.getenv(name, "")
        if secret:
            value = value.replace(secret, "[секрет скрыт]")
    value = re.sub(r"https?://\S+", "[адрес скрыт]", value, flags=re.I)
    value = re.sub(r"(?i)(?:vless|vmess|trojan|ss|hysteria2?|tuic)://\S+", "[сервер скрыт]", value)
    return value[:1000]


@router.get("/api/sync/manifest")
async def sync_manifest(request: Request):
    _check_sync(request)
    # Fetch before touching DB/output. Cold starts and expired cache trigger this request directly.
    body, headers, _stale = await state.fetch_upstream()
    pool = await state.db()
    selected_ids = None
    selected = request.query_params.get("user_ids", "").strip()
    if selected:
        try:
            selected_ids = list(dict.fromkeys(int(part) for part in selected.split(",") if part))
        except ValueError:
            raise HTTPException(422, "Некорректный список пользователей")
        if not selected_ids or len(selected_ids) > 30 or any(value <= 0 for value in selected_ids):
            raise HTTPException(422, "Некорректный список пользователей")
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO app_state(key,value) VALUES('last_script_request',to_jsonb(now())) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")
        settings = await _settings(conn)
        servers = await _sync_servers(conn, body)
        if selected_ids:
            rows = await conn.fetch("SELECT * FROM users WHERE id=ANY($1::bigint[]) ORDER BY id", selected_ids)
        else:
            rows = await conn.fetch("SELECT * FROM users ORDER BY id")
        # Same announcement text shared across every user in this manifest response.
        announce = await _announce(settings) if settings.get("announce_enabled", True) else ""
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
        user = await conn.fetchrow("SELECT id,status FROM users WHERE id=$1", data.user_id)
        if not user: return {"ok": True, "ignored": True}
        if user["status"] == "pending_delete" and data.ok:
            await conn.execute("DELETE FROM users WHERE id=$1", data.user_id)
            return {"ok": True, "deleted": True}
        await conn.execute("""UPDATE users SET drive_file_id=COALESCE($2,drive_file_id),last_synced_at=CASE WHEN $3 THEN now() ELSE last_synced_at END,
            last_sync_error=$4,updated_at=now() WHERE id=$1""", data.user_id, data.drive_file_id, data.ok, safe_error)
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
