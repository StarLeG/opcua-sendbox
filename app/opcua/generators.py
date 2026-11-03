"""Генераторы значений тегов.

Перенесены из Modbus SendBox (`modbus-test-server/app/modbus/generators.py`) без
привязки к регистрам: у генератора нет адреса и контекста, поэтому поведения
`follow` и `follow_bit` не переносились. Контракт прежний: `update(now)` —
функция времени (секунды от создания тега) и внутреннего состояния; числовые
генераторы возвращают int | float, битовые — bool. `is_static=True` — значение
не зависит от времени (обновлять узел не нужно).
"""
from __future__ import annotations

import bisect
import math
import random
from typing import Any, Literal, Protocol, get_args

#: Числовые генераторы (теги Double/Float/Int*/UInt*).
NumericKind = Literal[
    "counter", "sawtooth", "triangle", "meander", "sine",
    "random_uniform", "random_walk", "ramp_oneshot", "countdown", "step", "profile",
]
#: Битовые генераторы (теги Boolean).
BitKind = Literal["toggle", "pwm", "random_bit", "counter_bits"]
#: Все kind'ы; `constant` годится для любого типа данных.
GeneratorKind = Literal[NumericKind, BitKind, "constant"]

NUMERIC_KINDS: frozenset[str] = frozenset(get_args(NumericKind))
BIT_KINDS: frozenset[str] = frozenset(get_args(BitKind))


class ValueGenerator(Protocol):
    """Генератор значения одного тега."""

    is_static: bool

    def update(self, now: float) -> int | float | bool: ...


def _num(params: dict[str, Any], name: str, default: float) -> int | float:
    """Числовой параметр; bool и не-числа отклоняются (см. TASK-241)."""
    raw = params.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"параметр {name!r} должен быть числом, получено: {raw!r}")
    return raw


def _positive(params: dict[str, Any], name: str, default: float) -> int | float:
    value = _num(params, name, default)
    if value <= 0:
        raise ValueError(f"параметр {name!r} должен быть > 0, получено: {value!r}")
    return value


def _flag(params: dict[str, Any], name: str, default: bool) -> bool:
    raw = params.get(name, default)
    if not isinstance(raw, bool):
        raise TypeError(f"параметр {name!r} должен быть bool, получено: {raw!r}")
    return raw


class ConstantGenerator:
    """`constant`: фиксированное значение (0 / False по умолчанию)."""

    def __init__(self, value: float | bool) -> None:
        self.is_static = True
        self._value = value

    def update(self, now: float) -> int | float | bool:
        _ = now
        return self._value


class CounterGenerator:
    """`counter`: start + step * floor(now / interval_s), wrap/clamp в [min, max].

    `wrap=True` — цикл после max (при step < 0 — после min возвращается к max);
    `wrap=False` — насыщение на границе. `interval_s` — период шага (по умолчанию 1.0 —
    счётчик 1 Гц).
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._start = _num(params, "start", 0)
        self._step = _num(params, "step", 1)
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 2**32 - 1)
        self._wrap = _flag(params, "wrap", True)
        self._interval = _positive(params, "interval_s", 1.0)
        if self._max < self._min:
            raise ValueError(f"counter: max {self._max!r} < min {self._min!r}")

    def update(self, now: float) -> int | float:
        n = math.floor(now / self._interval)
        raw = self._start + self._step * n
        if not self._wrap:
            return min(max(raw, self._min), self._max)
        span = self._max - self._min + abs(self._step)
        if span == 0:
            return self._min
        value = self._min + ((raw - self._min) % span)
        return min(max(value, self._min), self._max)


class SawtoothGenerator:
    """`sawtooth`: линейная пила min→max за `period_s`, повтор; `phase` — доля периода."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 100)
        self._period = _positive(params, "period_s", 10.0)
        self._phase = _num(params, "phase", 0.0)
        if self._max < self._min:
            raise ValueError(f"sawtooth: max {self._max!r} < min {self._min!r}")

    def update(self, now: float) -> int | float:
        frac = (now / self._period + self._phase) % 1.0
        return self._min + (self._max - self._min) * frac


