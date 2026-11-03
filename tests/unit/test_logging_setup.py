"""Структурированные логи (перенесено из Modbus SendBox) - JSON-форматтер и настройка root-логгера."""
from __future__ import annotations

import json
import logging

import pytest

from app.logging_setup import JsonFormatter, configure_logging


def test_json_formatter_outputs_fields() -> None:
    record = logging.getLogger("test.json").makeRecord(
        "test.json", logging.INFO, __file__, 1, "привет %s", ("мир",), None
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.json"
    assert payload["message"] == "привет мир"
    assert payload["ts"].endswith("Z")


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("упал")
    except ValueError:
        record = logging.getLogger("test.exc").makeRecord(
            "test.exc", logging.ERROR, __file__, 1, "сбой", (), __import__("sys").exc_info()
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: упал" in payload["exc"]


def test_configure_logging_replaces_handlers() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging("json", "debug")
        configure_logging("text", "info")
        # Повторная настройка не плодит обработчики: остаётся ровно один.
        assert len(root.handlers) == 1
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def test_configure_logging_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="LOG_FORMAT"):
        configure_logging("xml")
