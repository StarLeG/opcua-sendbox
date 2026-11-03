"""Описания тегов и алармов сессии: схемы, приведение значений, демо-набор.

Модели Pydantic используются и в REST API (валидация запроса → 422), и в IPC
(команды воркеру), поэтому вся проверка параметров — здесь: генератор создаётся
пробно ещё при валидации, и ошибка параметров не доходит до воркера.
"""
from __future__ import annotations

import math
import random
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.opcua.generators import BIT_KINDS, NUMERIC_KINDS, GeneratorKind, create_generator

#: Типы данных OPC UA, доступные для тегов.
DataType = Literal["Double", "Float", "Int16", "Int32", "UInt16", "UInt32", "Boolean"]

#: Границы целочисленных типов (значение генератора приводится и насыщается).
INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "Int16": (-(2**15), 2**15 - 1),
    "Int32": (-(2**31), 2**31 - 1),
    "UInt16": (0, 2**16 - 1),
    "UInt32": (0, 2**32 - 1),
}

#: Имя тега/аларма — часть NodeId (`ns=2;s=Tags.<имя>`): латиница, цифры, `_`.
Name = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]

MIN_UPDATE_PERIOD_S = 0.1
MAX_UPDATE_PERIOD_S = 60.0


def coerce_value(value: float | bool, data_type: DataType) -> int | float | bool:
    """Привести значение генератора или записи клиента к типу тега.

    Целые типы — округление и насыщение на границах типа (NaN → 0); Float/Double —
    float; Boolean — bool.
    """
    if data_type == "Boolean":
        return bool(value)
    if data_type in ("Double", "Float"):
        return float(value)
    low, high = INTEGER_RANGES[data_type]
    number = float(value)
    if math.isnan(number):
        return 0
    if math.isinf(number):
        return high if number > 0 else low
    return min(max(round(number), low), high)


class TagSpec(BaseModel):
    """Тег сессии: переменная `ns=2;s=Tags.<name>` со своим генератором."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    data_type: DataType = "Double"
    kind: GeneratorKind = "sine"
    params: dict[str, Any] = Field(default_factory=dict)
    update_period_s: float = Field(default=1.0, ge=MIN_UPDATE_PERIOD_S, le=MAX_UPDATE_PERIOD_S)
    #: Клиент может писать в тег. Только для `constant`: генератор перезаписал бы запись.
    writable: bool = False
    #: Вести историю (HistoryRead).
    historize: bool = True
    description: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _check_kind_matches_type(self) -> TagSpec:
        boolean = self.data_type == "Boolean"
        if self.kind in BIT_KINDS and not boolean:
            raise ValueError(f"генератор {self.kind!r} даёт bool — нужен тип Boolean")
        if self.kind in NUMERIC_KINDS and boolean:
            raise ValueError(f"генератор {self.kind!r} числовой — тип Boolean не подходит")
        if self.writable and self.kind != "constant":
            raise ValueError("writable разрешён только для генератора constant")
        try:
            create_generator(self.kind, self.params, random.Random(0), boolean=boolean)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"параметры генератора {self.kind!r}: {exc}") from None
        return self


class AlarmSpec(BaseModel):
    """Аларм сессии: условие `ns=2;s=Alarms.<name>` (ADR-002 п. 3b).

    - `limit` — по порогу тега: активен при значении > high или < low, сброс с
      гистерезисом `deadband`;
    - `periodic` — каждые `period_s` переключается активен/норма;
    - `manual` — только по команде trigger.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name
    kind: Literal["limit", "periodic", "manual"] = "limit"
    tag: Name | None = None
    high: float | None = None
    low: float | None = None
    deadband: float = Field(default=0.0, ge=0)
    period_s: float = Field(default=10.0, ge=1.0, le=3600.0)
    severity: int = Field(default=700, ge=1, le=1000)
    message: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _check_limit_fields(self) -> AlarmSpec:
        if self.kind == "limit":
            if self.tag is None:
                raise ValueError("аларм limit требует tag")
            if self.high is None and self.low is None:
                raise ValueError("аларм limit требует high и/или low")
            if self.high is not None and self.low is not None and self.low >= self.high:
                raise ValueError(f"low {self.low} должен быть меньше high {self.high}")
        elif self.tag is not None or self.high is not None or self.low is not None:
            raise ValueError(f"tag/high/low относятся только к алармам limit, а не {self.kind!r}")
        return self


def check_session_content(
    tags: list[TagSpec], alarms: list[AlarmSpec], *, max_tags: int, max_alarms: int
) -> None:
    """Проверить набор сессии целиком: лимиты, уникальность имён, ссылки алармов.

    ValueError с понятным текстом при нарушении.
    """
    if len(tags) > max_tags:
        raise ValueError(f"тегов {len(tags)} — больше лимита {max_tags}")
    if len(alarms) > max_alarms:
        raise ValueError(f"алармов {len(alarms)} — больше лимита {max_alarms}")
    _check_unique([t.name for t in tags], "тег")
    _check_unique([a.name for a in alarms], "аларм")
    by_name = {t.name: t for t in tags}
    for alarm in alarms:
        if alarm.tag is None:
            continue
        tag = by_name.get(alarm.tag)
        if tag is None:
            raise ValueError(f"аларм {alarm.name!r} ссылается на несуществующий тег {alarm.tag!r}")
        if tag.data_type == "Boolean":
            raise ValueError(f"аларм {alarm.name!r}: порог по Boolean-тегу {tag.name!r} невозможен")


def _check_unique(names: list[str], what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"{what} {name!r} встречается дважды")
        seen.add(name)


def demo_tags(*, shared: bool) -> list[TagSpec]:
    """Демо-набор тегов: общий сервер и старт личной сессии.

    Writable-тег — только в личной сессии (общий сервер read-only).
    """
    tags = [
        TagSpec(name="Sine", kind="sine", params={"min": 0, "max": 100, "period_s": 60}),
        TagSpec(name="Sawtooth", kind="sawtooth", params={"min": 0, "max": 100, "period_s": 30}),
        TagSpec(name="Triangle", kind="triangle", params={"min": -50, "max": 50, "period_s": 40}),
        TagSpec(name="Counter", data_type="UInt32", kind="counter", params={"step": 1}),
        TagSpec(
            name="Temperature",
            data_type="Float",
            kind="random_walk",
            params={"min": 15, "max": 35, "step": 0.2, "mean_reversion": 0.02},
        ),
        TagSpec(name="Noise", kind="random_uniform", params={"min": -1.0, "max": 1.0}),
        TagSpec(
            name="Pressure",
            data_type="Int16",
            kind="meander",
            params={"min": 100, "max": 400, "period_s": 20},
        ),
        TagSpec(name="Pump", data_type="Boolean", kind="toggle", params={"period_s": 10}),
        TagSpec(name="Fault", data_type="Boolean", kind="random_bit", params={"p": 0.05}),
    ]
    if not shared:
        tags.append(
            TagSpec(
                name="Setpoint",
                kind="constant",
                params={"value": 50.0},
                writable=True,
                description="Запишите сюда значение из своего клиента",
            )
        )
    return tags


def demo_alarms() -> list[AlarmSpec]:
    """Демо-алармы: порог по Sine и периодический аларм для просмотрщиков A&C."""
    return [
        AlarmSpec(
            name="SineHigh",
            kind="limit",
            tag="Sine",
            high=80.0,
            deadband=5.0,
            severity=700,
            message="Sine выше 80",
        ),
        AlarmSpec(
            name="Heartbeat",
            kind="periodic",
            period_s=10.0,
            severity=300,
            message="Периодический аларм песочницы",
        ),
    ]
