"""Authenticated MCP tools for operating the primary admin panel."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

import httpx
from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import BaseRoute, Match, Mount
from starlette.types import Receive, Scope, Send
from starlette._utils import get_route_path


log = logging.getLogger(__name__)
_SECRET_KEYS = {
    "activation_url", "drive_link", "drive_file_id", "limited_link", "happ_install_code",
    "token_nonce", "token_hash", "browser_hash", "device_hash", "encrypted_link",
}
_MAX_MCP_IDS = 500


class _ExactPathASGIRoute(BaseRoute):
    """Dispatch the MCP app at the mount prefix with or without a trailing slash."""

    def __init__(self, path: str, child_app):
        self.path = path.rstrip("/")
        self.child_app = child_app

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http" or get_route_path(scope) != self.path:
            return Match.NONE, {}
        root_path = scope.get("root_path", "")
        path = scope["path"].rstrip("/") + "/"
        return Match.FULL, {
            "path": path,
            "raw_path": path.encode("ascii"),
            "root_path": root_path + self.path,
            "endpoint": self.child_app,
        }

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.child_app(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params: Any):
        if name == "mcp-agent-exact" and not path_params:
            from starlette.datastructures import URLPath

            return URLPath(self.path, name=name)
        from starlette.routing import NoMatchFound

        raise NoMatchFound(name, path_params)


def _public(value: Any) -> Any:
    """Remove bearer links and internal identifiers from ordinary tool results."""
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def install(app: FastAPI, panel) -> bool:
    """Mount the MCP endpoint when a valid 64-hex secret is configured."""
    secret = os.getenv("MCP_HASH", "").strip() or os.getenv("mcp_hash", "").strip()
    if not secret:
        log.info("MCP admin endpoint is disabled (mcp_hash is not configured)")
        return False
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", secret):
        raise RuntimeError("mcp_hash must contain exactly 64 hexadecimal characters")

    mcp = MCPServer(
        name="zeva-admin",
        title="Zeva Admin",
        description="Управление основной админ-панелью Zeva Online.",
        instructions=(
            "Работай только с основной средой /admin. Для любых изменений используй инструменты панели. "
            "Массовые изменения выполняй пакетными инструментами. Перед созданием, изменением, удалением, "
            "сбросом или синхронизацией передай confirm=true. Секретные ссылки возвращай только по явному запросу."
        ),
        version="1.0.0",
    )

    async def admin_api(method: str, path: str, data: dict | None = None) -> Any:
        """Call an existing primary admin API route in-process using a short-lived signed session."""
        if not path.startswith("/api/admin/") or path.startswith("/api/admin/test"):
            raise ToolError("Только API основной админ-панели доступно через этот инструмент")
        username = os.getenv("ADMIN_USERNAME", "").strip()
        session_secret = os.getenv("SESSION_SECRET", "")
        if not username or len(session_secret) < 32:
            raise ToolError("Основная админ-панель не настроена")
        csrf = secrets.token_urlsafe(24)
        session = panel._sign_session({
            "u": username,
            "c": csrf,
            "environment": "main",
            "exp": int(time.time()) + 120,
        })
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://zeva-online.onrender.com",
                timeout=httpx.Timeout(120.0),
                cookies={"admin_session": session},
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=data,
                    headers={"x-csrf-token": csrf, "accept": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise ToolError(f"Панель не ответила вовремя ({type(exc).__name__}); проверь состояние операции отдельно.") from None
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "")
            except Exception:
                detail = response.text[:500]
            raise ToolError(f"Панель вернула HTTP {response.status_code}: {detail}")
        if "application/json" in content_type:
            return response.json()
        return response.text

    @asynccontextmanager
    async def main_database():
        token = panel._test_environment.set(False)
        try:
            pool = await panel.state.db()
            yield pool
        finally:
            panel._test_environment.reset(token)

    async def require_confirm(confirm: bool) -> None:
        if confirm is not True:
            raise ToolError("Подтвердите действие параметром confirm=true")

    async def raw_subscriptions() -> list[dict]:
        result = await admin_api("GET", "/api/admin/users")
        return list(result.get("users", []))

    @mcp.tool()
    async def get_dashboard() -> dict[str, Any]:
        """Показывает сводку /admin: источники, серверы, пользователи, БД, Drive-доставка и ошибки."""
        return _public(await admin_api("GET", "/api/admin/dashboard"))

    @mcp.tool()
    async def get_operational_status() -> dict[str, Any]:
        """Возвращает статус БД, провайдеров, Happ и синхронизации из настроек панели."""
        dashboard, settings = await asyncio.gather(
            admin_api("GET", "/api/admin/dashboard"),
            admin_api("GET", "/api/admin/settings"),
        )
        return _public({"dashboard": dashboard, "settings_status": settings})

    @mcp.tool()
    async def list_subscriptions() -> dict[str, Any]:
        """Перечисляет все подписки основной панели без секретных ссылок и ID Drive."""
        result = await admin_api("GET", "/api/admin/users")
        return {"subscriptions": _public(result.get("users", [])),
                "device_limit_bounds": result.get("device_limit_bounds")}

    @mcp.tool()
    async def get_subscription(user_id: int) -> dict[str, Any]:
        """Показывает одну подписку по ID без bearer-ссылок и секретных идентификаторов."""
        users = await raw_subscriptions()
        user = next((item for item in users if int(item.get("id", -1)) == user_id), None)
        if user is None:
            raise ToolError("Подписка с таким ID не найдена в основной среде. Сначала проверь ID через list_subscriptions.")
        return _public(user)

    @mcp.tool()
    async def create_subscription(subscription: dict[str, Any], confirm: bool) -> dict[str, Any]:
        """Создаёт подписку через штатную валидацию панели. Передай поля UserBody; действие требует confirm=true."""
        await require_confirm(confirm)
        result = await admin_api("POST", "/api/admin/users", subscription)
        return _public(result)

    @mcp.tool()
    async def update_subscription(user_id: int, changes: dict[str, Any], confirm: bool) -> dict[str, Any]:
        """Изменяет подписку через штатную логику /admin. Пришли только меняемые поля и confirm=true."""
        await require_confirm(confirm)
        allowed = {
            "name", "note", "enabled", "expires_at", "title_override", "announce_override",
            "traffic_limit_bytes", "hide_traffic_usage", "include_sas", "device_limit",
            "activation_enabled", "activation_limit",
        }
        if not changes or set(changes) - allowed:
            raise ToolError(f"Изменения должны содержать только поля: {', '.join(sorted(allowed))}")
        users = await raw_subscriptions()
        current = next((item for item in users if int(item.get("id", -1)) == user_id), None)
        if current is None:
            raise ToolError("Подписка не найдена")
        body = {key: current.get(key) for key in (
            "name", "note", "enabled", "expires_at", "title_override", "announce_override",
            "traffic_limit_bytes", "hide_traffic_usage", "include_sas", "device_limit",
        )}
        body.update(changes)
        if "activation_limit" in changes and "activation_enabled" not in changes:
            if not current.get("activation_enabled"):
                raise ToolError("Для нового лимита включи activation_enabled=true и укажи activation_limit")
            body["activation_enabled"] = True
        if changes.get("activation_enabled") is True and "activation_limit" not in changes:
            if current.get("activation_enabled") and current.get("activation_limit"):
                body["activation_limit"] = current["activation_limit"]
            else:
                raise ToolError("При включении Render-активации укажи activation_limit от 1 до 20")
        result = await admin_api("PUT", f"/api/admin/users/{user_id}", body)
        return _public(result)

    @mcp.tool()
    async def bulk_update_subscriptions(user_ids: list[int], changes: dict[str, Any], confirm: bool) -> dict[str, Any]:
        """Пакетно меняет поля подписок и делает один targeted Drive sync. Нужен confirm=true; ID legacy-подписки не включай."""
        await require_confirm(confirm)
        ids = _validate_ids(user_ids)
        normalized = _validate_bulk_changes(changes, panel)
        async with main_database() as pool:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(
                        "SELECT id,name,slug,status,enabled,expires_at,include_sas,hide_traffic_usage,"
                        "traffic_limit_bytes,note,title_override,announce_override,device_limit,drive_file_id "
                        "FROM users WHERE id=ANY($1::bigint[]) FOR UPDATE", ids,
                    )
                    if len(rows) != len(ids):
                        raise ToolError("Один или несколько ID подписок не найдены")
                    activation_ids = set()
                    if "device_limit" in normalized:
                        activation_ids = set(await conn.fetchval(
                            "SELECT COALESCE(array_agg(user_id), ARRAY[]::bigint[]) FROM subscription_activation "
                            "WHERE user_id=ANY($1::bigint[])", ids))
                    legacy_ids = {int(row["id"]) for row in rows
                                  if row["slug"] == "legacy" or row["drive_file_id"] == panel.LEGACY_DRIVE_FILE_ID}
                    if "device_limit" in normalized and (legacy_ids or activation_ids):
                        raise ToolError("Массовую смену Happ-лимита нельзя применять к legacy- или Render-активациям")

                    changed_ids: list[int] = []
                    rebuild_ids: list[int] = []
                    skipped = []
                    for row in rows:
                        user_id = int(row["id"])
                        if row["status"] != "active":
                            skipped.append(user_id)
                            continue
                        values: dict[str, Any] = {}
                        if set(normalized) & {"name_prefix", "name_suffix", "find_text", "replace_text"}:
                            new_name = _renamed_name(str(row["name"]), normalized)
                            if new_name != row["name"]:
                                values["name"] = new_name
                        for field in ("enabled", "include_sas", "hide_traffic_usage", "note",
                                      "title_override", "announce_override", "traffic_limit_bytes", "expires_at"):
                            if field in normalized:
                                values[field] = normalized[field]
                        limit_changed = ("device_limit" in normalized
                                         and normalized["device_limit"] != row["device_limit"])
                        if limit_changed:
                            values.update({
                                "device_limit": normalized["device_limit"],
                                "happ_install_code": None,
                                "limited_link": None,
                                "device_limit_status": "waiting" if normalized["device_limit"] is not None else "none",
                                "device_limit_error": None,
                            })
                        if not values:
                            continue
                        columns = list(values)
                        args = [user_id, *(values[column] for column in columns)]
                        setters = [f"{column}=${index}" for index, column in enumerate(columns, 2)]
                        setters.append("updated_at=now()")
                        await conn.execute(
                            f"UPDATE users SET {', '.join(setters)} WHERE id=$1", *args,
                        )
                        changed_ids.append(user_id)
                        if limit_changed and normalized["device_limit"] is not None and row["enabled"]:
                            rebuild_ids.append(user_id)
        delivery = await panel.state.push_drive_sync(changed_ids) if changed_ids else None
        for user_id in rebuild_ids:
            panel._schedule_limited_link(user_id)
        return {"updated": changed_ids, "skipped_inactive": skipped,
                "device_limit_rebuild_scheduled": rebuild_ids, "drive_sync": delivery}

    @mcp.tool()
    async def deactivate_subscription(user_id: int, confirm: bool) -> dict[str, Any]:
        """Marks a subscription for Drive revocation and removes it after Apps Script confirms. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("DELETE", f"/api/admin/users/{user_id}")

    @mcp.tool()
    async def bulk_deactivate_subscriptions(user_ids: list[int], confirm: bool) -> dict[str, Any]:
        """Stages several subscriptions for revocation and sends one Drive sync. Requires confirm=true; legacy is protected."""
        await require_confirm(confirm)
        ids = _validate_ids(user_ids)
        async with main_database() as pool:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(
                        "SELECT id,slug,drive_file_id FROM users WHERE id=ANY($1::bigint[]) FOR UPDATE", ids,
                    )
                    if len(rows) != len(ids):
                        raise ToolError("Один или несколько ID подписок не найдены")
                    if any(row["slug"] == "legacy" or row["drive_file_id"] == panel.LEGACY_DRIVE_FILE_ID for row in rows):
                        raise ToolError("Основная legacy-подписка защищена от массового отключения")
                    await conn.execute(
                        "UPDATE users SET status='pending_delete',enabled=FALSE,updated_at=now() "
                        "WHERE id=ANY($1::bigint[])", ids,
                    )
        delivery = await panel.state.push_drive_sync(ids)
        return {"pending_delete": ids, "drive_sync": delivery}

    @mcp.tool()
    async def get_subscription_preview(user_id: int) -> dict[str, Any]:
        """Builds the exact client profile preview. It contains live server credentials; use only when needed."""
        content = await admin_api("GET", f"/api/admin/users/{user_id}/preview")
        return {"user_id": user_id, "sensitive_profile": True, "content": str(content)[:200_000]}

    @mcp.tool()
    async def get_subscription_link(user_id: int, kind: Literal["drive", "limited", "activation"]) -> dict[str, Any]:
        """Returns a private bearer link only on explicit request. Kinds: drive, limited, activation."""
        users = await raw_subscriptions()
        user = next((item for item in users if int(item.get("id", -1)) == user_id), None)
        if user is None:
            raise ToolError("Подписка не найдена")
        if kind == "drive":
            value = user.get("drive_link")
            pending = not value
        elif kind == "limited":
            value = user.get("limited_link")
            pending = not value
        else:
            value = user.get("activation_url") if user.get("activation_enabled") else None
            pending = not value
        return {"user_id": user_id, "kind": kind, "link": value, "pending": pending,
                "sensitive_bearer_link": bool(value)}

    @mcp.tool()
    async def list_activations() -> dict[str, Any]:
        """Shows activation limits, registered browser slots and sync readiness; hides activation bearer URLs."""
        result = await admin_api("GET", "/api/admin/activations")
        return _public(result)

    @mcp.tool()
    async def set_activation_limit(user_id: int, limit: int, confirm: bool) -> dict[str, Any]:
        """Sets a Render activation slot limit from 1 to 20; lowering it removes newest slots. Requires confirm=true."""
        await require_confirm(confirm)
        if not 1 <= limit <= 20:
            raise ToolError("Лимит Render-активации должен быть от 1 до 20")
        return await admin_api("PUT", f"/api/admin/users/{user_id}/activation/limit", {"limit": limit})

    @mcp.tool()
    async def bulk_set_activation_limits(user_ids: list[int], limit: int, confirm: bool) -> dict[str, Any]:
        """Applies a 1–20 Render slot limit to several activation-enabled subscriptions in one database transaction."""
        await require_confirm(confirm)
        ids = _validate_ids(user_ids)
        if not 1 <= limit <= 20:
            raise ToolError("Лимит Render-активации должен быть от 1 до 20")
        async with main_database() as pool:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    existing = await conn.fetch(
                        "SELECT user_id FROM subscription_activation WHERE user_id=ANY($1::bigint[]) FOR UPDATE", ids,
                    )
                    found = {int(row["user_id"]) for row in existing}
                    if found != set(ids):
                        raise ToolError("Все выбранные подписки должны иметь включённую Render-активацию")
                    for user_id in ids:
                        await conn.execute("""DELETE FROM activation_devices WHERE user_id=$1 AND device_hash IN (
                            SELECT device_hash FROM activation_devices WHERE user_id=$1
                            ORDER BY registered_at,device_hash OFFSET $2)""", user_id, limit)
                        await conn.execute(
                            "UPDATE subscription_activation SET device_limit=$2,updated_at=now() WHERE user_id=$1",
                            user_id, limit,
                        )
        return {"updated": ids, "limit": limit}

    @mcp.tool()
    async def reset_activation(user_id: int, confirm: bool) -> dict[str, Any]:
        """Clears registered browser slots while retaining the same activation URL. Requires confirm=true."""
        await require_confirm(confirm)
        return _public(await admin_api("POST", f"/api/admin/users/{user_id}/activation/reset", {}))

    @mcp.tool()
    async def retry_activation_sync(user_id: int, confirm: bool) -> dict[str, Any]:
        """Retries immediate Apps Script delivery for one activated subscription. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("POST", f"/api/admin/users/{user_id}/activation/retry", {})

    @mcp.tool()
    async def retry_happ_device_limit(user_id: int, action: Literal["retry", "rebuild"], confirm: bool) -> dict[str, Any]:
        """Creates or rebuilds one Happ limited link using configured Happ provider credentials. Requires confirm=true."""
        await require_confirm(confirm)
        result = await admin_api("POST", f"/api/admin/users/{user_id}/device-limit", {"action": action})
        return _public(result)

    @mcp.tool()
    async def bulk_retry_happ_device_limits(
        user_ids: list[int], action: Literal["retry", "rebuild"], confirm: bool,
    ) -> dict[str, Any]:
        """Retries/rebuilds limited Happ links for selected users, at most three provider calls concurrently."""
        await require_confirm(confirm)
        ids = _validate_ids(user_ids, max_count=30)
        semaphore = asyncio.Semaphore(3)

        async def run_one(user_id: int) -> dict[str, Any]:
            async with semaphore:
                try:
                    result = await admin_api("POST", f"/api/admin/users/{user_id}/device-limit", {"action": action})
                    return {"user_id": user_id, "ok": True, "result": _public(result)}
                except Exception as exc:
                    return {"user_id": user_id, "ok": False, "error": str(exc)[:500]}

        results = await asyncio.gather(*(run_one(user_id) for user_id in ids))
        return {"results": results, "succeeded": sum(bool(row["ok"]) for row in results),
                "failed": sum(not row["ok"] for row in results)}

    @mcp.tool()
    async def clear_happ_device_limit(user_id: int, confirm: bool) -> dict[str, Any]:
        """Clears local Happ limit state for a subscription. Already-issued Happ counters are not revoked. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("DELETE", f"/api/admin/users/{user_id}/device-limit")

    @mcp.tool()
    async def list_servers() -> dict[str, Any]:
        """Lists the current provider catalog; Secure X and SAS order follows the panel catalog."""
        return await admin_api("GET", "/api/admin/servers")

    @mcp.tool()
    async def update_server(
        server_id: str, custom_name: str | None, hidden: bool, sort_order: int, confirm: bool,
    ) -> dict[str, Any]:
        """Changes one global server name, visibility or order and syncs profiles. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("PUT", f"/api/admin/servers/{_path_part(server_id)}", {
            "custom_name": custom_name, "hidden": hidden, "sort_order": sort_order,
        })

    @mcp.tool()
    async def bulk_update_servers(servers: list[dict[str, Any]], confirm: bool) -> dict[str, Any]:
        """Applies staged global server name, visibility and order edits in one transaction and one Drive sync."""
        await require_confirm(confirm)
        if not servers or len(servers) > 500:
            raise ToolError("Передай от 1 до 500 серверов")
        return await admin_api("POST", "/api/admin/servers/batch", {"servers": servers})

    @mcp.tool()
    async def bulk_rename_servers(
        server_ids: list[str], prefix: str = "", suffix: str = "", find_text: str = "",
        replace_text: str = "", confirm: bool = False,
    ) -> dict[str, Any]:
        """Renames selected global servers with prefix/suffix or find-and-replace; persists all names in one sync."""
        await require_confirm(confirm)
        ids = _validate_server_ids(server_ids)
        if not prefix and not suffix and not find_text:
            raise ToolError("Укажи prefix, suffix или find_text для переименования")
        catalog = await admin_api("GET", "/api/admin/servers")
        selected = {str(item["id"]): item for item in catalog.get("servers", []) if str(item.get("id")) in set(ids)}
        if len(selected) != len(ids):
            raise ToolError("Один или несколько серверов отсутствуют в каталоге")
        items = []
        for server_id in ids:
            server = selected[server_id]
            base_name = str(server.get("custom_name") or server.get("original_name") or "")
            if find_text:
                base_name = base_name.replace(find_text, replace_text)
            name = (prefix + base_name + suffix)[:200]
            items.append({"server_id": server_id, "custom_name": name,
                          "hidden": bool(server.get("hidden")), "sort_order": int(server.get("sort_order", 0))})
        return await admin_api("POST", "/api/admin/servers/batch", {"servers": items})

    @mcp.tool()
    async def bulk_set_server_visibility(server_ids: list[str], hidden: bool, confirm: bool) -> dict[str, Any]:
        """Hides or shows selected global servers for every subscriber in one sync. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("POST", "/api/admin/servers/bulk", {
            "server_ids": _validate_server_ids(server_ids), "hidden": hidden,
        })

    @mcp.tool()
    async def refresh_server_sources(confirm: bool) -> dict[str, Any]:
        """Fetches both provider sources, rebuilds ordering and sends one Drive sync. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("POST", "/api/admin/servers/refresh", {})

    @mcp.tool()
    async def clear_server_cache(confirm: bool) -> dict[str, Any]:
        """Rebuilds the server catalog/order from both providers while preserving retained names/visibility. Requires confirm=true."""
        await require_confirm(confirm)
        return await admin_api("POST", "/api/admin/cache/refresh", {})

    @mcp.tool()
    async def list_subscription_servers(user_id: int) -> dict[str, Any]:
        """Lists the global catalog with this subscription's custom server names and visibility overrides."""
        return await admin_api("GET", f"/api/admin/users/{user_id}/servers")

    @mcp.tool()
    async def bulk_update_subscription_servers(
        user_id: int, servers: list[dict[str, Any]], confirm: bool,
    ) -> dict[str, Any]:
        """Stages per-subscription server names and visibility and saves them with one Drive sync."""
        await require_confirm(confirm)
        if not servers or len(servers) > 500:
            raise ToolError("Передай от 1 до 500 серверов")
        return await admin_api("POST", f"/api/admin/users/{user_id}/servers/batch", {"servers": servers})

    @mcp.tool()
    async def get_settings() -> dict[str, Any]:
        """Reads primary panel configuration and safe integration readiness flags."""
        return await admin_api("GET", "/api/admin/settings")

    @mcp.tool()
    async def update_settings(values: dict[str, Any], confirm: bool) -> dict[str, Any]:
        """Updates allowed panel settings using the existing validation and profile delivery logic. Requires confirm=true."""
        await require_confirm(confirm)
        if not values:
            raise ToolError("Передай хотя бы одну настройку")
        return _public(await admin_api("PUT", "/api/admin/settings", {"values": values}))

    @mcp.tool()
    async def sync_drive_now(confirm: bool) -> dict[str, Any]:
        """Runs immediate synchronization for all profiles through the configured Apps Script integration."""
        await require_confirm(confirm)
        return await admin_api("POST", "/api/admin/drive/sync", {})

    service_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip().lower() or "zeva-online.onrender.com"
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[service_host, "localhost:*", "127.0.0.1:*", "[::1]:*"],
        allowed_origins=[f"https://{service_host}", "http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"],
    )
    base_path = f"/mcp/agent/{secret}"
    child_app = mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        transport_security=security,
    )
    app.mount(base_path, child_app, name="mcp-agent")
    mount_index = next(index for index, route in enumerate(app.router.routes)
                       if isinstance(route, Mount) and route.path == base_path)
    app.router.routes.insert(mount_index, _ExactPathASGIRoute(base_path, child_app))

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(application):
        async with previous_lifespan(application):
            async with mcp.session_manager.run():
                yield

    app.router.lifespan_context = combined_lifespan
    log.info("MCP admin endpoint mounted (secret hidden)")
    return True


