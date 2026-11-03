"""Счётчики сервера для UI: HTTP-запросы, ошибки, скорости и uptime (TASK-205).

Запросы считаются http-middleware'ем приложения (`app.api.app`), скорости —
скользящим окном по секундным корзинам. Кадр `counters` уходит в `/ws/shared`
и дублируется в `GET /api/stats`. Ошибкой считается ответ со статусом 400 и выше.
"""
from __future__ import annotations

import time
from collections import deque

from app.api.models import StatsOut
from app.opcua.session_manager import SessionManager

#: Окно усреднения скоростей «Запросы/с» и «Ошибки/с», с.
RATE_WINDOW_S = 5.0
#: Сколько секундных корзин хранить (достаточно для окна).
_BUCKET_COUNT = 60


class RequestStats:
    """Счётчики HTTP-запросов web-процесса: всего, ошибки и скорости за окно."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self.total = 0
        self.errors = 0
        #: Секундные корзины [секунда, запросы, ошибки]; ведётся в event loop.
        self._buckets: deque[list[int]] = deque(maxlen=_BUCKET_COUNT)

    def hit(self, status: int) -> None:
        """Учесть ответ сервера; ошибкой считается статус 400 и выше."""
        self.total += 1
        bad = status >= 400
        if bad:
            self.errors += 1
        sec = int(time.monotonic())
        if not self._buckets or self._buckets[-1][0] != sec:
            self._buckets.append([sec, 0, 0])
        self._buckets[-1][1] += 1
        if bad:
            self._buckets[-1][2] += 1

    def rates(self) -> tuple[float, float]:
        """Средние запросы и ошибки в секунду за окно RATE_WINDOW_S."""
        now = time.monotonic()
        span = min(RATE_WINDOW_S, max(now - self._started, 1.0))
        hits = 0
        errs = 0
        for sec, count, bad in self._buckets:
            if sec >= now - RATE_WINDOW_S:
                hits += count
                errs += bad
        return hits / span, errs / span


def counters_payload(manager: SessionManager, stats: RequestStats) -> StatsOut:
    """Снимок счётчиков для кадра WS и GET /api/stats.

    «Активные соединения» — OPC UA клиенты общего сервера и всех личных сессий;
    uptime — время работы сервиса (менеджер стартует вместе с приложением).
    """
    rps, eps = stats.rates()
    return StatsOut(
        clients=manager.shared.clients + sum(s.clients for s in manager.sessions()),
        requests_per_s=round(rps, 2),
        errors_per_s=round(eps, 2),
        requests_total=stats.total,
        errors_total=stats.errors,
        uptime_s=round(manager.uptime(), 1),
    )
