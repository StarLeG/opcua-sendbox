"""Настройка логирования: текст или JSON (перенесено из Modbus SendBox).

`LOG_FORMAT=json` - одна строка JSON на событие: их собирают journald,
docker logs или любой shipper (Loki, ELK). Sentry и другие агенты в проект
не добавлялись: правила требуют минимума зависимостей, а ошибки уже видны
в логах и счётчиках `/metrics`. `text` - человекочитаемый формат для
разработки.
"""
from __future__ import annotations

import json
import logging
import sys
import time

#: Имена полей JSON-события в порядке вывода.
JSON_FORMAT = "json"
TEXT_FORMAT = "text"
_FORMATS = (TEXT_FORMAT, JSON_FORMAT)

_TEXT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись: ts, level, logger, message, exc."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(fmt: str = TEXT_FORMAT, level: str = "info") -> None:
    """Настроить root-логгер: один обработчик в stdout, без дублей.

    Вызывать один раз при старте процесса; повторный вызов перенастраивает
    существующий обработчик, а не плодит новые.
    """
    if fmt not in _FORMATS:
        raise ValueError(f"LOG_FORMAT: ожидается {_FORMATS}, получено {fmt!r}")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == JSON_FORMAT else logging.Formatter(_TEXT))
    root = logging.getLogger()
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
