"""Генераторы значений тегов: unit-тесты (перенесены из Modbus SendBox).

Детерминизм: `update(now)` — функция фиктивного времени; случайные
генераторы — с seeded `random.Random`. Проверяются границы и циклы.
"""
from __future__ import annotations

import math
import random
from typing import Any

import pytest

from app.opcua.generators import (
    BIT_KINDS,
    NUMERIC_KINDS,
    ConstantGenerator,
    ValueGenerator,
    create_generator,
)


def _gen(
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    seed: int = 42,
    boolean: bool = False,
) -> ValueGenerator:
    return create_generator(kind, params or {}, random.Random(seed), boolean=boolean)


# --- constant ----------------------------------------------------------------


def test_constant_numeric() -> None:
    gen = _gen("constant", {"value": 42})
    assert isinstance(gen, ConstantGenerator)
    assert gen.is_static is True
    assert gen.update(0.0) == 42
    assert gen.update(99.0) == 42  # значение не зависит от времени


def test_constant_bit() -> None:
    assert _gen("constant", {"value": 1}, boolean=True).update(0.0) is True
    assert _gen("constant", {"value": 0}, boolean=True).update(0.0) is False


def test_constant_default_zero() -> None:
    assert _gen("constant").update(0.0) == 0
    assert _gen("constant", boolean=True).update(0.0) is False


# --- counter -----------------------------------------------------------------


def test_counter_default_steps_once_per_second() -> None:
    gen = _gen("counter")
    assert gen.update(0.0) == 0
    assert gen.update(0.99) == 0
    assert gen.update(1.0) == 1
    assert gen.update(2.5) == 2


def test_counter_step_start_interval() -> None:
    gen = _gen("counter", {"start": 10, "step": 5, "interval_s": 0.1})
    assert gen.update(0.0) == 10
    assert gen.update(0.1) == 15
    assert gen.update(0.25) == 20


def test_counter_saturates_without_wrap() -> None:
    gen = _gen("counter", {"start": 4, "step": 1, "min": 0, "max": 5, "wrap": False})
    assert gen.update(1.0) == 5
    assert gen.update(9.0) == 5


def test_counter_wraps_after_max() -> None:
    gen = _gen("counter", {"start": 5, "step": 1, "min": 0, "max": 5, "wrap": True})
    assert gen.update(0.0) == 5
    assert gen.update(1.0) == 0
    assert gen.update(2.0) == 1


def test_counter_negative_step_wraps_to_max() -> None:
    gen = _gen("counter", {"start": 1, "step": -1, "min": 0, "max": 5, "wrap": True})
    assert gen.update(0.0) == 1
    assert gen.update(1.0) == 0
    assert gen.update(2.0) == 5


def test_counter_negative_step_saturates_at_min() -> None:
    gen = _gen("counter", {"start": 1, "step": -1, "min": 0, "max": 5, "wrap": False})
    assert gen.update(2.0) == 0
    assert gen.update(9.0) == 0


def test_counter_float_params() -> None:
    gen = _gen(
        "counter",
        {"start": 0.0, "step": 0.5, "min": 0.0, "max": 1.5, "wrap": True, "interval_s": 1},
    )
    assert gen.update(0.0) == 0.0
    assert gen.update(1.0) == 0.5
    assert gen.update(3.0) == 1.5
    assert gen.update(4.0) == 0.0  # цикл: span = max - min + step = 2.0


def test_counter_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="interval_s"):
        _gen("counter", {"interval_s": 0})
    with pytest.raises(ValueError, match="interval_s"):
        _gen("counter", {"interval_s": -1})


def test_counter_min_max_validation() -> None:
    with pytest.raises(ValueError, match="max"):
        _gen("counter", {"min": 5, "max": 1})


# --- sawtooth ----------------------------------------------------------------