class TriangleGenerator:
    """`triangle`: min→max→min за `period_s`; `phase` — доля периода."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 100)
        self._period = _positive(params, "period_s", 10.0)
        self._phase = _num(params, "phase", 0.0)
        if self._max < self._min:
            raise ValueError(f"triangle: max {self._max!r} < min {self._min!r}")

    def update(self, now: float) -> int | float:
        frac = (now / self._period + self._phase) % 1.0
        span = self._max - self._min
        if frac < 0.5:
            return self._min + span * (frac * 2)
        return self._max - span * ((frac - 0.5) * 2)


class MeanderGenerator:
    """`meander`: max в первые `duty` доли периода, иначе min (меандр/прямоугольник)."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 100)
        self._period = _positive(params, "period_s", 10.0)
        self._duty = _num(params, "duty", 0.5)
        self._phase = _num(params, "phase", 0.0)
        if self._max < self._min:
            raise ValueError(f"meander: max {self._max!r} < min {self._min!r}")
        if not 0 <= self._duty <= 1:
            raise ValueError(f"meander: duty должен быть в 0..1, получено: {self._duty!r}")

    def update(self, now: float) -> int | float:
        frac = (now / self._period + self._phase) % 1.0
        return self._max if frac < self._duty else self._min


class SineGenerator:
    """`sine`: синусоида `(min+max)/2 + (max-min)/2 * sin(2π(now/period_s + phase))`."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 100)
        self._period = _positive(params, "period_s", 10.0)
        self._phase = _num(params, "phase", 0.0)
        if self._max < self._min:
            raise ValueError(f"sine: max {self._max!r} < min {self._min!r}")
        self._mid = (self._min + self._max) / 2
        self._amp = (self._max - self._min) / 2

    def update(self, now: float) -> int | float:
        return self._mid + self._amp * math.sin(2 * math.pi * (now / self._period + self._phase))


class RandomUniformGenerator:
    """`random_uniform`: равномерное случайное значение в [min, max] на каждом update.

    Если min и max — целые — `rng.randint` (включительно), иначе `rng.uniform`.
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        self._rng = rng
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 100)
        if self._max < self._min:
            raise ValueError(f"random_uniform: max {self._max!r} < min {self._min!r}")

    def update(self, now: float) -> int | float:
        _ = now
        if isinstance(self._min, int) and isinstance(self._max, int):
            return self._rng.randint(self._min, self._max)
        return self._rng.uniform(self._min, self._max)


class RandomWalkGenerator:
    """`random_walk`: блуждание `pos += U(-step, step) + mean_reversion * (center - pos)`.

    Стартовая точка — центр `[min, max]`; после каждого шага позиция клампится
    в `[min, max]`.
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        self._rng = rng
        self._min = _num(params, "min", 0)
        self._max = _num(params, "max", 100)
        self._step = _num(params, "step", 1)
        self._mean_rev = _num(params, "mean_reversion", 0.0)
        if self._max < self._min:
            raise ValueError(f"random_walk: max {self._max!r} < min {self._min!r}")
        if self._step < 0:
            raise ValueError(f"random_walk: step должен быть >= 0, получено: {self._step!r}")
        if not 0 <= self._mean_rev <= 1:
            raise ValueError(
                f"random_walk: mean_reversion должен быть в 0..1, получено: {self._mean_rev!r}"
            )
        self._center = (self._min + self._max) / 2
        self._pos = self._center

    def update(self, now: float) -> int | float:
        _ = now
        self._pos += self._rng.uniform(-self._step, self._step)
        self._pos += self._mean_rev * (self._center - self._pos)
        self._pos = min(max(self._pos, self._min), self._max)
        return self._pos


class RampOneshotGenerator:
    """`ramp_oneshot`: линейный переход от `from` к `to` за `duration_s`, затем `then`.

    `then`: `"hold"` — держать `to`; `"loop"` — повторять ramp по кругу;
    число — перейти на эту константу.
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._start = _num(params, "from", 0)
        self._to = _num(params, "to", 100)
        self._duration = _positive(params, "duration_s", 10.0)
        then = params.get("then", "hold")
        self._mode: Literal["hold", "loop", "value"]
        self._then_value: int | float = 0
        if isinstance(then, str):
            if then == "hold":
                self._mode = "hold"
            elif then == "loop":
                self._mode = "loop"
            else:
                raise ValueError(
                    f"ramp_oneshot: then должен быть 'hold', 'loop' или числом, получено: {then!r}"
                )
        elif isinstance(then, bool) or not isinstance(then, (int, float)):
            raise TypeError(
                f"ramp_oneshot: then должен быть 'hold', 'loop' или числом, получено: {then!r}"
            )
        else:
            self._mode = "value"
            self._then_value = then

    def update(self, now: float) -> int | float:
        if now >= self._duration:
            if self._mode == "loop":
                now = now % self._duration
            elif self._mode == "hold":
                return self._to
            else:
                return self._then_value
        return self._start + (self._to - self._start) * (now / self._duration)


