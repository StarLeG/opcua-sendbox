"""WorkerHandle + worker на настоящих дочерних процессах (spawn)."""
from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest
from asyncua import Client, ua

from app.opcua.port_allocator import probe_bind
from app.opcua.tags import AlarmSpec, TagSpec, demo_alarms, demo_tags
from app.opcua.worker import WorkerParams
from app.opcua.worker_handle import (
    WorkerCommandError,
    WorkerGoneError,
    WorkerHandle,
    WorkerStartError,
)
from tests.integration.conftest import PUBLIC_HOST


def params_for(port: int, **changes: Any) -> WorkerParams:
    base: dict[str, Any] = {
        "port": port,
        "endpoint_url": f"opc.tcp://{PUBLIC_HOST}:{port}/electroman/",
        "history_size": 100,
        "state_hz": 10.0,
        "tags": tuple(t.model_dump() for t in demo_tags(shared=False)),
        "alarms": tuple(a.model_dump() for a in demo_alarms()),
    }
    base.update(changes)
    return WorkerParams(**base)


async def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("условие не выполнилось за отведённое время")
        await asyncio.sleep(0.05)


def ns_node(name: str) -> str:
    return f"ns=2;s=Tags.{name}"


async def test_worker_serves_changing_values_and_stops_cleanly(port: int) -> None:
    messages: list[dict[str, Any]] = []
    handle = WorkerHandle(params_for(port), on_message=messages.append)
    await handle.start()
    try:
        async with Client(f"opc.tcp://127.0.0.1:{port}/electroman/") as client:
            node = client.get_node(ns_node("Counter"))
            first = await node.read_value()
            await asyncio.sleep(1.2)
            assert await node.read_value() != first
        await wait_for(lambda: any(m["type"] == "tag_values" for m in messages))
        snapshot = next(m for m in messages if m["type"] == "tag_values")
        assert set(snapshot["values"]) == {t.name for t in demo_tags(shared=False)}
        assert set(snapshot["alarms"]) == {"SineHigh", "Heartbeat"}
    finally:
        await handle.stop()
    assert handle.exitcode == 0
    assert not handle.is_alive()
    assert probe_bind(port)  # порт свободен
    exited = [m for m in messages if m["type"] == "exited"]
    assert exited == [{"type": "exited", "exitcode": 0, "expected": True}]


async def test_requests_add_remove_and_errors(port: int) -> None:
    handle = WorkerHandle(params_for(port, tags=(), alarms=()))
    await handle.start()
    try:
        assert await handle.request("ping") == "pong"
        await handle.request("add_tag", spec=TagSpec(name="New", kind="constant", params={"value": 5}).model_dump())
        with pytest.raises(WorkerCommandError, match="уже есть"):
            await handle.request("add_tag", spec=TagSpec(name="New").model_dump())
        with pytest.raises(WorkerCommandError, match="неизвестная команда"):
            await handle.request("format_c")
        async with Client(f"opc.tcp://127.0.0.1:{port}/electroman/") as client:
            assert await client.get_node(ns_node("New")).read_value() == 5.0
        described = await handle.request("describe")
        assert [t["name"] for t in described["tags"]] == ["New"]
        await handle.request("remove_tag", name="New")
        assert (await handle.request("describe"))["tags"] == []
    finally:
        await handle.stop()


async def test_write_and_alarm_reach_control_channel(port: int) -> None:
    messages: list[dict[str, Any]] = []
    handle = WorkerHandle(params_for(port), on_message=messages.append)
    await handle.start()
    try:
        async with Client(f"opc.tcp://127.0.0.1:{port}/electroman/") as client:
            await client.get_node(ns_node("Setpoint")).write_value(12.5)
            await wait_for(lambda: {"type": "write", "tag": "Setpoint", "value": 12.5} in messages)
            await wait_for(lambda: any(m["type"] == "client_count" and m["clients"] >= 1 for m in messages))
        assert await handle.request("trigger_alarm", name="Heartbeat", active=True) is True
        await wait_for(lambda: any(m["type"] == "alarm" and m["alarm"] == "Heartbeat" for m in messages))
    finally:
        await handle.stop()