def test_sawtooth_ramp_and_repeat() -> None:
    gen = _gen("sawtooth", {"min": 0, "max": 10, "period_s": 4, "phase": 0})
    assert gen.update(0.0) == 0
    assert gen.update(1.0) == 2.5
    assert gen.update(2.0) == 5
    assert gen.update(3.9) == pytest.approx(9.75)
    assert gen.update(4.0) == 0  # цикл повторяется


def test_sawtooth_phase() -> None:
    gen = _gen("sawtooth", {"min": 0, "max": 10, "period_s": 4, "phase": 0.25})
    assert gen.update(0.0) == 2.5


def test_sawtooth_flat_limits() -> None:
    gen = _gen("sawtooth", {"min": 3, "max": 3, "period_s": 2})
    assert gen.update(0.5) == 3


def test_sawtooth_validation() -> None:
    with pytest.raises(ValueError, match="period_s"):
        _gen("sawtooth", {"period_s": 0})
    with pytest.raises(ValueError, match="max"):
        _gen("sawtooth", {"min": 5, "max": 1})


# --- triangle ----------------------------------------------------------------


def test_triangle_wave() -> None:
    gen = _gen("triangle", {"min": 0, "max": 10, "period_s": 4, "phase": 0})
    assert gen.update(0.0) == 0
    assert gen.update(1.0) == 5
    assert gen.update(2.0) == 10
    assert gen.update(3.0) == 5
    assert gen.update(4.0) == 0


def test_triangle_phase() -> None:
    gen = _gen("triangle", {"min": 0, "max": 10, "period_s": 4, "phase": 0.25})
    assert gen.update(0.0) == 5
    assert gen.update(1.0) == 10


def test_triangle_validation() -> None:
    with pytest.raises(ValueError, match="period_s"):
        _gen("triangle", {"period_s": -1})
    with pytest.raises(ValueError, match="max"):
        _gen("triangle", {"min": 5, "max": 1})


# --- meander -----------------------------------------------------------------


def test_meander_duty_half() -> None:
    gen = _gen("meander", {"min": 0, "max": 10, "period_s": 4, "duty": 0.5})
    assert gen.update(0.0) == 10
    assert gen.update(1.9) == 10
    assert gen.update(2.0) == 0
    assert gen.update(3.9) == 0
    assert gen.update(4.0) == 10


def test_meander_duty_quarter() -> None:
    gen = _gen("meander", {"min": 0, "max": 10, "period_s": 4, "duty": 0.25, "phase": 0})
    assert gen.update(0.9) == 10
    assert gen.update(1.1) == 0


def test_meander_duty_extremes() -> None:
    assert _gen("meander", {"min": 0, "max": 10, "duty": 0}).update(0.5) == 0
    assert _gen("meander", {"min": 0, "max": 10, "duty": 1}).update(0.5) == 10


def test_meander_validation() -> None:
    with pytest.raises(ValueError, match="duty"):
        _gen("meander", {"duty": 1.5})
    with pytest.raises(ValueError, match="duty"):
        _gen("meander", {"duty": -0.1})
    with pytest.raises(ValueError, match="period_s"):
        _gen("meander", {"period_s": 0})


# --- sine --------------------------------------------------------------------


def test_sine_wave() -> None:
    gen = _gen("sine", {"min": -10, "max": 10, "period_s": 4, "phase": 0})
    assert gen.update(0.0) == pytest.approx(0)  # sin(0) = 0
    assert gen.update(1.0) == pytest.approx(10)  # sin(π/2) = 1 → max
    assert gen.update(2.0) == pytest.approx(0)  # sin(π) = 0
    assert gen.update(3.0) == pytest.approx(-10)  # sin(3π/2) = -1 → min
    assert gen.update(4.0) == pytest.approx(0)  # цикл


