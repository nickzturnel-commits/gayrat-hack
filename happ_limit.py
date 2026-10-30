"""Happ Limited Links API client.

This module only creates install codes and encrypts Drive URLs. Device counting
is performed by Happ when the user adds the resulting link in the Happ app.
"""
from __future__ import annotations

import json
import logging
import re

HAPP_ADD_INSTALL_URL = "https://api.happ-proxy.com/api/add-install"
HAPP_CRYPTO_URL = "https://crypto.happ.su/api-v2.php"
MIN_LIMIT = 1
# Happ documents 1..100; Secure X intentionally caps the selectable value at 99.
MAX_LIMIT = 99
REQUEST_TIMEOUT = 15.0
PROVIDER_CODE_RE = re.compile(r"^[A-Za-z0-9]{8}$")
AUTH_KEY_RE = re.compile(r"^[-_A-Za-z0-9]{32}$")
INSTALL_CODE_RE = re.compile(r"^[A-Za-z0-9]{12}$")
ENCRYPTED_PREFIX = "happ://crypt"

# The Happ API requires auth_key in the query string. Prevent httpx's INFO
# request logging from printing the complete request URL and key.
logging.getLogger("httpx").setLevel(logging.WARNING)


class HappLimitError(Exception):
    """Safe-to-display error returned by the Happ integration."""


def is_configured(provider_code: str | None, auth_key: str | None) -> bool:
    return bool(PROVIDER_CODE_RE.fullmatch((provider_code or "").strip())
                and AUTH_KEY_RE.fullmatch((auth_key or "").strip()))


def _redact(message: str, *secrets: str) -> str:
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    for secret in secrets:
        if secret and secret.strip():
            text = text.replace(secret.strip(), "[скрыто]")
    text = re.sub(r"https?://\S+", "[адрес скрыт]", text)
    return text[:200]


def limited_source_url(drive_url: str, install_code: str) -> str:
    """Append Happ's InstallID fragment before encrypting the Drive URL."""
    if not INSTALL_CODE_RE.fullmatch(install_code or ""):
        raise HappLimitError("Happ вернул некорректный код установки")
    if not (drive_url or "").startswith("https://"):
        raise HappLimitError("Ссылка Drive должна начинаться с https://")
    if "#" in drive_url:
        raise HappLimitError("Ссылка Drive уже содержит фрагмент URL")
    return f"{drive_url}#?installid={install_code}"


async def create_install_code(client, provider_code: str, auth_key: str, limit: int) -> str:
    if not is_configured(provider_code, auth_key):
        raise HappLimitError("Не заданы HAPP_PROVIDER_CODE и HAPP_AUTH_KEY")
    provider_code = provider_code.strip()
    auth_key = auth_key.strip()
    if not PROVIDER_CODE_RE.fullmatch(provider_code):
        raise HappLimitError("HAPP_PROVIDER_CODE должен содержать 8 букв или цифр")
    if not AUTH_KEY_RE.fullmatch(auth_key):
        raise HappLimitError("HAPP_AUTH_KEY должен содержать 32 допустимых символа")
    if isinstance(limit, bool) or not isinstance(limit, int) or not (MIN_LIMIT <= limit <= MAX_LIMIT):
        raise HappLimitError(f"Лимит устройств должен быть целым числом от {MIN_LIMIT} до {MAX_LIMIT}")
    try:
        response = await client.get(
            HAPP_ADD_INSTALL_URL,
            params={"provider_code": provider_code, "auth_key": auth_key, "install_limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:
        # The exception may contain the complete URL with auth_key.
        raise HappLimitError(f"Сервис Happ недоступен ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise HappLimitError(f"Happ вернул HTTP {response.status_code} при создании счётчика")
    try:
        payload = json.loads(response.text)
    except (ValueError, TypeError):
        raise HappLimitError("Happ вернул ответ в неизвестном формате") from None
    if not isinstance(payload, dict) or payload.get("rc") != 1:
        reason = _redact(payload.get("msg") if isinstance(payload, dict) else "", provider_code, auth_key)
        raise HappLimitError("Happ отклонил создание счётчика" + (f": {reason}" if reason else ""))
    code = str(payload.get("install_code") or "")
    if not INSTALL_CODE_RE.fullmatch(code):
        raise HappLimitError("Happ вернул некорректный код установки")
    return code


def _find_encrypted(value) -> str | None:
    if isinstance(value, str):
        candidate = value.strip().strip('"').strip()
        if candidate.startswith(ENCRYPTED_PREFIX) and not re.search(r"\s", candidate):
            return candidate
    elif isinstance(value, dict):
        for item in value.values():
            found = _find_encrypted(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_encrypted(item)
            if found:
                return found
    return None


def extract_encrypted_link(text: str) -> str | None:
    """Accept a plain link or a nested JSON result; Happ's v2 response shape is undocumented."""
    body = (text or "").strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return _find_encrypted(body)
    return _find_encrypted(parsed)


async def encrypt_link(client, url: str) -> str:
    try:
        response = await client.post(HAPP_CRYPTO_URL, json={"url": url}, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        raise HappLimitError(f"Сервис шифрования Happ недоступен ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise HappLimitError(f"Сервис шифрования Happ вернул HTTP {response.status_code}")
    link = extract_encrypted_link(response.text)
    if not link:
        raise HappLimitError("Сервис шифрования Happ не вернул ссылку happ://crypt")
    return link


async def build_limited_link(client, provider_code: str, auth_key: str, drive_url: str,
                             limit: int, existing_code: str | None = None) -> tuple[str, str]:
    """Return (install_code, encrypted_link), reusing a code when requested."""
    code = existing_code or await create_install_code(client, provider_code, auth_key, limit)
    return code, await encrypt_link(client, limited_source_url(drive_url, code))