async def test_start_failure_on_busy_port(port: int) -> None:
    """Порт занят чужим процессом между пробой и bind — ошибка запуска, процесс убран."""
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", port))
        blocker.listen()
        handle = WorkerHandle(params_for(port))
        with pytest.raises(WorkerStartError):
            await handle.start(timeout=15)
    assert not handle.is_alive()


async def test_invalid_spec_fails_start(port: int) -> None:
    bad = ({"name": "A", "kind": "limit", "tag": "Missing", "high": 1.0},)
    handle = WorkerHandle(params_for(port, tags=(), alarms=bad))
    with pytest.raises(WorkerStartError, match="Missing"):
        await handle.start()
    assert not handle.is_alive()


async def test_crash_is_reported_and_requests_fail(port: int) -> None:
    messages: list[dict[str, Any]] = []
    handle = WorkerHandle(params_for(port, tags=(), alarms=()), on_message=messages.append)
    await handle.start()
    handle._process.kill()  # авария процесса
    await wait_for(lambda: any(m["type"] == "exited" for m in messages))
    exited = next(m for m in messages if m["type"] == "exited")
    assert exited["expected"] is False and exited["exitcode"] != 0
    with pytest.raises(WorkerGoneError):
        await handle.request("ping")
    await handle.stop()  # повторная остановка мёртвого процесса — без ошибок
    assert probe_bind(port)


async def test_read_only_worker_rejects_changes(port: int) -> None:
    handle = WorkerHandle(params_for(port, read_only=True, tags=(), alarms=()))
    await handle.start()
    try:
        with pytest.raises(WorkerCommandError, match="только для чтения"):
            await handle.request("add_tag", spec=TagSpec(name="X").model_dump())
        with pytest.raises(WorkerCommandError, match="только для чтения"):
            await handle.request("trigger_alarm", name="X")
    finally:
        await handle.stop()


async def test_slow_reader_drops_telemetry_but_not_control(port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Инвариант 2: медленный читатель телеметрии — дропы растут, control не теряется."""
    monkeypatch.setattr("app.opcua.worker.STATS_INTERVAL_S", 0.0)
    messages: list[dict[str, Any]] = []
    tags = (TagSpec(name="Sp", kind="constant", params={"value": 0.0}, writable=True).model_dump(),)
    alarms = (AlarmSpec(name="M", kind="manual").model_dump(),)
    handle = WorkerHandle(
        params_for(port, tags=tags, alarms=alarms, state_hz=50.0),
        on_message=messages.append,
        telemetry_queue_size=2,
    )
    handle.pause_telemetry()
    await handle.start()
    try:
        await asyncio.sleep(1.5)  # 50 Гц в очередь на 2 сообщения
        async with Client(f"opc.tcp://127.0.0.1:{port}/electroman/") as client:
            for value in (1.0, 2.0, 3.0):
                await client.get_node(ns_node("Sp")).write_value(value)
        for _ in range(3):
            await handle.request("trigger_alarm", name="M")
        await wait_for(lambda: len([m for m in messages if m["type"] == "write"]) == 3)
        await wait_for(lambda: len([m for m in messages if m["type"] == "alarm"]) == 3)
        await wait_for(lambda: handle.telemetry_dropped > 0, timeout=8)
        handle.pause_telemetry(False)
        await wait_for(lambda: any(m["type"] == "tag_values" for m in messages))
    finally:
        await handle.stop()


async def test_events_from_worker_reach_opcua_client(port: int) -> None:
    handle = WorkerHandle(params_for(port, tags=(), alarms=(AlarmSpec(name="M", kind="manual").model_dump(),)))
    await handle.start()
    received: list[Any] = []

    class Handler:
        def event_notification(self, event: Any) -> None:
            received.append(event)

    try:
        async with Client(f"opc.tcp://[::1]:{port}/electroman/") as client:
            sub = await client.create_subscription(100, Handler())
            await sub.subscribe_alarms_and_conditions(ua.ObjectIds.Server, ua.ObjectIds.AlarmConditionType)
            await handle.request("trigger_alarm", name="M", active=True)
            await wait_for(lambda: bool(received))
    finally:
        await handle.stop()
