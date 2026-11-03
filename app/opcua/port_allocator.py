"""Выдача портов личным сессиям (ADR-001 п. 3).

Потокобезопасен (`threading.RLock`): вызывается из event loop под `asyncio.Lock`
менеджера сессий и из reader-потоков воркеров. Порт выдаётся только после
успешной пробы `bind`: порт, занятый чужим процессом, пропускается.

Выдача идёт по кругу, начиная после последнего выданного порта. Освобождённый
порт не уходит следующей сессии сразу: клиент закрытой сессии, переподключаясь
по старому адресу, не должен попасть в чужую песочницу.

Проба не исключает гонку: между ней и bind в воркере порт может занять кто-то
ещё. Тогда воркер не стартует, и менеджер сессий освобождает порт и сообщает об
ошибке (инвариант 3 ISSUES.md).
"""
from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable


class PortsExhaustedError(RuntimeError):
    """Свободных портов в диапазоне нет (или все свободные заняты чужими процессами)."""


def probe_bind(port: int, host: str | None = None) -> bool:
    """True, если порт можно занять: bind на IPv4 и, если есть, на IPv6.

    host=None — все интерфейсы, как у воркера (ADR-002 п. 1).
    """
    families = [(socket.AF_INET, host or "0.0.0.0")]
    if socket.has_ipv6 and (host is None or ":" in host):
        families.append((socket.AF_INET6, host or "::"))
    for family, address in families:
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            if family == socket.AF_INET6:
                # IPv6 выключен в ядре целиком (ipv6.disable=1): сокет не создать,
                # и asyncio тоже будет слушать только IPv4 — проверять нечего.
                continue
            return False
        with sock:
            if os.name != "nt":
                # Как asyncio.create_server на POSIX: иначе соединения в TIME_WAIT
                # после отключения клиентов делали бы свободный порт «занятым» ~60 с.
                # На Windows SO_REUSEADDR означает перехват чужого порта — не ставим.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind((address, port))
            except OSError:
                return False
    return True


class PortAllocator:
    """Пул портов [first, last] с пробой bind и выдачей по кругу."""

    def __init__(
        self,
        first: int,
        last: int,
        *,
        probe: Callable[[int], bool] = probe_bind,
    ) -> None:
        if not 1 <= first <= last <= 65535:
            raise ValueError(f"неверный диапазон портов: {first}..{last}")
        self._first = first
        self._last = last
        self._probe = probe
        self._in_use: set[int] = set()
        self._next = first
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        """Размер диапазона."""
        return self._last - self._first + 1

    @property
    def in_use(self) -> frozenset[int]:
        """Выданные и ещё не возвращённые порты (снимок)."""
        with self._lock:
            return frozenset(self._in_use)

    def acquire(self) -> int:
        """Выдать свободный порт; PortsExhaustedError, если таких нет."""
        with self._lock:
            for offset in range(self.capacity):
                port = self._first + (self._next - self._first + offset) % self.capacity
                if port in self._in_use or not self._probe(port):
                    continue
                self._in_use.add(port)
                self._next = port + 1 if port < self._last else self._first
                return port
            raise PortsExhaustedError(
                f"нет свободных портов в диапазоне {self._first}..{self._last} "
                f"(выдано {len(self._in_use)} из {self.capacity})"
            )

    def release(self, port: int) -> None:
        """Вернуть порт. Повторный возврат ничего не делает; чужой порт — ValueError."""
        if not self._first <= port <= self._last:
            raise ValueError(f"порт {port} вне диапазона {self._first}..{self._last}")
        with self._lock:
            self._in_use.discard(port)
