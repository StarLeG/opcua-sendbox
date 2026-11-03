"""Лимит мутирующих действий из UI/REST на IP (как ADR-012 Modbus SendBox).

Одно скользящее окно на IP для всех изменений: создание и удаление сессий,
теги, алармы, вызовы алармов. Чтение не ограничивается. Хранение — в памяти.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from app.opcua.keys import SlidingWindow


class IpRateLimiter:
    """Не более `limit` действий за `window_s` на каждый IP."""

    def __init__(
        self, limit: int, window_s: float = 60.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if limit <= 0 or window_s <= 0:
            raise ValueError("лимит и окно должны быть > 0")
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._windows: dict[str, SlidingWindow] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def allow(self, ip: str) -> bool:
        with self._lock:
            window = self._windows.get(ip)
            if window is None:
                window = SlidingWindow(self._limit, self._window_s, self._clock)
                self._windows[ip] = window
            self._calls += 1
            if self._calls % 1000 == 0:  # периодически выбрасываем пустые окна
                for stale in [k for k, w in self._windows.items() if w.empty() and k != ip]:
                    del self._windows[stale]
            return window.allow()

    def __len__(self) -> int:
        with self._lock:
            return len(self._windows)