def test_sine_offset_and_amplitude() -> None:
    """min=0, max=100 → ось 50, амплитуда 50."""
    gen = _gen("sine", {"min": 0, "max": 100, "period_s": 4, "phase": 0})
    assert gen.update(0.0) == pytest.approx(50)
    assert gen.update(1.0) == pytest.approx(100)
    assert gen.update(3.0) == pytest.approx(0)


def test_sine_phase() -> None:
    gen = _gen("sine", {"min": -10, "max": 10, "period_s": 4, "phase": 0.25})
    assert gen.update(0.0) == pytest.approx(10)


def test_sine_validation() -> None:
    with pytest.raises(ValueError, match="period_s"):
        _gen("sine", {"period_s": 0})
    with pytest.raises(ValueError, match="max"):
        _gen("sine", {"min": 5, "max": 1})


# --- random_uniform ----------------------------------------------------------


def test_random_uniform_int_params_give_ints() -> None:
    gen = _gen("random_uniform", {"min": 0, "max": 10}, seed=7)
    for _ in range(20):
        value = gen.update(0.0)
        assert isinstance(value, int)
        assert 0 <= value <= 10


def test_random_uniform_float_params_give_floats() -> None:
    gen = _gen("random_uniform", {"min": 0.5, "max": 1.5}, seed=7)
    for _ in range(20):
        value = gen.update(0.0)
        assert isinstance(value, float)
        assert 0.5 <= value <= 1.5


def test_random_uniform_deterministic_with_seed() -> None:
    gen_a = _gen("random_uniform", {"min": 0, "max": 100}, seed=1)
    gen_b = _gen("random_uniform", {"min": 0, "max": 100}, seed=1)
    seq_a = [gen_a.update(0.0) for _ in range(5)]
    seq_b = [gen_b.update(0.0) for _ in range(5)]
    assert seq_a == seq_b  # одинаковый seed → одинаковая последовательность


def test_random_uniform_validation() -> None:
    with pytest.raises(ValueError, match="max"):
        _gen("random_uniform", {"min": 5, "max": 1})


# --- random_walk -------------------------------------------------------------


def test_random_walk_stays_in_bounds() -> None:
    gen = _gen("random_walk", {"min": 0, "max": 100, "step": 30, "mean_reversion": 0}, seed=3)
    for _ in range(200):
        value = gen.update(0.0)
        assert 0 <= value <= 100


def test_random_walk_starts_at_center() -> None:
    gen = _gen("random_walk", {"min": 0, "max": 100, "step": 0, "mean_reversion": 0}, seed=3)
    assert gen.update(0.0) == pytest.approx(50)  # step=0 → остаётся в центре


def test_random_walk_mean_reversion_pulls_to_center() -> None:
    gen = _gen("random_walk", {"min": 0, "max": 100, "step": 0, "mean_reversion": 0.5}, seed=3)
    gen._pos = 80.0  # type: ignore[attr-defined]  # начальное отклонение для проверки стягивания
    assert gen.update(0.0) == pytest.approx(65)  # 80 + 0.5*(50-80)


def test_random_walk_validation() -> None:
    with pytest.raises(ValueError, match="max"):
        _gen("random_walk", {"min": 5, "max": 1})
    with pytest.raises(ValueError, match="step"):
        _gen("random_walk", {"step": -1})
    with pytest.raises(ValueError, match="mean_reversion"):
        _gen("random_walk", {"mean_reversion": 1.5})


# --- ramp_oneshot ------------------------------------------------------------


def test_ramp_oneshot_hold() -> None:
    gen = _gen("ramp_oneshot", {"from": 0, "to": 100, "duration_s": 10, "then": "hold"})
    assert gen.update(0.0) == 0
    assert gen.update(5.0) == pytest.approx(50)
    assert gen.update(10.0) == 100  # держит to
    assert gen.update(25.0) == 100


def test_ramp_oneshot_loop() -> None:
    gen = _gen("ramp_oneshot", {"from": 0, "to": 100, "duration_s": 10, "then": "loop"})
    assert gen.update(2.0) == pytest.approx(20)
    assert gen.update(10.0) == 0  # цикл начинается заново
    assert gen.update(12.0) == pytest.approx(20)


