"""KeyRegistry: выдача, проверка, лимит на IP, забывание неактивных."""
from __future__ import annotations

import pytest

from app.opcua.keys import KeyRateLimitError, KeyRegistry, SlidingWindow


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_issue_and_check() -> None:
    clock = Clock()
    keys = KeyRegistry(per_ip_per_hour=5, clock=clock)
    info = keys.issue("1.2.3.4")
    assert len(info.key) >= 32
    clock.now = 10
    assert keys.check(info.key) is info
    assert info.last_seen == 10
    assert keys.check("нет-такого") is None
    assert keys.check(None) is None
    assert keys.check("") is None


def test_keys_are_unique() -> None:
    keys = KeyRegistry(per_ip_per_hour=1000)
    assert len({keys.issue("ip").key for _ in range(200)}) == 200


def test_issue_limit_per_ip_with_sliding_window() -> None:
    clock = Clock()
    keys = KeyRegistry(per_ip_per_hour=2, clock=clock)
    keys.issue("a")
    keys.issue("a")
    with pytest.raises(KeyRateLimitError, match="не больше 2"):
        keys.issue("a")
    keys.issue("b")  # другой IP — свой лимит
    clock.now = 3600
    keys.issue("a")  # окно сдвинулось


def test_forget_inactive_keeps_keys_in_use() -> None:
    clock = Clock()
    keys = KeyRegistry(per_ip_per_hour=10, clock=clock)
    used, idle, fresh = keys.issue("ip"), keys.issue("ip"), None
    clock.now = 100
    fresh = keys.issue("ip")
    assert keys.forget_inactive(50, in_use={used.key}) == 1
    assert keys.check(used.key) and keys.check(fresh.key)
    assert keys.check(idle.key) is None
    assert len(keys) == 2


def test_sliding_window() -> None:
    clock = Clock()
    window = SlidingWindow(2, 10, clock)
    assert window.allow() and window.allow() and not window.allow()
    clock.now = 10
    assert window.allow()
    clock.now = 25
    assert window.empty()