def _validate_ids(user_ids: list[int], *, max_count: int = _MAX_MCP_IDS) -> list[int]:
    if not isinstance(user_ids, list) or not user_ids or len(user_ids) > max_count:
        raise ToolError(f"Передай от 1 до {max_count} ID")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in user_ids):
        raise ToolError("ID должны быть положительными целыми числами")
    if len(set(user_ids)) != len(user_ids):
        raise ToolError("В списке есть повторяющиеся ID")
    return user_ids


def _validate_server_ids(server_ids: list[str]) -> list[str]:
    if not isinstance(server_ids, list) or not server_ids or len(server_ids) > 500:
        raise ToolError("Передай от 1 до 500 ID серверов")
    if any(not isinstance(value, str) or not value or len(value) > 200 for value in server_ids):
        raise ToolError("Каждый ID сервера должен быть строкой длиной 1–200 символов")
    if len(set(server_ids)) != len(server_ids):
        raise ToolError("В списке есть повторяющиеся серверы")
    return server_ids


def _path_part(value: str) -> str:
    from urllib.parse import quote

    if not value or len(value) > 200:
        raise ToolError("Некорректный ID сервера")
    return quote(value, safe="")


def _validate_bulk_changes(changes: dict[str, Any], panel) -> dict[str, Any]:
    allowed = {
        "enabled", "include_sas", "hide_traffic_usage", "expires_at", "note", "title_override",
        "announce_override", "traffic_limit_bytes", "device_limit", "name_prefix", "name_suffix",
        "find_text", "replace_text",
    }
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise ToolError(f"Массово разрешены только поля: {', '.join(sorted(allowed))}")
    result = dict(changes)
    for field in ("enabled", "include_sas", "hide_traffic_usage"):
        if field in result and not isinstance(result[field], bool):
            raise ToolError(f"{field} должен быть логическим значением")
    for field, limit in (("note", 2000), ("title_override", 200), ("announce_override", 4000),
                         ("name_prefix", 120), ("name_suffix", 120), ("find_text", 200),
                         ("replace_text", 200)):
        if field in result and result[field] is not None and (not isinstance(result[field], str)
                or len(result[field]) > limit):
            raise ToolError(f"Некорректное значение {field}")
    if "traffic_limit_bytes" in result and result["traffic_limit_bytes"] is not None:
        value = result["traffic_limit_bytes"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1_000_000 <= value <= 1_000_000_000_000:
            raise ToolError("traffic_limit_bytes должен быть от 1 МБ до 1 ТБ или null")
    if "device_limit" in result and result["device_limit"] is not None:
        value = result["device_limit"]
        if isinstance(value, bool) or not isinstance(value, int) or not panel.happ_limit.MIN_LIMIT <= value <= panel.happ_limit.MAX_LIMIT:
            raise ToolError(f"device_limit должен быть от {panel.happ_limit.MIN_LIMIT} до {panel.happ_limit.MAX_LIMIT} или null")
    if "expires_at" in result and result["expires_at"] is not None:
        value = result["expires_at"]
        if not isinstance(value, str):
            raise ToolError("expires_at должен быть ISO 8601 строкой или null")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolError("expires_at должен быть корректной ISO 8601 датой") from exc
        result["expires_at"] = panel._expiry_utc(parsed)
    if "replace_text" in result and "find_text" not in result:
        raise ToolError("replace_text можно передать только вместе с find_text")
    if "find_text" in result and result["find_text"] == "":
        raise ToolError("find_text не может быть пустым")
    if any(field in result for field in ("name_prefix", "name_suffix", "find_text", "replace_text")):
        result.setdefault("name_prefix", "")
        result.setdefault("name_suffix", "")
        result.setdefault("find_text", "")
        result.setdefault("replace_text", "")
        for field in ("name_prefix", "name_suffix", "find_text", "replace_text"):
            if result[field] is None:
                result[field] = ""
    if result.get("note", None) is None and "note" in result:
        result["note"] = ""
    return result


def _renamed_name(name: str, changes: dict[str, Any]) -> str:
    if changes.get("find_text"):
        name = name.replace(changes.get("find_text", ""), changes.get("replace_text", ""))
    name = changes.get("name_prefix", "") + name + changes.get("name_suffix", "")
    name = name.strip()
    if not name or len(name) > 120:
        raise ToolError("Массовое переименование создаёт пустое имя или имя длиннее 120 символов")
    return name


