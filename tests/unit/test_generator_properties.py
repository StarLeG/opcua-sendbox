"""Свойства генераторов: границы, периодичность, монотонность (перенесены из Modbus SendBox).

Периодические генераторы повторяются через `period_s`, монотонные
(counter/countdown/ramp_oneshot) не меняют направление, значения числовых
генераторов не выходят за [min, max] за долгий прогон. Случайные — с seeded rng.
"""
from __future__ import annotations

import random
from itertools import pairwise
from typing import Any

import pytest

from app.opcua.generators import ValueGenerator, create_generator


def _gen(kind: str, params: dict[str, Any], *, seed: int = 7) -> ValueGenerator:
    return create_generator(kind, params, random.Random(seed), boolean=kind == "constant")


# Числовые генераторы с явными границами [lo, hi] долгого прогона.
BOUNDED_CASES: list[tuple[str, dict[str, Any], float, float]] = [
    ("counter", {"start": 5, "step": 3, "min": 0, "max": 40, "wrap": False}, 0, 40),
    ("counter", {"start": 5, "step": 3, "min": 0, "max": 40}, 0, 40),  # wrap → 0..40
    ("sawtooth", {"min": -3, "max": 7, "period_s": 2.0}, -3, 7),
    ("triangle", {"min": -3, "max": 7, "period_s": 2.0}, -3, 7),
    ("meander", {"min": -3, "max": 7, "period_s": 2.0, "duty": 0.3}, -3, 7),
    ("sine", {"min": -3, "max": 7, "period_s": 2.0}, -3, 7),
    ("random_uniform", {"min": -3, "max": 7}, -3, 7),
    ("random_walk", {"min": -3, "max": 7, "step": 1.0}, -3, 7),
    ("ramp_oneshot", {"from": -3, "to": 7, "duration_s": 4.0, "then": "hold"}, -3, 7),
    ("countdown", {"start": 30, "step": 2, "min": 0}, 0, 30),
    ("step", {"values": [-3, 0, 7], "interval_s": 0.5}, -3, 7),
]

PERIODIC_KINDS: dict[str, dict[str, Any]] = {
    "sawtooth": {"min": -3, "max": 7, "period_s": 2.0},
    "triangle": {"min": -3, "max": 7, "period_s": 2.0},
    "meander": {"min": -3, "max": 7, "period_s": 2.0, "duty": 0.3},
    "sine": {"min": -3, "max": 7, "period_s": 2.0},
    "step": {"values": [-3, 0, 7], "interval_s": 0.5},
}

DISCRETE_KINDS: dict[str, dict[str, Any]] = {
    "constant": {"value": 1},
    "toggle": {"period_s": 1.0},
    "pwm": {"period_s": 1.0, "duty": 0.25},
    "random_bit": {"p": 0.5},
    "counter_bits": {"bit": 0},
}


@pytest.mark.parametrize(("kind", "params", "lo", "hi"), BOUNDED_CASES)
def test_numeric_behaviors_stay_in_bounds(
    kind: str, params: dict[str, Any], lo: float, hi: float
) -> None:
    """Значение любого числового генератора за долгий прогон — внутри окна."""
    gen = _gen(kind, params)
    for tick in range(1000):
        value = gen.update(tick * 0.1)
        assert lo <= value <= hi, f"{kind} t={tick * 0.1}: {value}"


@pytest.mark.parametrize(("kind", "params"), list(PERIODIC_KINDS.items()))
def test_periodic_behaviors_repeat_after_period(kind: str, params: dict[str, Any]) -> None:
    """f(t + period) == f(t): циклические генераторы детерминированы фазой."""
    gen = _gen(kind, params)
    period = float(params["period_s"]) if "period_s" in params else 1.0
    interval = float(params.get("interval_s", 0.25))
    if kind == "step":
        period = interval * len(params["values"])
    for tick in range(20):
        t = tick * (period / 8)  # кратные периоду точки — без ошибок округления
        # approx: sin/cos в точке 2π отличается от 0 на машинный эпсилон
        assert gen.update(t + period) == pytest.approx(gen.update(t)), f"{kind} t={t}"


@pytest.mark.parametrize(
    ("kind", "params", "direction"),
    [
        ("counter", {"start": 0, "step": 2, "min": 0, "max": 1000, "wrap": False}, +1),
        ("counter", {"start": 100, "step": -3, "min": 0, "max": 100, "wrap": False}, -1),
        ("countdown", {"start": 50, "step": 1, "min": 0}, -1),
        ("ramp_oneshot", {"from": 0, "to": 10, "duration_s": 5.0, "then": "hold"}, +1),
    ],
)
def test_monotonic_behaviors_keep_direction(
    kind: str, params: dict[str, Any], direction: int
) -> None:
    """counter/countdown/ramp_oneshot не меняют направление изменения."""
    gen = _gen(kind, params)
    values = [gen.update(tick * 0.5) for tick in range(40)]
    deltas = [b - a for a, b in pairwise(values)]
    if direction > 0:
        assert all(d >= 0 for d in deltas), f"{kind}: {values}"
    else:
        assert all(d <= 0 for d in deltas), f"{kind}: {values}"


@pytest.mark.parametrize(("kind", "params"), list(DISCRETE_KINDS.items()))
def test_discrete_behaviors_return_bool(kind: str, params: dict[str, Any]) -> None:
    """Дискретные генератора всегда возвращают bool (не 0/1)."""
    gen = _gen(kind, params)
    for tick in range(50):
        assert isinstance(gen.update(tick * 0.1), bool), kind