class CountdownGenerator:
    """`countdown`: обратный отсчёт `max(min, start - step * floor(now / interval_s))`.

    Шаг раз в `interval_s` (как у `counter`); насыщение на `min`.
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._start = _num(params, "start", 100)
        self._step = _num(params, "step", 1)
        self._min = _num(params, "min", 0)
        self._interval = _positive(params, "interval_s", 1.0)
        if self._step <= 0:
            raise ValueError(f"countdown: step должен быть > 0, получено: {self._step!r}")
        if self._start < self._min:
            raise ValueError(f"countdown: start {self._start!r} < min {self._min!r}")

    def update(self, now: float) -> int | float:
        n = math.floor(now / self._interval)
        return max(self._min, self._start - self._step * n)


class StepGenerator:
    """`step`: циклический перебор списка `values`, смена раз в `interval_s`."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        raw = params.get("values", (0, 1))
        if not isinstance(raw, (list, tuple)):
            raise TypeError(f"step: values должен быть списком, получено: {raw!r}")
        values: list[int | float] = []
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise TypeError(f"step: элемент values должен быть числом, получено: {item!r}")
            values.append(item)
        if not values:
            raise ValueError("step: values не должен быть пустым")
        self._values = values
        self._interval = _positive(params, "interval_s", 1.0)

    def update(self, now: float) -> int | float:
        n = math.floor(now / self._interval)
        return self._values[n % len(self._values)]


class ProfileGenerator:
    """`profile`: своя кривая по точкам `(t, value)` с линейной интерполяцией.

    `points` — список пар `[t, value]`: времена строго возрастают, `t >= 0`
    (по умолчанию — линейный рост 0→10 за 10 с). До первой точки — значение
    первой; после последней — удержание последней (`repeat=False`, по умолчанию)
    или цикл с периодом `tN - t0` (`repeat=True`). Одна точка — постоянное
    значение (`is_static`).
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        raw = params.get("points", [(0, 0), (10, 10)])
        if not isinstance(raw, (list, tuple)):
            raise TypeError(f"profile: points должен быть списком пар [t, value], получено: {raw!r}")
        points: list[tuple[float, float]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise TypeError(f"profile: точка должна быть парой [t, value], получено: {item!r}")
            t_raw, v_raw = item
            if isinstance(t_raw, bool) or not isinstance(t_raw, (int, float)):
                raise TypeError(f"profile: время точки должно быть числом, получено: {item!r}")
            if isinstance(v_raw, bool) or not isinstance(v_raw, (int, float)):
                raise TypeError(f"profile: значение точки должно быть числом, получено: {item!r}")
            if t_raw < 0:
                raise ValueError(f"profile: время точки должно быть >= 0, получено: {t_raw!r}")
            if points and t_raw <= points[-1][0]:
                raise ValueError(f"profile: времена точек должны строго возрастать, получено: {t_raw!r}")
            points.append((float(t_raw), float(v_raw)))
        if not points:
            raise ValueError("profile: points не должен быть пустым")
        self._repeat = _flag(params, "repeat", False)
        if self._repeat and points[-1][0] == points[0][0]:
            raise ValueError("profile: repeat требует минимум двух точек с разным временем")
        self._points = points
        self._times = [t for t, _ in points]
        self._values = [v for _, v in points]
        self._span = points[-1][0] - points[0][0]
        if len(points) == 1:
            self.is_static = True

    def update(self, now: float) -> int | float:
        t0 = self._times[0]
        if now < t0:
            return self._values[0]
        if self._repeat:
            now = t0 + (now - t0) % self._span
        elif now >= self._times[-1]:
            return self._values[-1]
        i = bisect.bisect_right(self._times, now) - 1
        t_a, t_b = self._times[i], self._times[i + 1]
        v_a, v_b = self._values[i], self._values[i + 1]
        return v_a + (v_b - v_a) * ((now - t_a) / (t_b - t_a))


class ToggleGenerator:
    """`toggle`: меандр 50% — True первые `period_s / 2`, затем False (полный период = `period_s`)."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._period = _positive(params, "period_s", 1.0)

    def update(self, now: float) -> bool:
        return (now / self._period) % 1.0 < 0.5