def test_ramp_oneshot_numeric_then() -> None:
    gen = _gen("ramp_oneshot", {"from": 0, "to": 100, "duration_s": 10, "then": 55})
    assert gen.update(5.0) == pytest.approx(50)
    assert gen.update(10.0) == 55
    assert gen.update(20.0) == 55


def test_ramp_oneshot_downhill() -> None:
    gen = _gen("ramp_oneshot", {"from": 100, "to": 0, "duration_s": 4})
    assert gen.update(0.0) == 100
    assert gen.update(2.0) == pytest.approx(50)
    assert gen.update(4.0) == 0  # then по умолчанию — hold


def test_ramp_oneshot_validation() -> None:
    with pytest.raises(ValueError, match="duration_s"):
        _gen("ramp_oneshot", {"duration_s": 0})
    with pytest.raises(ValueError, match="then"):
        _gen("ramp_oneshot", {"then": "bogus"})
    with pytest.raises(TypeError, match="then"):
        _gen("ramp_oneshot", {"then": True})


# --- countdown ---------------------------------------------------------------


def test_countdown_default_steps_once_per_second() -> None:
    gen = _gen("countdown", {"start": 10, "step": 1})
    assert gen.update(0.0) == 10
    assert gen.update(0.99) == 10
    assert gen.update(1.0) == 9
    assert gen.update(3.5) == 7


def test_countdown_saturates_at_min() -> None:
    gen = _gen("countdown", {"start": 3, "step": 1, "min": 0})
    assert gen.update(3.0) == 0
    assert gen.update(99.0) == 0  # ниже min не опускается


def test_countdown_step_and_interval() -> None:
    gen = _gen("countdown", {"start": 100, "step": 25, "min": 10, "interval_s": 0.5})
    assert gen.update(0.0) == 100
    assert gen.update(0.5) == 75
    assert gen.update(1.0) == 50
    assert gen.update(3.6) == 10  # насыщение на min


def test_countdown_validation() -> None:
    with pytest.raises(ValueError, match="interval_s"):
        _gen("countdown", {"interval_s": 0})
    with pytest.raises(ValueError, match="step"):
        _gen("countdown", {"step": 0})
    with pytest.raises(ValueError, match="start"):
        _gen("countdown", {"start": 5, "min": 10})


# --- step --------------------------------------------------------------------


def test_step_cycles_values() -> None:
    gen = _gen("step", {"values": [10, 20, 30], "interval_s": 1})
    assert gen.update(0.0) == 10
    assert gen.update(0.5) == 10
    assert gen.update(1.0) == 20
    assert gen.update(2.0) == 30
    assert gen.update(3.0) == 10  # цикл
    assert gen.update(7.5) == 20


def test_step_float_values_and_fast_interval() -> None:
    gen = _gen("step", {"values": [0.5, -1.5], "interval_s": 0.1})
    assert gen.update(0.0) == 0.5
    assert gen.update(0.1) == -1.5
    assert gen.update(0.25) == 0.5


def test_step_validation() -> None:
    with pytest.raises(ValueError, match="values"):
        _gen("step", {"values": []})
    with pytest.raises(TypeError, match="values"):
        _gen("step", {"values": "1,2"})
    with pytest.raises(TypeError, match="values"):
        _gen("step", {"values": [1, "x"]})
    with pytest.raises(TypeError, match="values"):
        _gen("step", {"values": [True]})
    with pytest.raises(ValueError, match="interval_s"):
        _gen("step", {"values": [1], "interval_s": -1})


# --- profile -----------------------------------------------------------------


def test_profile_interpolates_between_points() -> None:
    gen = _gen("profile", {"points": [[0, 20], [10, 40], [20, 0]]})
    assert gen.is_static is False
    assert gen.update(0.0) == 20
    assert gen.update(5.0) == 30
    assert gen.update(10.0) == 40
    assert gen.update(15.0) == 20
    assert gen.update(20.0) == 0


