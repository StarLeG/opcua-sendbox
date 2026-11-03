"""AlarmEvaluator: пороги, гистерезис, переходы High↔Low, период, ручной вызов."""
from __future__ import annotations

import math

import pytest

from app.opcua.address_space import AlarmEvaluator, AlarmTransition
from app.opcua.tags import AlarmSpec


def limit(**kwargs: object) -> AlarmEvaluator:
    return AlarmEvaluator(AlarmSpec(name="A", kind="limit", tag="T", **kwargs))


def test_high_limit_with_deadband() -> None:
    ev = limit(high=80, deadband=5)
    assert ev.on_value(50) is None  # норма → норма: события нет
    assert ev.on_value(80) is None  # ровно порог — ещё норма
    assert ev.on_value(80.1) == AlarmTransition(active=True, limit="High", value=80.1)
    assert ev.on_value(90) is None  # уже активен
    assert ev.on_value(76) is None  # зона гистерезиса (75, 80]
    assert ev.on_value(75) == AlarmTransition(active=False, limit=None, value=75)
    assert ev.active is False


def test_low_limit_with_deadband() -> None:
    ev = limit(low=10, deadband=2)
    assert ev.on_value(9.9) == AlarmTransition(active=True, limit="Low", value=9.9)
    assert ev.on_value(11.5) is None  # < low + deadband
    assert ev.on_value(12) == AlarmTransition(active=False, limit=None, value=12)


def test_direct_jump_high_to_low() -> None:
    ev = limit(low=10, high=90)
    assert ev.on_value(95) is not None
    jump = ev.on_value(5)
    assert jump == AlarmTransition(active=True, limit="Low", value=5)


def test_zero_deadband_clears_immediately() -> None:
    ev = limit(high=50)
    ev.on_value(51)
    assert ev.on_value(50) == AlarmTransition(active=False, limit=None, value=50)


def test_nan_is_ignored() -> None:
    ev = limit(high=50)
    assert ev.on_value(math.nan) is None
    assert ev.active is False


def test_periodic_alternates_by_period() -> None:
    ev = AlarmEvaluator(AlarmSpec(name="P", kind="periodic", period_s=10))
    assert ev.on_time(0) is None  # чётный период — норма, как и начальное состояние
    assert ev.on_time(9.9) is None
    assert ev.on_time(10) == AlarmTransition(active=True)
    assert ev.on_time(15) is None
    assert ev.on_time(20) == AlarmTransition(active=False)


def test_manual_trigger_toggles_and_sets() -> None:
    ev = AlarmEvaluator(AlarmSpec(name="M", kind="manual"))
    assert ev.on_trigger().active is True
    assert ev.on_trigger().active is False
    assert ev.on_trigger(True).active is True
    assert ev.on_trigger(True).active is True  # повтор: событие уходит, состояние то же


def test_manual_clear_resets_limit_state() -> None:
    ev = limit(high=10)
    ev.on_value(20)
    assert ev.on_trigger(False) == AlarmTransition(active=False, limit=None)
    # после ручного сброса превышение снова даёт событие активации
    assert ev.on_value(20) == AlarmTransition(active=True, limit="High", value=20)


def test_wrong_method_for_kind_rejected() -> None:
    with pytest.raises(ValueError, match="не limit"):
        AlarmEvaluator(AlarmSpec(name="P", kind="periodic")).on_value(1)
    with pytest.raises(ValueError, match="не periodic"):
        limit(high=1).on_time(1)