class PwmGenerator:
    """`pwm`: True в первые `duty` доли периода, иначе False (скважность бита)."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        self._period = _positive(params, "period_s", 1.0)
        self._duty = _num(params, "duty", 0.5)
        if not 0 <= self._duty <= 1:
            raise ValueError(f"pwm: duty должен быть в 0..1, получено: {self._duty!r}")

    def update(self, now: float) -> bool:
        return (now / self._period) % 1.0 < self._duty


class RandomBitGenerator:
    """`random_bit`: True с вероятностью `p` на каждом update."""

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        self._rng = rng
        self._p = _num(params, "p", 0.5)
        if not 0 <= self._p <= 1:
            raise ValueError(f"random_bit: p должен быть в 0..1, получено: {self._p!r}")

    def update(self, now: float) -> bool:
        _ = now
        return self._rng.random() < self._p


class CounterBitsGenerator:
    """`counter_bits`: бит `bit` целочисленного счётчика секунд `(int(now) >> bit) & 1`.

    По умолчанию `bit = 0`; бит N меняется раз в 2^N секунд.
    """

    is_static = False

    def __init__(
        self,
        params: dict[str, Any],
        rng: random.Random,
    ) -> None:
        _ = rng
        raw = params.get("bit", 0)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError(f"counter_bits: bit должен быть целым, получено: {raw!r}")
        if raw < 0:
            raise ValueError(f"counter_bits: bit должен быть >= 0, получено: {raw!r}")
        self._bit = raw

    def update(self, now: float) -> bool:
        return bool((int(now) >> self._bit) & 1)


_FACTORIES: dict[str, type[Any]] = {
    "counter": CounterGenerator,
    "sawtooth": SawtoothGenerator,
    "triangle": TriangleGenerator,
    "meander": MeanderGenerator,
    "sine": SineGenerator,
    "random_uniform": RandomUniformGenerator,
    "random_walk": RandomWalkGenerator,
    "ramp_oneshot": RampOneshotGenerator,
    "countdown": CountdownGenerator,
    "step": StepGenerator,
    "profile": ProfileGenerator,
    "toggle": ToggleGenerator,
    "pwm": PwmGenerator,
    "random_bit": RandomBitGenerator,
    "counter_bits": CounterBitsGenerator,
}


def create_generator(
    kind: str,
    params: dict[str, Any] | None = None,
    rng: random.Random | None = None,
    *,
    boolean: bool = False,
) -> ValueGenerator:
    """Создать генератор по kind и параметрам.

    Параметры проверяются конструктором генератора: неверный тип — TypeError,
    недопустимое значение — ValueError. `boolean=True` приводит `constant` к bool
    (тег Boolean). Неизвестный kind — ValueError.
    """
    params = params or {}
    if kind == "constant":
        raw = params.get("value", 0)
        if boolean:
            return ConstantGenerator(bool(raw))
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError(f"constant: value должен быть числом, получено: {raw!r}")
        return ConstantGenerator(raw)
    factory = _FACTORIES.get(kind)
    if factory is None:
        raise ValueError(f"неизвестный генератор {kind!r}")
    generator: ValueGenerator = factory(params, rng if rng is not None else random.Random())
    return generator
