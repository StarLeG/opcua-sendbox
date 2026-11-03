"""Запуск сервиса: `uv run python -m app`.

Web-слушатель — `WEB_HOST`/`WEB_PORT`, OPC UA — переменные `OPCUA_*` (.env.example).
Воркеры OPC UA запускаются дочерними процессами (spawn), поэтому весь запуск —
под `if __name__ == "__main__"`.
"""
from __future__ import annotations

import os

import uvicorn

from app.api.app import create_app
from app.config import Config
from app.logging_setup import configure_logging


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "info")
    # LOG_FORMAT=json — одна строка JSON на событие (journald, docker logs, Loki).
    configure_logging(os.environ.get("LOG_FORMAT", "text"), log_level)
    config = Config.from_env()
    uvicorn.run(
        create_app(config),
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8766")),
        # За nginx реальный IP клиента приходит в X-Forwarded-For; доверяем только
        # перечисленным адресам, иначе лимиты по IP считали бы всех одним клиентом.
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level=log_level,
        # uvicorn не настраивает логи сам: общий формат задан configure_logging.
        log_config=None,
    )


if __name__ == "__main__":
    main()
