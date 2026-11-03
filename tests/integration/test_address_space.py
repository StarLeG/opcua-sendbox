"""AddressSpace на настоящем сервере asyncua: теги, запись, история, алармы, endpoint."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asyncua import Client, ua

from app.opcua.address_space import client_count
from app.opcua.tags import AlarmSpec, TagSpec, demo_alarms, demo_tags
from tests.integration.conftest import PUBLIC_HOST, Sandbox, make_sandbox


class EventCollector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event_notification(self, event: Any) -> None:
        fields = event.get_event_props_as_fields_dict()
        self.events.append({k: v.Value if isinstance(v, ua.Variant) else v for k, v in fields.items()})


async def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("условие не выполнилось за отведённое время")
        await asyncio.sleep(0.05)


async def test_tag_value_changes_and_is_readable(sandbox: Sandbox) -> None:
    """ТЗ: клиент подключается, читает тег и видит, что значение обновляется."""
    await sandbox.space.add_tag(
        TagSpec(name="Saw", kind="sawtooth", params={"period_s": 2}, update_period_s=0.1)
    )
    sandbox.space.start()
    async with Client(sandbox.url) as client:
        node = client.get_node(sandbox.space.tag_node_id("Saw"))
        first = await node.read_value()
        await asyncio.sleep(0.5)
        second = await node.read_value()
        assert first != second
        assert (await node.read_data_value()).SourceTimestamp is not None


async def test_ipv6_loopback_connects(sandbox: Sandbox) -> None:
    """ADR-002: сервер слушает IPv6 — UaExpert и ModbusToolkit ходят на ::1."""
    await sandbox.space.add_tag(TagSpec(name="C", kind="constant", params={"value": 3}))
    async with Client(f"opc.tcp://[::1]:{sandbox.port}/electroman/") as client:
        assert await client.get_node(sandbox.space.tag_node_id("C")).read_value() == 3.0


async def test_get_endpoints_announces_public_host(sandbox: Sandbox) -> None:
    """ADR-002: без EndpointUrl в запросе сервер отдаёт публичный хост, а не sockname."""
    client = Client(sandbox.url)
    await client.connect_socket()
    try:
        await client.send_hello()
        await client.open_secure_channel()
        params = ua.GetEndpointsParameters()
        params.EndpointUrl = ""
        endpoints = await client.uaclient.get_endpoints(params)
        await client.close_secure_channel()
    finally:
        client.disconnect_socket()
    expected = f"opc.tcp://{PUBLIC_HOST}:{sandbox.port}/electroman/"
    assert {e.EndpointUrl for e in endpoints} == {expected}
    assert {e.SecurityPolicyUri for e in endpoints} == {"http://opcfoundation.org/UA/SecurityPolicy#None"}


async def test_data_types_and_coercion(sandbox: Sandbox) -> None:
    space = sandbox.space
    await space.add_tag(TagSpec(name="I16", data_type="Int16", kind="constant", params={"value": 99999}))
    await space.add_tag(TagSpec(name="F", data_type="Float", kind="constant", params={"value": 1}))
    await space.add_tag(TagSpec(name="B", data_type="Boolean", kind="toggle"))
    async with Client(sandbox.url) as client:
        i16 = await client.get_node(space.tag_node_id("I16")).read_data_value()
        assert (i16.Value.Value, i16.Value.VariantType) == (32767, ua.VariantType.Int16)
        f = await client.get_node(space.tag_node_id("F")).read_data_value()
        assert f.Value.VariantType == ua.VariantType.Float
        b = await client.get_node(space.tag_node_id("B")).read_data_value()
        assert b.Value.VariantType == ua.VariantType.Boolean


async def test_history_read_on_running_server(sandbox: Sandbox) -> None:
    """ADR-002 п. 2: HistoryRead возвращает значения, сервер не останавливали."""
    await sandbox.space.add_tag(TagSpec(name="Cnt", kind="counter", params={"interval_s": 0.1}))
    start = datetime.now(UTC) - timedelta(seconds=1)
    for _ in range(5):
        await sandbox.space.update_tag("Cnt")
        await asyncio.sleep(0.12)
    await asyncio.sleep(1.5)
    async with Client(sandbox.url) as client:
        node = client.get_node(sandbox.space.tag_node_id("Cnt"))
        history = await node.read_raw_history(starttime=start, endtime=datetime.now(UTC))
        assert len(history) >= 5
        assert all(dv.SourceTimestamp is not None for dv in history)


async def test_external_write_without_source_timestamp(sandbox: Sandbox) -> None:
    """ADR-002 п. 2: запись клиента без SourceTimestamp — метка у значения и в истории."""
    space = sandbox.space
    await space.add_tag(TagSpec(name="Sp", kind="constant", params={"value": 1.0}, writable=True))
    start = datetime.now(UTC) - timedelta(seconds=1)
    async with Client(sandbox.url) as client:
        node = client.get_node(space.tag_node_id("Sp"))
        await node.write_attribute(
            ua.AttributeIds.Value, ua.DataValue(ua.Variant(42.0, ua.VariantType.Double))
        )
        current = await node.read_data_value()
        assert current.Value.Value == 42.0
        assert current.SourceTimestamp is not None  # проставил PreWrite
        await asyncio.sleep(1.0)
        history = await node.read_raw_history(starttime=start, endtime=datetime.now(UTC) + timedelta(seconds=5))
        assert 42.0 in [dv.Value.Value for dv in history]
        assert await client.get_node(space.tag_node_id("Sp")).read_value() == 42.0  # сервер жив
    assert {"type": "write", "tag": "Sp", "value": 42.0} in sandbox.events
    assert space.snapshot()["Sp"] == 42.0


async def test_non_writable_tag_rejects_write(sandbox: Sandbox) -> None:
    await sandbox.space.add_tag(TagSpec(name="Ro", kind="constant", params={"value": 1.0}))
    async with Client(sandbox.url) as client:
        with pytest.raises(ua.UaStatusCodeError):
            await client.get_node(sandbox.space.tag_node_id("Ro")).write_value(5.0)


async def test_read_only_space_ignores_writable_flag(port: int) -> None:
    """Общий сервер read-only: writable-тег в нём не принимает запись."""
    box = await make_sandbox(port, read_only=True)
    await box.space.add_tag(TagSpec(name="Sp", kind="constant", writable=True))
    async with box.server, Client(box.url) as client:
        with pytest.raises(ua.UaStatusCodeError):
            await client.get_node(box.space.tag_node_id("Sp")).write_value(5.0)


async def test_limit_alarm_events_and_condition_refresh(sandbox: Sandbox) -> None:
    """Аларм по порогу: событие от Server с полями условия, ConditionRefresh, сброс."""
    space = sandbox.space
    await space.add_tag(TagSpec(name="Sp", kind="constant", params={"value": 0.0}, writable=True))
    await space.add_alarm(AlarmSpec(name="SpHigh", kind="limit", tag="Sp", high=80, deadband=5))
    condition_id = space.alarm_node_id("SpHigh")
    async with Client(sandbox.url) as client:
        collector = EventCollector()
        sub = await client.create_subscription(100, collector)
        await sub.subscribe_alarms_and_conditions(ua.ObjectIds.Server, ua.ObjectIds.ExclusiveLimitAlarmType)
        tag = client.get_node(space.tag_node_id("Sp"))

        await tag.write_value(90.0)
        await wait_for(lambda: any(e.get("NodeId") == condition_id for e in collector.events))
        event = collector.events[-1]
        assert event["Retain"] is True
        assert event["ActiveState/Id"] is True
        assert event["AckedState/Id"] is True  # квитирование не поддерживается
        assert event["ConditionName"] == "SpHigh"
        assert event["Severity"] == 700
        assert event["SourceNode"] == space.tag_node_id("Sp")

        collector.events.clear()
        await client.get_node(ua.ObjectIds.ConditionType).call_method(
            ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh),
            ua.Variant(sub.subscription_id, ua.VariantType.UInt32),
        )
        await wait_for(lambda: any(e.get("NodeId") == condition_id for e in collector.events))

        collector.events.clear()
        await tag.write_value(77.0)  # зона гистерезиса — события нет
        await tag.write_value(70.0)
        await wait_for(lambda: any(e.get("ActiveState/Id") is False for e in collector.events))
    states = [e["active"] for e in sandbox.events if e["type"] == "alarm"]
    assert states == [True, False]


async def test_alarm_events_reach_sendbox_folder_too(sandbox: Sandbox) -> None:
    space = sandbox.space
    await space.add_alarm(AlarmSpec(name="M", kind="manual", message="ручной"))
    async with Client(sandbox.url) as client:
        collector = EventCollector()
        sub = await client.create_subscription(100, collector)
        await sub.subscribe_events(space.notifiers[0], ua.ObjectIds.AlarmConditionType)
        assert await space.trigger_alarm("M") is True
        await wait_for(lambda: bool(collector.events))
        assert collector.events[-1]["Message"].Text == "ручной"


async def test_periodic_alarm_with_fake_clock(port: int) -> None:
    now = [0.0]
    box = await make_sandbox(port)
    box.space._clock = lambda: now[0]  # детерминированное время для tick_alarm
    await box.space.add_alarm(AlarmSpec(name="P", kind="periodic", period_s=10))
    async with box.server:
        box.space.start()
        for t in (5.0, 10.0, 15.0, 20.0):
            now[0] = t
            await box.space.tick_alarm("P")
        await box.space.stop()
    assert [e["active"] for e in box.events if e["type"] == "alarm"] == [True, False]


async def test_remove_tag_and_alarm(sandbox: Sandbox) -> None:
    space = sandbox.space
    await space.add_tag(TagSpec(name="T", kind="constant"))
    await space.add_alarm(AlarmSpec(name="A", kind="limit", tag="T", high=1))
    with pytest.raises(ValueError, match="используют алармы"):
        await space.remove_tag("T")
    with pytest.raises(ValueError, match="уже есть"):
        await space.add_tag(TagSpec(name="T"))
    await space.remove_alarm("A")
    await space.remove_tag("T")
    assert space.tags == [] and space.alarms == []
    async with Client(sandbox.url) as client:
        with pytest.raises(ua.UaStatusCodeError):
            await client.get_node(space.tag_node_id("T")).read_value()


async def test_generator_error_keeps_server_alive(sandbox: Sandbox) -> None:
    await sandbox.space.add_tag(TagSpec(name="T", kind="sine"))

    class Broken:
        is_static = False

        def update(self, now: float) -> float:
            raise RuntimeError("сбой генератора")

    sandbox.space._tags["T"].generator = Broken()
    before = sandbox.space.snapshot()["T"]
    await sandbox.space.update_tag("T")  # не бросает
    assert sandbox.space.snapshot()["T"] == before


async def test_demo_set_builds_and_client_count(sandbox: Sandbox) -> None:
    for tag in demo_tags(shared=False):
        await sandbox.space.add_tag(tag)
    for alarm in demo_alarms():
        await sandbox.space.add_alarm(alarm)
    base = client_count()
    async with Client(sandbox.url) as client:
        assert client_count() == base + 1
        tags_folder = client.get_node(ua.NodeId("Tags", sandbox.space.ns))
        names = {(await n.read_browse_name()).Name for n in await tags_folder.get_children()}
        assert names == {t.name for t in demo_tags(shared=False)}
    await wait_for(lambda: client_count() == base)
