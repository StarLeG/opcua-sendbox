"""SendBoxHistory: подстановка SourceTimestamp для записей без неё (ADR-002 п. 2)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from asyncua import ua

from app.opcua.address_space import SendBoxHistory

NODE = ua.NodeId(1, 2)


async def _history() -> SendBoxHistory:
    history = SendBoxHistory()
    await history.new_historized_node(NODE, period=None, count=10)
    return history


async def _read_all(history: SendBoxHistory) -> list[ua.DataValue]:
    start = datetime.now(UTC) - timedelta(hours=1)
    end = datetime.now(UTC) + timedelta(hours=1)
    values, _ = await history.read_node_history(NODE, start, end, 100)
    return values


async def test_missing_source_timestamp_takes_server_timestamp() -> None:
    history = await _history()
    stamp = datetime.now(UTC) - timedelta(seconds=5)
    await history.save_node_value(NODE, ua.DataValue(ua.Variant(42.0), ServerTimestamp=stamp))
    [saved] = await _read_all(history)
    assert saved.Value.Value == 42.0
    assert saved.SourceTimestamp == stamp


async def test_missing_both_timestamps_takes_now() -> None:
    history = await _history()
    before = datetime.now(UTC)
    await history.save_node_value(NODE, ua.DataValue(ua.Variant(1.0)))
    [saved] = await _read_all(history)
    assert saved.SourceTimestamp is not None
    assert before <= saved.SourceTimestamp <= datetime.now(UTC)


async def test_existing_source_timestamp_untouched_and_input_not_mutated() -> None:
    history = await _history()
    stamp = datetime.now(UTC) - timedelta(seconds=30)
    original = ua.DataValue(ua.Variant(7.0), SourceTimestamp=stamp)
    await history.save_node_value(NODE, original)
    no_stamp = ua.DataValue(ua.Variant(8.0))
    await history.save_node_value(NODE, no_stamp)
    first, second = await _read_all(history)
    assert first.SourceTimestamp == stamp
    assert second.SourceTimestamp is not None and second.SourceTimestamp != stamp
    assert no_stamp.SourceTimestamp is None  # исходный DataValue не меняется


async def test_count_limit_still_applies() -> None:
    history = await _history()
    for i in range(15):
        await history.save_node_value(NODE, ua.DataValue(ua.Variant(float(i))))
    values = await _read_all(history)
    assert [v.Value.Value for v in values] == [float(i) for i in range(5, 15)]
