"""SessionManager на поддельном воркере: лимиты, гонки, откаты, reaper, общий сервер."""
from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from app.opcua.session_manager import (
    NoCapacityError,
    SessionLimitError,
    SessionNotFoundError,
    SessionState,
)
from app.opcua.tags import TagSpec
from tests.fakes import Env


@pytest.fixture
def env() -> Env:
    return Env()


async def test_create_returns_running_session_with_demo_set(env: Env) -> None:
    session = await env.manager.create("k1", "1.1.1.1")
    assert session.state is SessionState.RUNNING
    assert 101 <= session.port <= 120
    assert session.endpoint_url == f"opc.tcp://opcua-sendbox.electroman.by:{session.port}/electroman/"
    params = env.handles[0].params
    assert {t["name"] for t in params.tags} >= {"Sine", "Setpoint"}
    assert {a["name"] for a in params.alarms} == {"SineHigh", "Heartbeat"}
    assert env.manager.get(session.id, "k1") is session


async def test_limits_per_key_ip_total() -> None:
    env = Env(SESSIONS_PER_KEY=2, SESSIONS_PER_IP=3, SESSIONS_TOTAL=4)
    m = env.manager
    await m.create("a", "ip1")
    await m.create("a", "ip1")
    with pytest.raises(SessionLimitError) as by_key:
        await m.create("a", "ip2")
    assert by_key.value.scope == "key"
    await m.create("b", "ip1")
    with pytest.raises(SessionLimitError) as by_ip:
        await m.create("c", "ip1")
    assert by_ip.value.scope == "ip"
    await m.create("c", "ip2")
    with pytest.raises(SessionLimitError) as total:
        await m.create("d", "ip3")
    assert total.value.scope == "total"


async def test_content_checked_before_port_taken(env: Env) -> None:
    tags = [TagSpec(name=f"T{i}") for i in range(env.config.TAGS_PER_SESSION + 1)]
    with pytest.raises(ValueError, match="лимита"):
        await env.manager.create("k", "ip", tags=tags)
    assert env.manager.allocator.in_use == frozenset()
    assert env.handles == []


async def test_ports_exhausted() -> None:
    env = Env(OPCUA_SESSION_PORT_MIN=101, OPCUA_SESSION_PORT_MAX=101, SESSIONS_PER_KEY=4, SESSIONS_PER_IP=4)
    await env.manager.create("k", "ip")
    with pytest.raises(NoCapacityError, match="портов"):
        await env.manager.create("k", "ip")
    assert len(env.manager.sessions()) == 1


async def test_start_failure_rolls_back(env: Env) -> None:
    env.fail = True
    with pytest.raises(NoCapacityError, match="не запустился"):
        await env.manager.create("k", "ip")
    assert env.manager.sessions() == []
    assert env.manager.allocator.in_use == frozenset()
    assert env.handles[0].killed


async def test_cancel_during_start_rolls_back(env: Env) -> None:
    """Инвариант 3: отмена запроса посреди запуска не оставляет сессию в starting."""
    env.delay = 1.0
    task = asyncio.create_task(env.manager.create("k", "ip"))
    await asyncio.sleep(0.05)
    assert [s.state for s in env.manager.sessions()] == [SessionState.STARTING]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.manager.sessions() == []
    assert env.manager.allocator.in_use == frozenset()
    assert env.handles[0].killed


async def test_delete_during_start(env: Env) -> None:
    env.delay = 0.2
    task = asyncio.create_task(env.manager.create("k", "ip"))
    await asyncio.sleep(0.05)
    [starting] = env.manager.sessions()
    await env.manager.delete(starting.id, "k")
    with pytest.raises(SessionNotFoundError, match="во время запуска"):
        await task
    assert env.manager.sessions() == []
    assert env.manager.allocator.in_use == frozenset()
    assert env.handles[0].killed


