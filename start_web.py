from __future__ import annotations
import os
import secrets
if os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"):
    raise SystemExit("start_web.py is a local preview helper and must not run on Render.")
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _name in tuple(os.environ):
    if _name.startswith("UPSTREAM_") or _name in {
        "DATABASE_URL",
        "SUBSCRIPTION_URL",
        "SAS_SUBSCRIPTION_URL",
        "SYNC_KEY",
        "DRIVE_SYNC_URL",
        "DRIVE_API_KEY",
        "ADMIN_PASSWORD_HASH",
    }:
        os.environ.pop(_name, None)
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"
os.environ["SESSION_SECRET"] = secrets.token_urlsafe(48)
import uvicorn
from app import app
def main() -> None:
    print("Локальная панель: http://localhost:8000/admin")
    print("Логин: admin")
    print("Пароль: admin")
    print("Это только визуальный preview: без БД и подключений к внешним сервисам.")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
if __name__ == "__main__":
    main()
