"""SessionManager на настоящих процессах: изоляция сессий, гонки create/delete, общий сервер."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import psutil
import pytest
from asyncua import Client, ua

from app.config import Config
from app.opcua.port_allocator import probe_bind
from app.opcua.session_manager import SessionManager, SessionState
from app.opcua.tags import TagSpec
from tests.integration.conftest import TEST_CERTS_DIR

SHARED_PORT = 15059


def make_config(**changes: Any) -> Config:
    base: dict[str, Any] = {
        "OPCUA_PUBLIC_HOST": "localhost",
        "OPCUA_SHARED_PORT": SHARED_PORT,
        "OPCUA_SESSION_PORT_MIN": 15060,
        "OPCUA_SESSION_PORT_MAX": 15100,
        "OPCUA_CERTS_DIR": str(TEST_CERTS_DIR),
        "SESSIONS_PER_KEY": 10,
        "SESSIONS_PER_IP": 10,
        "SESSIONS_TOTAL": 20,
        "WORKER_START_TIMEOUT_S": 30,
    }
    base.update(changes)
    return replace(Config(), **base)


@pytest.fixture
async def manager() -> AsyncIterator[SessionManager]:
    m = SessionManager(make_config())
    try:
        yield m
    finally:
        await m.close()


def tag_id(name: str) -> ua.NodeId:
    return ua.NodeId(f"Tags.{name}", 2)


def pid_alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def test_two_sessions_isolated(manager: SessionManager) -> None:
    """ТЗ: две сессии параллельно — разные порты, данные не пересекаются."""
    only_a = [TagSpec(name="OnlyA", kind="constant", params={"value": 1.0}, writable=True)]
    only_b = [TagSpec(name="OnlyB", kind="constant", params={"value": 2.0}, writable=True)]
    a, b = await asyncio.gather(
        manager.create("ka", "10.0.0.1", tags=only_a, alarms=[]),
        manager.create("kb", "10.0.0.2", tags=only_b, alarms=[]),
    )
    assert a.port != b.port
    assert a.endpoint_url.startswith("opc.tcp://localhost:")
    async with Client(a.endpoint_url) as ca, Client(b.endpoint_url) as cb:
        await ca.get_node(tag_id("OnlyA")).write_value(42.0)
        assert await ca.get_node(tag_id("OnlyA")).read_value() == 42.0
        assert await cb.get_node(tag_id("OnlyB")).read_value() == 2.0
        with pytest.raises(ua.UaStatusCodeError):
            await cb.get_node(tag_id("OnlyA")).read_value()
        with pytest.raises(ua.UaStatusCodeError):
            await ca.get_node(tag_id("OnlyB")).read_value()


async def test_parallel_create_delete_with_real_processes(manager: SessionManager) -> None:
    """ТЗ: 10 параллельных create/delete — уникальные порты, всё освобождено, процессов нет."""
    sessions = await asyncio.gather(*(manager.create(f"k{n}", "10.0.0.9", alarms=[]) for n in range(10)))
    ports = [s.port for s in sessions]
    pids = [s.handle.pid for s in sessions if s.handle is not None and s.handle.pid is not None]
    assert len(set(ports)) == 10
    assert len(pids) == 10
    assert all(s.state is SessionState.RUNNING for s in sessions)
    await asyncio.gather(*(manager.delete(s.id, s.key) for s in sessions))
    await asyncio.sleep(0.5)  # ОС дочищает записи о процессах после join
    assert manager.sessions() == []
    assert manager.allocator.in_use == frozenset()
    assert [pid for pid in pids if pid_alive(pid)] == []
    assert all(probe_bind(port) for port in ports)


async def test_cancelled_create_leaves_nothing(manager: SessionManager) -> None:
    task = asyncio.create_task(manager.create("kc", "10.0.0.3", alarms=[]))
    await asyncio.sleep(0.3)  # процесс уже стартует
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    assert manager.sessions() == []
    assert manager.allocator.in_use == frozenset()


async def test_shared_server_read_only_and_restarts_after_crash(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.opcua.session_manager.SHARED_RESTART_MIN_S", 0.2)
    await manager.start(reaper_interval=3600)
    assert manager.shared.status == "running"
    url = f"opc.tcp://localhost:{SHARED_PORT}/electroman/"
    async with Client(url) as client:
        sine = client.get_node(tag_id("Sine"))
        assert isinstance(await sine.read_value(), float)
        with pytest.raises(ua.UaStatusCodeError):
            await sine.write_value(1.0)
        with pytest.raises(ua.UaStatusCodeError):
            await client.get_node(tag_id("Setpoint")).read_value()  # writable-тега на общем нет
    handle = manager.shared.handle
    assert handle is not None and handle.pid is not None
    psutil.Process(handle.pid).kill()
    deadline = asyncio.get_running_loop().time() + 30
    while manager.shared.status != "running" or manager.shared.handle is handle:
        assert asyncio.get_running_loop().time() < deadline, "общий сервер не перезапустился"
        await asyncio.sleep(0.2)
    assert manager.shared.restarts == 1
    async with Client(url) as client:
        assert isinstance(await client.get_node(tag_id("Sine")).read_value(), float)
