"""Базовая проверка: пакет импортируется, версия читается из метаданных."""
from __future__ import annotations

from importlib.metadata import version


def test_package_version_is_semver() -> None:
    parts = version("opcua-sendbox").split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