def test_profile_holds_first_and_last_values() -> None:
    gen = _gen("profile", {"points": [[10, 5], [20, 7]]})
    assert gen.update(0.0) == 5  # до первой точки — значение первой
    assert gen.update(25.0) == 7  # после последней — удержание (repeat=False)


def test_profile_repeat_loops() -> None:
    gen = _gen("profile", {"points": [[0, 0], [10, 100]], "repeat": True})
    assert gen.update(5.0) == 50
    assert gen.update(10.0) == 0  # конец цикла → начало
    assert gen.update(15.0) == 50
    assert gen.update(21.0) == 10


def test_profile_repeat_with_offset_start() -> None:
    gen = _gen("profile", {"points": [[5, 1], [15, 3]], "repeat": True})
    assert gen.update(5.0) == 1
    assert gen.update(15.0) == 1  # конец цикла → начало
    assert gen.update(16.0) == pytest.approx(1.2)  # интерполяция во втором проходе


def test_profile_single_point_is_static() -> None:
    gen = _gen("profile", {"points": [[3, 42]]})
    assert gen.is_static is True
    assert gen.update(0.0) == 42
    assert gen.update(99.0) == 42


def test_profile_validation() -> None:
    with pytest.raises(ValueError, match="points"):
        _gen("profile", {"points": []})
    with pytest.raises(TypeError, match="points"):
        _gen("profile", {"points": 5})
    with pytest.raises(TypeError, match="парой"):
        _gen("profile", {"points": [[0, 1, 2]]})
    with pytest.raises(TypeError, match="время"):
        _gen("profile", {"points": [["a", 1]]})
    with pytest.raises(TypeError, match="значение"):
        _gen("profile", {"points": [[0, True]]})
    with pytest.raises(ValueError, match=">= 0"):
        _gen("profile", {"points": [[-1, 0]]})
    with pytest.raises(ValueError, match="возрастать"):
        _gen("profile", {"points": [[0, 0], [0, 1]]})
    with pytest.raises(ValueError, match="repeat"):
        _gen("profile", {"points": [[0, 0]], "repeat": True})


# --- toggle (bool) ------------------------------------------------------------


def test_toggle_flips_every_half_period() -> None:
    gen = _gen("toggle", {"period_s": 2})
    assert gen.update(0.0) is True
    assert gen.update(0.99) is True
    assert gen.update(1.0) is False
    assert gen.update(1.99) is False
    assert gen.update(2.0) is True  # новый период


def test_toggle_validation() -> None:
    with pytest.raises(ValueError, match="period_s"):
        _gen("toggle", {"period_s": 0})


# --- pwm (bool) --------------------------------------------------------------


def test_pwm_duty_quarter() -> None:
    gen = _gen("pwm", {"period_s": 4, "duty": 0.25})
    assert gen.update(0.0) is True
    assert gen.update(0.9) is True
    assert gen.update(1.0) is False
    assert gen.update(3.9) is False
    assert gen.update(4.0) is True  # новый период


def test_pwm_duty_extremes() -> None:
    assert _gen("pwm", {"duty": 0}).update(0.5) is False
    assert _gen("pwm", {"duty": 1}).update(0.5) is True


def test_pwm_validation() -> None:
    with pytest.raises(ValueError, match="duty"):
        _gen("pwm", {"duty": 1.5})
    with pytest.raises(ValueError, match="duty"):
        _gen("pwm", {"duty": -0.1})
    with pytest.raises(ValueError, match="period_s"):
        _gen("pwm", {"period_s": -1})


# --- random_bit (bool) --------------------------------------------------------


