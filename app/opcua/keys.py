"""API-ключи: автоматическая выдача без регистрации, лимит выдачи на IP.

Ключ выдаётся при первом визите (`POST /api/keys`) и хранится только в памяти:
перезапуск сервиса сбрасывает ключи, UI тогда молча запрашивает новый. Ключ
нужен прежде всего для лимита сессий на пользователя (ADR-001).
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


class KeyRateLimitError(RuntimeError):
    """С этого IP выдано слишком много ключей за окно."""


@dataclass
class KeyInfo:
    key: str
    ip: str
    created: float
    last_seen: float


class SlidingWindow:
    """Не более `limit` событий за `window_s` секунд (скользящее окно)."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._stamps: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= self._window_s:
            self._stamps.popleft()
        if len(self._stamps) >= self._limit:
            return False
        self._stamps.append(now)
        return True

    def empty(self) -> bool:
        now = self._clock()
        return not any(now - stamp < self._window_s for stamp in self._stamps)


class KeyRegistry:
    """Выданные ключи. Потокобезопасен (вызывается из REST-обработчиков)."""

    def __init__(
        self,
        *,
        per_ip_per_hour: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_ip = per_ip_per_hour
        self._clock = clock
        self._keys: dict[str, KeyInfo] = {}
        self._issue_windows: dict[str, SlidingWindow] = {}
        self._lock = threading.Lock()

    def issue(self, ip: str) -> KeyInfo:
        """Выдать новый ключ; KeyRateLimitError при превышении лимита для IP."""
        with self._lock:
            window = self._issue_windows.setdefault(ip, SlidingWindow(self._per_ip, 3600.0, self._clock))
            if not window.allow():
                raise KeyRateLimitError(f"слишком много ключей с адреса {ip}: не больше {self._per_ip} в час")
            now = self._clock()
            info = KeyInfo(key=secrets.token_urlsafe(24), ip=ip, created=now, last_seen=now)
            self._keys[info.key] = info
            return info

    def check(self, key: str | None) -> KeyInfo | None:
        """Ключ существует — отметить использование и вернуть; иначе None."""
        if not key:
            return None
        with self._lock:
            info = self._keys.get(key)
            if info is not None:
                info.last_seen = self._clock()
            return info

    def forget_inactive(self, idle_s: float, in_use: set[str]) -> int:
        """Удалить ключи без сессий, не использованные дольше idle_s; вернуть число удалённых."""
        now = self._clock()
        with self._lock:
            stale = [k for k, info in self._keys.items() if k not in in_use and now - info.last_seen > idle_s]
            for key in stale:
                del self._keys[key]
            for ip in [ip for ip, w in self._issue_windows.items() if w.empty()]:
                del self._issue_windows[ip]
            return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)
