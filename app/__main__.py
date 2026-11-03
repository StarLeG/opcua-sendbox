"""Запуск сервиса: `uv run python -m app`.

Web-слушатель — `WEB_HOST`/`WEB_PORT`, OPC UA — переменные `OPCUA_*` (.env.example).
Воркеры OPC UA запускаются дочерними процессами (spawn), поэтому весь запуск —
под `if __name__ == "__main__"`.
"""
from __future__ import annotations

import logging
import os

import uvicorn

from app.api.app import create_app
from app.config import Config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    uvicorn.run(
        create_app(config),
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8766")),
        # За nginx реальный IP клиента приходит в X-Forwarded-For; доверяем только
        # перечисленным адресам, иначе лимиты по IP считали бы всех одним клиентом.
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
