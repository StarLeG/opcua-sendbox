"""Поддельный воркер и окружение SessionManager для быстрых тестов без процессов."""
from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import replace
from typing import Any

from app.config import Config
from app.opcua.port_allocator import PortAllocator
from app.opcua.session_manager import SessionManager
from app.opcua.worker import WorkerParams
from app.opcua.worker_handle import MessageCallback, WorkerCommandError, WorkerStartError

_pids = itertools.count(1000)


class FakeHandle:
    """Подделка WorkerHandle: команды как у настоящего воркера, управляемые сбои."""

    def __init__(self, params: WorkerParams, on_message: MessageCallback, *, delay: float, fail: bool) -> None:
        self.params = params
        self.on_message = on_message
        self.delay = delay
        self.fail = fail
        self.alive = False
        self.stopped = False
        self.killed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.tags: dict[str, dict[str, Any]] = {t["name"]: t for t in params.tags}
        self.alarms: dict[str, dict[str, Any]] = {a["name"]: a for a in params.alarms}
        self.alarm_states: dict[str, bool] = {name: False for name in self.alarms}
        self._pid = next(_pids)

    @property
    def pid(self) -> int | None:
        return self._pid

    def is_alive(self) -> bool:
        return self.alive

    async def start(self, timeout: float = 10.0) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise WorkerStartError("порт занят")
        self.alive = True

    async def request(self, cmd: str, timeout: float = 10.0, **fields: Any) -> Any:
        self.requests.append((cmd, fields))
        if cmd == "describe":
            return {
                "tags": list(self.tags.values()),
                "alarms": list(self.alarms.values()),
                "values": {name: 0.0 for name in self.tags},
                "alarm_states": dict(self.alarm_states),
                "clients": 0,
            }
        if cmd == "add_tag":
            self._add(self.tags, fields["spec"], "тег")
            return None
        if cmd == "add_alarm":
            self._add(self.alarms, fields["spec"], "аларм")
            self.alarm_states[fields["spec"]["name"]] = False
            return None
        if cmd == "remove_tag":
            if any(a.get("tag") == fields["name"] for a in self.alarms.values()):
                raise WorkerCommandError(f"тег {fields['name']!r} используют алармы")
            self._remove(self.tags, fields["name"], "тега")
            return None
        if cmd == "remove_alarm":
            self._remove(self.alarms, fields["name"], "аларма")
            return None
        if cmd == "trigger_alarm":
            name = fields["name"]
            if name not in self.alarms:
                raise WorkerCommandError(f"аларма {name!r} нет")
            active = fields.get("active")
            self.alarm_states[name] = (not self.alarm_states[name]) if active is None else bool(active)
            return self.alarm_states[name]
        return "ok"

    @staticmethod
    def _add(store: dict[str, dict[str, Any]], spec: dict[str, Any], what: str) -> None:
        if spec["name"] in store:
            raise WorkerCommandError(f"{what} {spec['name']!r} уже есть")
        store[spec["name"]] = spec

    @staticmethod
    def _remove(store: dict[str, dict[str, Any]], name: str, what: str) -> None:
        if name not in store:
            raise WorkerCommandError(f"{what} {name!r} нет")
        del store[name]

    async def stop(self, timeout: float = 5.0) -> None:
        self.alive = False
        self.stopped = True

    async def kill(self) -> None:
        self.alive = False
        self.killed = True

    def crash(self) -> None:
        self.alive = False
        self.on_message({"type": "exited", "exitcode": -9, "expected": False})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Env:
    """SessionManager с поддельными воркерами, часами и пробой портов."""

    def __init__(self, **config_changes: Any) -> None:
        defaults: dict[str, Any] = {
            "OPCUA_SHARED_PORT": 100,
            "OPCUA_SESSION_PORT_MIN": 101,
            "OPCUA_SESSION_PORT_MAX": 120,
            "SESSIONS_PER_KEY": 2,
            "SESSIONS_PER_IP": 4,
            "SESSIONS_TOTAL": 10,
            "SESSION_IDLE_TIMEOUT_S": 60,
            "SESSION_MAX_LIFETIME_S": 600,
        }
        defaults.update(config_changes)
        self.config = replace(Config(), **defaults)
        self.clock = Clock()
        self.handles: list[FakeHandle] = []
        self.delay = 0.0
        self.fail = False
        self.random_delay = False
        allocator = PortAllocator(
            self.config.OPCUA_SESSION_PORT_MIN, self.config.OPCUA_SESSION_PORT_MAX, probe=lambda _p: True
        )
        self.manager = SessionManager(self.config, allocator=allocator, handle_factory=self.factory, clock=self.clock)

    def factory(self, params: WorkerParams, on_message: MessageCallback) -> FakeHandle:
        delay = random.uniform(0, 0.02) if self.random_delay else self.delay
        handle = FakeHandle(params, on_message, delay=delay, fail=self.fail)
        self.handles.append(handle)
        return handle