async def test_delete_running_and_ownership(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    events: list[dict[str, Any]] = []
    session.listeners.add(events.append)
    with pytest.raises(SessionNotFoundError):
        env.manager.get(session.id, "чужой")
    with pytest.raises(SessionNotFoundError):
        await env.manager.delete(session.id, "чужой")
    await env.manager.delete(session.id, "k")
    assert env.handles[0].stopped
    assert env.manager.allocator.in_use == frozenset()
    assert events == [{"type": "closed", "reason": "deleted"}]
    with pytest.raises(SessionNotFoundError):
        await env.manager.delete(session.id, "k")


async def test_request_forwards_to_worker_and_touches(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    env.clock.now += 30
    assert await env.manager.request(session.id, "k", "trigger_alarm", name="Heartbeat") is True
    assert env.handles[0].requests == [("trigger_alarm", {"name": "Heartbeat"})]
    assert session.last_activity == env.clock.now


async def test_parallel_create_delete_no_races() -> None:
    """ТЗ: 10 параллельных задач create/delete — порты уникальны, всё освобождено."""
    env = Env(SESSIONS_PER_KEY=10, SESSIONS_PER_IP=10, SESSIONS_TOTAL=10)
    env.random_delay = True
    m = env.manager

    async def cycle(n: int) -> int:
        session = await m.create(f"key{n}", "ip")
        port = session.port
        await asyncio.sleep(random.uniform(0, 0.02))
        await m.delete(session.id, f"key{n}")
        return port

    first = await asyncio.gather(*(m.create(f"key{n}", "ip") for n in range(10)))
    assert len({s.port for s in first}) == 10
    await asyncio.gather(*(m.delete(s.id, s.key) for s in first))
    ports = await asyncio.gather(*(cycle(n) for n in range(10)))
    assert len(ports) == 10
    assert m.sessions() == []
    assert m.allocator.in_use == frozenset()
    assert all(not h.alive for h in env.handles)


async def test_reaper_idle_respects_connected_clients(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    env.handles[0].on_message({"type": "client_count", "clients": 1})
    env.clock.now += 120  # дольше idle-таймаута, но клиент подключён
    assert await env.manager.reap_once() == []
    env.handles[0].on_message({"type": "client_count", "clients": 0})  # таймер заново
    env.clock.now += 59
    assert await env.manager.reap_once() == []
    env.clock.now += 2
    assert await env.manager.reap_once() == [(session.id, "idle")]
    assert env.handles[0].stopped
    assert env.manager.allocator.in_use == frozenset()


async def test_reaper_lifetime_even_with_clients(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    env.handles[0].on_message({"type": "client_count", "clients": 3})
    env.clock.now += 600
    assert await env.manager.reap_once() == [(session.id, "lifetime")]


async def test_api_activity_postpones_idle(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    env.clock.now += 50
    env.manager.get(session.id, "k")
    env.clock.now += 50
    assert await env.manager.reap_once() == []


async def test_worker_crash_cleans_up(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    events: list[dict[str, Any]] = []
    session.listeners.add(events.append)
    env.handles[0].crash()
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert env.manager.sessions() == []
    assert env.manager.allocator.in_use == frozenset()
    types = [e["type"] for e in events]
    assert "crashed" in types and {"type": "closed", "reason": "crashed"} in events


async def test_listener_error_does_not_break_others(env: Env) -> None:
    session = await env.manager.create("k", "ip")
    got: list[dict[str, Any]] = []

    def broken(_message: dict[str, Any]) -> None:
        raise RuntimeError("WS упал")

    session.listeners.update({broken, got.append})
    env.handles[0].on_message({"type": "tag_values", "values": {}})
    assert got == [{"type": "tag_values", "values": {}}]


async def test_shared_server_read_only_and_restart(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    monkeypatch.setattr("app.opcua.session_manager.SHARED_RESTART_MIN_S", 0.01)
    await env.manager.start(reaper_interval=3600)
    shared_handle = env.handles[0]
    assert env.manager.shared.status == "running"
    assert shared_handle.params.read_only is True
    assert shared_handle.params.port == 100
    assert not any(t.get("writable") for t in shared_handle.params.tags)
    shared_handle.crash()
    assert env.manager.shared.status == "restarting"
    for _ in range(50):
        await asyncio.sleep(0.01)
        if env.manager.shared.status == "running":
            break
    assert env.manager.shared.status == "running"
    assert env.manager.shared.restarts == 1
    await env.manager.close()
    assert env.manager.shared.status == "down"


async def test_close_stops_everything(env: Env) -> None:
    await env.manager.start(reaper_interval=3600)
    await env.manager.create("k", "ip")
    await env.manager.close()
    assert env.manager.sessions() == []
    assert all(not h.alive for h in env.handles)
    assert env.manager.allocator.in_use == frozenset()
    with pytest.raises(NoCapacityError):
        await env.manager.create("k", "ip")


async def test_reaper_forgets_inactive_keys(env: Env) -> None:
    info = env.manager.keys.issue("ip")
    env.clock.now += 61
    await env.manager.reap_once()
    assert env.manager.keys.check(info.key) is None