def test_random_bit_extremes() -> None:
    never = _gen("random_bit", {"p": 0}, seed=5)
    always = _gen("random_bit", {"p": 1}, seed=5)
    assert all(never.update(0.0) is False for _ in range(10))
    assert all(always.update(0.0) is True for _ in range(10))


def test_random_bit_deterministic_with_seed() -> None:
    gen_a = _gen("random_bit", {"p": 0.5}, seed=1)
    gen_b = _gen("random_bit", {"p": 0.5}, seed=1)
    seq_a = [gen_a.update(0.0) for _ in range(20)]
    seq_b = [gen_b.update(0.0) for _ in range(20)]
    assert seq_a == seq_b
    assert any(seq_a) and not all(seq_a)  # оба значения встречаются


def test_random_bit_validation() -> None:
    with pytest.raises(ValueError, match="p"):
        _gen("random_bit", {"p": 1.5})
    with pytest.raises(ValueError, match="p"):
        _gen("random_bit", {"p": -0.5})


# --- counter_bits (bool) ------------------------------------------------------


def test_counter_bits_default_bit_zero() -> None:
    gen = _gen("counter_bits")  # bit = 0 по умолчанию
    assert gen.update(0.0) is False
    assert gen.update(0.9) is False  # int(0.9) = 0
    assert gen.update(1.0) is True  # 1 >> 0 & 1 = 1
    assert gen.update(2.0) is False  # 2 >> 0 & 1 = 0
    assert gen.update(3.0) is True


def test_counter_bits_explicit_bit() -> None:
    gen = _gen("counter_bits", {"bit": 2})
    assert gen.update(3.0) is False  # 3 >> 2 & 1 = 0
    assert gen.update(4.0) is True  # 4 >> 2 & 1 = 1
    assert gen.update(5.0) is True  # 5 >> 2 & 1 = 1
    assert gen.update(8.0) is False  # 8 >> 2 & 1 = 0


def test_counter_bits_validation() -> None:
    with pytest.raises(ValueError, match="bit"):
        _gen("counter_bits", {"bit": -1})
    with pytest.raises(TypeError, match="bit"):
        _gen("counter_bits", {"bit": 1.5})
    with pytest.raises(TypeError, match="bit"):
        _gen("counter_bits", {"bit": True})


# --- фабрика и требования ТЗ ----------------------------------------------------------


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError, match="неизвестный"):
        _gen("follow")


def test_constant_numeric_value_must_be_number() -> None:
    with pytest.raises(TypeError, match="value"):
        _gen("constant", {"value": "abc"})


def test_every_kind_constructs_with_defaults() -> None:
    for kind in NUMERIC_KINDS | BIT_KINDS:
        value = _gen(kind).update(1.5)
        expected = bool if kind in BIT_KINDS else (int, float)
        assert isinstance(value, expected), kind


def test_sine_shape_at_key_points() -> None:
    """ТЗ: форма синусоиды — середина в 0, максимум в T/4, середина в T/2, минимум в 3T/4."""
    gen = _gen("sine", {"min": -10, "max": 10, "period_s": 8.0})
    assert gen.update(0.0) == pytest.approx(0.0, abs=1e-9)
    assert gen.update(2.0) == pytest.approx(10.0)
    assert gen.update(4.0) == pytest.approx(0.0, abs=1e-9)
    assert gen.update(6.0) == pytest.approx(-10.0)
    assert gen.update(8.0) == pytest.approx(gen.update(0.0), abs=1e-9)


def test_random_uniform_bounds_over_many_samples() -> None:
    """ТЗ: границы случайных чисел — 10 000 выборок внутри [min, max]."""
    gen = _gen("random_uniform", {"min": -5.5, "max": 7.25}, seed=1)
    samples = [float(gen.update(float(i))) for i in range(10_000)]
    assert all(-5.5 <= s <= 7.25 for s in samples)
    assert max(samples) - min(samples) > 12  # выборка покрывает почти весь диапазон
    assert not any(math.isnan(s) for s in samples)
