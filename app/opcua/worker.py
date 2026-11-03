"""Процесс-воркер: один OPC UA сервер со своим event loop (ADR-001).

Точка входа `run_worker` вызывается в дочернем процессе (`multiprocessing`,
контекст spawn), поэтому всё, что ей передаётся, должно сериализоваться pickle:
параметры — dataclass `WorkerParams`, теги и алармы — dict'ы (model_dump).

IPC (ADR-001 п. 4):
- `commands` (родитель → воркер): `{"id", "cmd", ...}`; на каждую команду —
  ответ `{"type": "reply", "id", "ok", "error", "result"}` в control;
- `control` (воркер → родитель, без лимита): `ready`, `reply`, `write`, `alarm`,
  `client_count`, `stats`, `error`, `stopped` — терять нельзя;
- `telemetry` (воркер → родитель, ограниченная): `tag_values`, `log` —
  drop-newest при переполнении, счётчик дропов уходит в `stats`.

Воркер завершается сам, если родительский процесс умер (сирота не держит порт).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import multiprocessing.queues
import os
import queue
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.logging_setup import configure_logging
from app.opcua.address_space import AddressSpace, client_count, create_server
from app.opcua.security import ServerSecurity
from app.opcua.tags import AlarmSpec, TagSpec

logger = logging.getLogger(__name__)

#: Как часто воркер проверяет живость родителя и число клиентов, с.
WATCH_INTERVAL_S = 1.0
#: Как часто отправляется `stats` (если счётчики изменились), с.
STATS_INTERVAL_S = 5.0


class PutQueue(Protocol):
    """Минимум очереди, нужный воркеру (multiprocessing.Queue и queue.Queue)."""

    def put(self, item: Any) -> None: ...

    def put_nowait(self, item: Any) -> None: ...


class GetQueue(Protocol):
    def get(self, block: bool = True, timeout: float | None = None) -> Any: ...


@dataclass(frozen=True)
class WorkerParams:
    """Всё, что нужно воркеру для запуска сервера (передаётся в дочерний процесс)."""

    port: int
    endpoint_url: str
    #: Шифрование и демо-учётки (ADR-004); в воркере всегда одни и те же.
    security: ServerSecurity
    bind_host: str | None = None
    server_name: str = "OPC UA SendBox"
    history_size: int = 1000
    read_only: bool = False
    state_hz: float = 2.0
    tags: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    alarms: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class TelemetrySender:
    """Отправка телеметрии с drop-newest (ADR-001 п. 5).

    `put_nowait`; при переполнении сообщение выбрасывается и считается. Каждый
    снимок полный, следующий заменяет потерянный.
    """

    def __init__(self, target: PutQueue) -> None:
        self._target = target
        self.dropped = 0

    def send(self, message: dict[str, Any]) -> bool:
        try:
            self._target.put_nowait(message)
        except queue.Full:
            self.dropped += 1
            return False
        return True


class _TelemetryLogHandler(logging.Handler):
    """Записи журнала воркера (WARNING и выше) — в телеметрию, для live-лога UI."""

    def __init__(self, sender: TelemetrySender) -> None:
        super().__init__(level=logging.WARNING)
        self._sender = sender

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self._sender.send(
                {"type": "log", "level": record.levelname, "message": record.getMessage()[:500]}
            )


def run_worker(
    params: WorkerParams,
    commands: GetQueue,
    control: PutQueue,
    telemetry: PutQueue,
) -> None:
    """Точка входа процесса: поднять сервер и обслуживать команды до `stop`.

    Ошибка запуска уходит в control как `{"type": "error", "fatal": True}`;
    код возврата процесса — 1.
    """
    # env наследуется от родителя: тот же формат логов (text/json), уровень — WARNING,
    # чтобы INFO asyncua про каждую сессию клиента не засоряли журнал сервиса.
    with contextlib.suppress(ValueError):
        configure_logging(os.environ.get("LOG_FORMAT", "text"), "warning")
    try:
        asyncio.run(_serve(params, commands, control, telemetry))
    except Exception as exc:
        control.put(
            {
                "type": "error",
                "fatal": True,
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8),
            }
        )
        raise SystemExit(1) from exc
    finally:
        # Дождаться отправки control-сообщений (feeder-поток multiprocessing.Queue).
        for q in (control, telemetry):
            if isinstance(q, multiprocessing.queues.Queue):
                q.close()
                if q is telemetry:
                    q.cancel_join_thread()  # родитель может уже не читать телеметрию
                else:
                    q.join_thread()


async def _serve(
    params: WorkerParams,
    commands: GetQueue,
    control: PutQueue,
    telemetry: PutQueue,
) -> None:
    sender = TelemetrySender(telemetry)
    logging.getLogger().addHandler(_TelemetryLogHandler(sender))
    server = await create_server(
        endpoint_url=params.endpoint_url,
        port=params.port,
        bind_host=params.bind_host,
        server_name=params.server_name,
        security=params.security,
    )
    space = AddressSpace(
        server,
        history_size=params.history_size,
        read_only=params.read_only,
        on_event=control.put,
    )
    await space.setup()
    for tag in params.tags:
        await space.add_tag(TagSpec.model_validate(tag))
    for alarm in params.alarms:
        await space.add_alarm(AlarmSpec.model_validate(alarm))
    async with server:
        space.start()
        control.put({"type": "ready", "port": params.port})
        background = [
            asyncio.create_task(_telemetry_loop(space, sender, params.state_hz)),
            asyncio.create_task(_watch_loop(control, sender)),
        ]
        try:
            await _command_loop(space, commands, control, read_only=params.read_only)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await space.stop()
    control.put({"type": "stopped"})


async def _command_loop(
    space: AddressSpace, commands: GetQueue, control: PutQueue, *, read_only: bool
) -> None:
    """Команды родителя по одной; `stop` — выход. Ответ на каждую команду с id."""
    while True:
        message: dict[str, Any] = await asyncio.to_thread(commands.get)
        cmd = message.get("cmd")
        if cmd == "stop":
            control.put({"type": "reply", "id": message.get("id"), "ok": True, "result": None, "error": None})
            return
        try:
            result = await _execute(space, cmd, message, read_only=read_only)
        except (ValueError, KeyError, TypeError) as exc:
            reply = {"type": "reply", "id": message.get("id"), "ok": False, "result": None, "error": str(exc)}
        except Exception as exc:
            logger.exception("команда %s", cmd)
            reply = {
                "type": "reply",
                "id": message.get("id"),
                "ok": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            reply = {"type": "reply", "id": message.get("id"), "ok": True, "result": result, "error": None}
        control.put(reply)


async def _execute(space: AddressSpace, cmd: Any, message: dict[str, Any], *, read_only: bool) -> Any:
    if cmd in ("add_tag", "remove_tag", "add_alarm", "remove_alarm") and read_only:
        raise ValueError("общий сервер только для чтения")
    if cmd == "add_tag":
        await space.add_tag(TagSpec.model_validate(message["spec"]))
        return None
    if cmd == "remove_tag":
        await space.remove_tag(str(message["name"]))
        return None
    if cmd == "add_alarm":
        await space.add_alarm(AlarmSpec.model_validate(message["spec"]))
        return None
    if cmd == "remove_alarm":
        await space.remove_alarm(str(message["name"]))
        return None
    if cmd == "trigger_alarm":
        if read_only:
            raise ValueError("общий сервер только для чтения")
        active = message.get("active")
        return await space.trigger_alarm(str(message["name"]), None if active is None else bool(active))
    if cmd == "describe":
        return {
            "tags": [t.model_dump() for t in space.tags],
            "alarms": [a.model_dump() for a in space.alarms],
            "values": space.snapshot(),
            "alarm_states": space.alarm_states(),
            "clients": client_count(),
        }
    if cmd == "ping":
        return "pong"
    raise ValueError(f"неизвестная команда {cmd!r}")


async def _telemetry_loop(space: AddressSpace, sender: TelemetrySender, state_hz: float) -> None:
    """Снимок всех тегов не чаще state_hz: поток ограничен независимо от числа тегов."""
    period = 1.0 / state_hz
    while True:
        await asyncio.sleep(period)
        sender.send(
            {
                "type": "tag_values",
                "t": time.time(),
                "values": space.snapshot(),
                "alarms": space.alarm_states(),
            }
        )


async def _watch_loop(control: PutQueue, sender: TelemetrySender) -> None:
    """Живость родителя, число клиентов (при изменении), счётчик дропов (stats)."""
    parent = multiprocessing.parent_process()
    last_clients = -1
    last_dropped = 0
    last_stats = time.monotonic()
    while True:
        await asyncio.sleep(WATCH_INTERVAL_S)
        if parent is not None and not parent.is_alive():
            # Родителя, который ждал бы ответа, уже нет: выходим сразу, освобождая порт.
            os._exit(0)
        clients = client_count()
        if clients != last_clients:
            control.put({"type": "client_count", "clients": clients})
            last_clients = clients
        now = time.monotonic()
        if sender.dropped != last_dropped and now - last_stats >= STATS_INTERVAL_S:
            control.put({"type": "stats", "telemetry_dropped": sender.dropped})
            last_dropped = sender.dropped
            last_stats = now
