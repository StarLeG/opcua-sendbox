"""Счётчики сервера: HTTP-запросы, ошибки, скорости и uptime (TASK-205)."""
from __future__ import annotations

import time

import pytest

from app.api.stats import RATE_WINDOW_S, RequestStats, counters_payload
from tests.fakes import Env


def test_hit_counts_totals_and_errors() -> None:
    stats = RequestStats()
    for code in (200, 201, 204, 404, 500):
        stats.hit(code)
    assert stats.total == 5
    assert stats.errors == 2  # ошибкой считается статус 400 и выше


def test_rates_average_over_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    stats = RequestStats()
    for _ in range(5):
        stats.hit(200)
    stats.hit(503)
    rps, eps = stats.rates()
    assert (rps, eps) == (6.0, 1.0)  # на старте span = 1 с
    now[0] = 100.0 + RATE_WINDOW_S + 0.1  # корзины выпали из окна
    assert stats.rates() == (0.0, 0.0)


async def test_counters_payload_counts_clients_and_uptime() -> None:
    env = Env()
    manager = env.manager
    manager._on_shared_message({"type": "client_count", "clients": 1})
    session = await manager.create("k", "ip")
    session.handle.on_message({"type": "client_count", "clients": 2})
    stats = RequestStats()
    stats.hit(200)
    stats.hit(404)

    payload = counters_payload(manager, stats)
    assert set(payload.model_dump()) == {
        "clients",
        "requests_per_s",
        "errors_per_s",
        "requests_total",
        "errors_total",
        "uptime_s",
    }
    assert payload.clients == 3  # общий сервер + личная сессия
    assert payload.requests_total == 2
    assert payload.errors_total == 1
    assert payload.uptime_s >= 0
