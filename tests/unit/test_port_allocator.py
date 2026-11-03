"""PortAllocator: уникальность, исчерпание, возврат, занятые порты, конкурентность."""
from __future__ import annotations

import socket
import threading
from collections import Counter

import pytest

from app.opcua.port_allocator import PortAllocator, PortsExhaustedError, probe_bind


def always_free(port: int) -> bool:
    _ = port
    return True


def test_acquire_unique_until_exhausted() -> None:
    allocator = PortAllocator(100, 104, probe=always_free)
    ports = [allocator.acquire() for _ in range(5)]
    assert sorted(ports) == [100, 101, 102, 103, 104]
    assert allocator.in_use == frozenset(ports)
    with pytest.raises(PortsExhaustedError, match="100..104"):
        allocator.acquire()


def test_release_makes_port_available_again() -> None:
    allocator = PortAllocator(100, 101, probe=always_free)
    first, second = allocator.acquire(), allocator.acquire()
    allocator.release(first)
    assert allocator.acquire() == first
    assert allocator.in_use == {first, second}


def test_release_is_idempotent_and_checks_range() -> None:
    allocator = PortAllocator(100, 101, probe=always_free)
    port = allocator.acquire()
    allocator.release(port)
    allocator.release(port)  # повтор — без ошибки
    assert allocator.in_use == frozenset()
    with pytest.raises(ValueError, match="вне диапазона"):
        allocator.release(99)


def test_round_robin_does_not_reuse_just_released_port() -> None:
    """Порт закрытой сессии не уходит сразу следующей (переподключение в чужую песочницу)."""
    allocator = PortAllocator(100, 104, probe=always_free)
    port = allocator.acquire()
    allocator.release(port)
    assert allocator.acquire() != port
    # после полного круга порт снова доступен
    sequence = [allocator.acquire() for _ in range(4)]
    assert port in sequence


def test_busy_port_skipped_and_retried_later() -> None:
    busy = {101}
    allocator = PortAllocator(100, 102, probe=lambda p: p not in busy)
    assert [allocator.acquire(), allocator.acquire()] == [100, 102]
    with pytest.raises(PortsExhaustedError):
        allocator.acquire()
    busy.clear()  # чужой процесс освободил порт
    assert allocator.acquire() == 101


def test_invalid_range_rejected() -> None:
    with pytest.raises(ValueError):
        PortAllocator(10, 5)
    with pytest.raises(ValueError):
        PortAllocator(0, 5)
    with pytest.raises(ValueError):
        PortAllocator(65000, 70000)


def test_concurrent_acquire_from_threads_never_duplicates() -> None:
    """ТЗ: гонка за порты — 50 потоков одновременно, ни одного дубля."""
    allocator = PortAllocator(1000, 1039, probe=always_free)
    barrier = threading.Barrier(50)
    results: list[int] = []
    errors: list[Exception] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            port = allocator.acquire()
        except PortsExhaustedError as exc:
            with results_lock:
                errors.append(exc)
            return
        with results_lock:
            results.append(port)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 40  # ёмкость диапазона
    assert len(errors) == 10
    assert max(Counter(results).values()) == 1


def test_concurrent_acquire_release_keeps_consistency() -> None:
    allocator = PortAllocator(2000, 2009, probe=always_free)
    duplicates: list[int] = []
    held: set[int] = set()
    held_lock = threading.Lock()

    def worker() -> None:
        for _ in range(200):
            try:
                port = allocator.acquire()
            except PortsExhaustedError:
                continue
            with held_lock:
                if port in held:
                    duplicates.append(port)
                held.add(port)
            with held_lock:
                held.discard(port)
            allocator.release(port)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert duplicates == []
    assert allocator.in_use == frozenset()


def test_probe_bind_detects_occupied_port() -> None:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert probe_bind(port) is False
    assert probe_bind(port) is True


def test_probe_bind_detects_ipv6_only_listener() -> None:
    """Слушатель только на IPv6 тоже делает порт занятым (воркер слушает оба семейства)."""
    if not socket.has_ipv6:
        pytest.skip("IPv6 недоступен")
    with socket.socket(socket.AF_INET6) as sock:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            sock.bind(("::", 0))
        except OSError:
            pytest.skip("bind на :: недоступен")
        sock.listen()
        port = sock.getsockname()[1]
        assert probe_bind(port) is False
