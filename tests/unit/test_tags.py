"""Схемы тегов и алармов, приведение значений к типу, демо-набор."""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.opcua.tags import (
    AlarmSpec,
    TagSpec,
    check_session_content,
    coerce_value,
    demo_alarms,
    demo_tags,
)

# --- coerce_value -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "data_type", "expected"),
    [
        (12.6, "Int16", 13),
        (-12.5, "Int32", -12),  # банковское округление round()
        (1e9, "Int16", 32767),
        (-1e9, "Int16", -32768),
        (-5, "UInt16", 0),
        (2**40, "UInt32", 2**32 - 1),
        (math.inf, "Int32", 2**31 - 1),
        (-math.inf, "UInt16", 0),
        (math.nan, "Int32", 0),
        (3, "Double", 3.0),
        (True, "Float", 1.0),
        (0.0, "Boolean", False),
        (2, "Boolean", True),
    ],
)
def test_coerce_value(value: float, data_type: str, expected: object) -> None:
    result = coerce_value(value, data_type)
    assert result == expected
    assert type(result) is type(expected)


# --- TagSpec ----------------------------------------------------------------


def test_tag_defaults() -> None:
    tag = TagSpec(name="T1")
    assert (tag.data_type, tag.kind, tag.update_period_s, tag.writable) == ("Double", "sine", 1.0, False)


@pytest.mark.parametrize("name", ["", "1abc", "has space", "dot.name", "x" * 65, "Тег"])
def test_tag_name_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        TagSpec(name=name)


@pytest.mark.parametrize("period", [0.0, 0.05, 61.0, -1.0])
def test_update_period_bounds(period: float) -> None:
    with pytest.raises(ValidationError):
        TagSpec(name="T", update_period_s=period)


def test_bit_kind_requires_boolean() -> None:
    with pytest.raises(ValidationError, match="Boolean"):
        TagSpec(name="T", kind="toggle")
    assert TagSpec(name="T", kind="toggle", data_type="Boolean").kind == "toggle"


def test_numeric_kind_rejects_boolean() -> None:
    with pytest.raises(ValidationError, match="числовой"):
        TagSpec(name="T", kind="sine", data_type="Boolean")


def test_constant_fits_any_type() -> None:
    assert TagSpec(name="B", kind="constant", data_type="Boolean", params={"value": 1})
    assert TagSpec(name="I", kind="constant", data_type="Int16", params={"value": 7})


def test_writable_only_for_constant() -> None:
    with pytest.raises(ValidationError, match="writable"):
        TagSpec(name="T", kind="sine", writable=True)
    assert TagSpec(name="T", kind="constant", writable=True).writable


def test_generator_params_validated() -> None:
    """Неверные параметры генератора — ошибка схемы (422 в REST), а не сбой воркера."""
    with pytest.raises(ValidationError, match="sine"):
        TagSpec(name="T", kind="sine", params={"min": 10, "max": 0})
    with pytest.raises(ValidationError, match="period_s"):
        TagSpec(name="T", kind="sawtooth", params={"period_s": 0})
    with pytest.raises(ValidationError, match="должен быть числом"):
        TagSpec(name="T", kind="counter", params={"step": "one"})


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        TagSpec(name="T", colour="red")  # type: ignore[call-arg]


# --- AlarmSpec --------------------------------------------------------------


def test_limit_alarm_requires_tag_and_threshold() -> None:
    with pytest.raises(ValidationError, match="tag"):
        AlarmSpec(name="A", kind="limit", high=1)
    with pytest.raises(ValidationError, match="high"):
        AlarmSpec(name="A", kind="limit", tag="T")
    with pytest.raises(ValidationError, match="low"):
        AlarmSpec(name="A", kind="limit", tag="T", low=5, high=5)
    assert AlarmSpec(name="A", kind="limit", tag="T", low=1).low == 1


def test_non_limit_alarm_rejects_threshold_fields() -> None:
    with pytest.raises(ValidationError, match="limit"):
        AlarmSpec(name="A", kind="periodic", tag="T")
    assert AlarmSpec(name="A", kind="manual").kind == "manual"


@pytest.mark.parametrize(
    "changes",
    [{"severity": 0}, {"severity": 1001}, {"period_s": 0.5}, {"deadband": -1}],
)
def test_alarm_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AlarmSpec(name="A", kind="periodic", **changes)


# --- check_session_content ----------------------------------------------------


def test_session_content_limits_and_uniqueness() -> None:
    tags = [TagSpec(name="A"), TagSpec(name="B")]
    check_session_content(tags, [], max_tags=2, max_alarms=0)
    with pytest.raises(ValueError, match="лимита 1"):
        check_session_content(tags, [], max_tags=1, max_alarms=0)
    with pytest.raises(ValueError, match="дважды"):
        check_session_content([TagSpec(name="A"), TagSpec(name="A")], [], max_tags=5, max_alarms=0)
    alarms = [AlarmSpec(name="X", kind="manual"), AlarmSpec(name="X", kind="manual")]
    with pytest.raises(ValueError, match="дважды"):
        check_session_content([], alarms, max_tags=5, max_alarms=5)
    with pytest.raises(ValueError, match="алармов"):
        check_session_content([], alarms, max_tags=5, max_alarms=1)


def test_alarm_must_reference_numeric_tag() -> None:
    bool_tag = TagSpec(name="Flag", data_type="Boolean", kind="toggle")
    with pytest.raises(ValueError, match="несуществующий"):
        check_session_content(
            [], [AlarmSpec(name="A", tag="Missing", high=1)], max_tags=5, max_alarms=5
        )
    with pytest.raises(ValueError, match="Boolean"):
        check_session_content(
            [bool_tag], [AlarmSpec(name="A", tag="Flag", high=1)], max_tags=5, max_alarms=5
        )


# --- демо-набор ---------------------------------------------------------------


@pytest.mark.parametrize("shared", [True, False])
def test_demo_set_is_valid(shared: bool) -> None:
    tags, alarms = demo_tags(shared=shared), demo_alarms()
    check_session_content(tags, alarms, max_tags=100, max_alarms=20)
    writable = [t.name for t in tags if t.writable]
    assert writable == ([] if shared else ["Setpoint"])  # общий сервер read-only
    assert {t.data_type for t in tags} >= {"Double", "Float", "Int16", "UInt32", "Boolean"}
