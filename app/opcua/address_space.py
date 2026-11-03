"""Адресное пространство сессии: сервер asyncua, теги, история, алармы (ADR-002).

Модуль работает внутри процесса воркера и ничего не знает об IPC: наружу он
сообщает через колбэк `on_event` (записи клиентов, переходы алармов), а
снимок значений отдаёт `snapshot()`.

Решения ADR-002, которые здесь реализованы:
- endpoint объявляет публичный хост, сервер слушает все интерфейсы
  (`socket_address`, `set_match_discovery_client_ip(False)`);
- `SendBoxHistory` подставляет SourceTimestamp, если клиент его не передал;
- запись клиента в writable-тег получает SourceTimestamp ещё до применения (PreWrite);
- событие аларма рассылается снимками на папку SendBox и на Server (`emit_event`);
- поля условия (ConditionId, Retain, *State/Id) выставляет `AlarmEvaluator` через
  `_fill_condition`; ConditionRefresh включён (`bind_condition_methods`).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any, Literal, cast

from asyncua import Node, Server, ua
from asyncua.common.callback import CallbackType, ServerItemCallback
from asyncua.server.event_generator import EventGenerator
from asyncua.server.history import HistoryDict
from asyncua.server.internal_session import InternalSession

from app.opcua.generators import ValueGenerator, create_generator
from app.opcua.tags import AlarmSpec, DataType, TagSpec, coerce_value

logger = logging.getLogger(__name__)

NAMESPACE_URI = "urn:electroman:opcua-sendbox"
APPLICATION_URI = "urn:electroman:opcua-sendbox:server"

VARIANT_TYPES: dict[DataType, ua.VariantType] = {
    "Double": ua.VariantType.Double,
    "Float": ua.VariantType.Float,
    "Int16": ua.VariantType.Int16,
    "Int32": ua.VariantType.Int32,
    "UInt16": ua.VariantType.UInt16,
    "UInt32": ua.VariantType.UInt32,
    "Boolean": ua.VariantType.Boolean,
}

#: Колбэк событий наружу (control-сообщения воркера): dict с ключом "type".
EventCallback = Callable[[dict[str, Any]], None]


def _utcnow() -> ua.DateTime:
    """Текущее UTC в типе asyncua (DateTime — обёртка над datetime для аннотаций)."""
    return cast(ua.DateTime, datetime.now(UTC))


def _node_id(identifier: str | int, namespace: int = 0) -> ua.NodeId:
    """NodeId из обычных str/int: аннотации asyncua требуют её обёрток Int32/String/Int16."""
    return ua.NodeId(identifier, namespace)  # type: ignore[arg-type]


def _project_version() -> str:
    try:
        return package_version("opcua-sendbox")
    except PackageNotFoundError:  # pragma: no cover - запуск без установки пакета
        return "0.0.0"


# --- история -----------------------------------------------------------------


class SendBoxHistory(HistoryDict):
    """HistoryDict, устойчивый к DataValue без SourceTimestamp (ADR-002 п. 2).

    Подставляет ServerTimestamp, иначе текущее UTC. Метку предыдущего значения
    не копирует: иначе история выглядела бы так, будто значение не менялось.
    """

    async def save_node_value(self, node_id: ua.NodeId, datavalue: ua.DataValue) -> None:
        if datavalue.SourceTimestamp is None:
            datavalue = copy.copy(datavalue)
            datavalue.SourceTimestamp = datavalue.ServerTimestamp or _utcnow()
        await super().save_node_value(node_id, datavalue)


# --- логика алармов ---------------------------------------------------------------


@dataclass(frozen=True)
class AlarmTransition:
    """Новое состояние аларма, которое надо отправить событием."""

    active: bool
    #: Для limit: какой порог нарушен ("High"/"Low"), None — норма или не limit.
    limit: Literal["High", "Low"] | None = None
    #: Значение тега, вызвавшее переход (limit).
    value: float | None = None


class AlarmEvaluator:
    """Состояние одного аларма без asyncua: пороги, гистерезис, период, ручной вызов.

    State machine `ExclusiveLimitAlarmType` в asyncua не реализована (ADR-002 п. 3),
    поэтому вся логика здесь. Методы возвращают AlarmTransition, если событие
    нужно отправить, иначе None.
    """

    def __init__(self, spec: AlarmSpec) -> None:
        self.spec = spec
        self.active = False
        self.limit: Literal["High", "Low"] | None = None

    def on_value(self, value: float) -> AlarmTransition | None:
        """Новое значение тега для аларма limit.

        Активация: value > high (High) или value < low (Low). Сброс с гистерезисом:
        High — когда value <= high - deadband, Low — когда value >= low + deadband.
        Прямой переход High ↔ Low — событие с новым порогом.
        """
        if self.spec.kind != "limit":
            raise ValueError(f"аларм {self.spec.name!r} не limit")
        if math.isnan(value):
            return None
        high, low, deadband = self.spec.high, self.spec.low, self.spec.deadband
        new_limit: Literal["High", "Low"] | None
        if high is not None and value > high:
            new_limit = "High"
        elif low is not None and value < low:
            new_limit = "Low"
        elif self.limit == "High" and high is not None and value > high - deadband:
            new_limit = "High"  # ещё в зоне гистерезиса
        elif self.limit == "Low" and low is not None and value < low + deadband:
            new_limit = "Low"
        else:
            new_limit = None
        if new_limit == self.limit:
            return None
        self.limit = new_limit
        self.active = new_limit is not None
        return AlarmTransition(active=self.active, limit=new_limit, value=value)

    def on_time(self, now: float) -> AlarmTransition | None:
        """Время для periodic: нечётные периоды — активен, чётные — норма."""
        if self.spec.kind != "periodic":
            raise ValueError(f"аларм {self.spec.name!r} не periodic")
        active = math.floor(now / self.spec.period_s) % 2 == 1
        if active == self.active:
            return None
        self.active = active
        return AlarmTransition(active=active)

    def on_trigger(self, active: bool | None = None) -> AlarmTransition:
        """Ручной вызов: явное состояние или переключение (None). Событие уходит всегда."""
        self.active = (not self.active) if active is None else active
        if not self.active:
            self.limit = None
        return AlarmTransition(active=self.active, limit=self.limit)


# --- рассылка событий ---------------------------------------------------------------


async def emit_event(server: Server, evgen: EventGenerator, notifiers: list[ua.NodeId], message: str) -> None:
    """Одно событие (один EventId) — на каждый узел-нотификатор из списка (ADR-002 п. 3b).

    asyncua доставляет событие только мониторам ровно на emitting_node и не
    поднимает его по HasNotifier к Server. Каждому нотификатору уходит снимок:
    SubscriptionService хранит для ConditionRefresh ссылку на последнее событие
    условия. Server идёт последним, чтобы refresh адресовался его подписчикам.
    """
    await evgen.trigger(message=message)
    service = server.iserver.isession.subscription_service
    server_id = _node_id(ua.ObjectIds.Server)
    ordered = [n for n in notifiers if n != server_id] + [n for n in notifiers if n == server_id]
    for node_id in ordered:
        if node_id == evgen.event.emitting_node and node_id != server_id:
            continue  # trigger() уже доставил
        snapshot = copy.copy(evgen.event)
        snapshot.emitting_node = node_id
        await service.trigger_event(snapshot)


# --- сервер ----------------------------------------------------------------------------


async def create_server(
    *,
    endpoint_url: str,
    port: int,
    bind_host: str | None,
    server_name: str = "OPC UA SendBox",
) -> Server:
    """Сервер asyncua с настройками ADR-002 (ещё не запущен, адресное пространство пустое)."""
    server = Server()
    # ConditionRefresh и события RefreshStart/End — только до init() (ADR-002 п. 3b).
    server.iserver.bind_condition_methods = True
    await server.init()
    server.iserver.history_manager.set_storage(SendBoxHistory())
    server.set_endpoint(endpoint_url)
    # host=None: все интерфейсы, IPv4 и IPv6 (ADR-002 п. 1). Аннотация asyncua — str.
    server.socket_address = (bind_host, port)  # type: ignore[assignment]
    server.set_match_discovery_client_ip(False)
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    server.set_identity_tokens([ua.AnonymousIdentityToken])
    server.set_server_name(server_name)
    await server.set_application_uri(APPLICATION_URI)
    version = _project_version()
    await server.set_build_info(
        product_uri="https://opcua-sendbox.electroman.by",
        manufacturer_name="Electroman",
        product_name=server_name,
        software_version=version,
        build_number=version,
        build_date=_utcnow(),
    )
    return server


def client_count() -> int:
    """Активные OPC UA сессии процесса (ADR-002 п. 4; приватный API asyncua).

    Счётчик — переменная класса, общая для процесса; в воркере ровно один сервер.
    """
    return int(InternalSession._current_connections)


# --- адресное пространство ---------------------------------------------------------------


@dataclass
class _TagRuntime:
    spec: TagSpec
    node: Node
    generator: ValueGenerator
    value: int | float | bool
    task: asyncio.Task[None] | None = None


@dataclass
class _AlarmRuntime:
    spec: AlarmSpec
    evaluator: AlarmEvaluator
    evgen: EventGenerator
    node: Node
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None


class AddressSpace:
    """Узлы песочницы на сервере: `Objects/SendBox/{Tags,Alarms}`.

    Порядок: `setup()` до `server.start()`, затем теги и алармы — в любое время;
    `start()` запускает генераторы, `stop()` их останавливает.
    """

    def __init__(
        self,
        server: Server,
        *,
        history_size: int,
        read_only: bool = False,
        on_event: EventCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._server = server
        self._history_size = history_size
        self._read_only = read_only
        self._on_event = on_event
        self._clock = clock
        self._t0 = clock()
        self._running = False
        self._tags: dict[str, _TagRuntime] = {}
        self._alarms: dict[str, _AlarmRuntime] = {}
        self._writable_nodes: dict[ua.NodeId, str] = {}
        self.ns = 0
        self._sendbox: Node | None = None
        self._tags_folder: Node | None = None
        self._alarms_folder: Node | None = None

    # --- построение ---------------------------------------------------------------

    async def setup(self) -> None:
        """Пространство имён, папки, нотификатор событий, хуки записи."""
        self.ns = await self._server.register_namespace(NAMESPACE_URI)
        objects = self._server.nodes.objects
        self._sendbox = await objects.add_folder(_node_id("SendBox", self.ns), "SendBox")
        await self._sendbox.set_event_notifier([ua.EventNotifier.SubscribeToEvents])
        await self._server.nodes.server.add_reference(self._sendbox.nodeid, ua.ObjectIds.HasNotifier)
        self._tags_folder = await self._sendbox.add_folder(_node_id("Tags", self.ns), "Tags")
        self._alarms_folder = await self._sendbox.add_folder(_node_id("Alarms", self.ns), "Alarms")
        self._server.subscribe_server_callback(CallbackType.PreWrite, self._pre_write)
        self._server.subscribe_server_callback(CallbackType.PostWrite, self._post_write)

    def tag_node_id(self, name: str) -> ua.NodeId:
        return _node_id(f"Tags.{name}", self.ns)

    def alarm_node_id(self, name: str) -> ua.NodeId:
        return _node_id(f"Alarms.{name}", self.ns)

    @property
    def notifiers(self) -> list[ua.NodeId]:
        assert self._sendbox is not None, "setup() не вызван"
        return [self._sendbox.nodeid, _node_id(ua.ObjectIds.Server)]

    @property
    def tags(self) -> list[TagSpec]:
        return [rt.spec for rt in self._tags.values()]

    @property
    def alarms(self) -> list[AlarmSpec]:
        return [rt.spec for rt in self._alarms.values()]

    async def add_tag(self, spec: TagSpec) -> None:
        """Добавить тег; ValueError, если имя занято."""
        assert self._tags_folder is not None, "setup() не вызван"
        if spec.name in self._tags:
            raise ValueError(f"тег {spec.name!r} уже есть")
        boolean = spec.data_type == "Boolean"
        generator = create_generator(spec.kind, spec.params, boolean=boolean)
        value = coerce_value(generator.update(0.0), spec.data_type)
        node = await self._tags_folder.add_variable(
            self.tag_node_id(spec.name),
            spec.name,
            ua.Variant(value, VARIANT_TYPES[spec.data_type]),
        )
        if spec.description:
            await node.write_attribute(
                ua.AttributeIds.Description, ua.DataValue(ua.Variant(ua.LocalizedText(spec.description)))
            )
        if spec.writable and not self._read_only:
            await node.set_writable()
            self._writable_nodes[node.nodeid] = spec.name
        if spec.historize:
            # Глубину задаёт count; period — дефолт asyncua (7 суток), он больше любой сессии.
            await self._server.historize_node_data_change(node, count=self._history_size)
        runtime = _TagRuntime(spec=spec, node=node, generator=generator, value=value)
        self._tags[spec.name] = runtime
        await self._write(runtime, value)
        if self._running:
            self._start_tag(runtime)

    async def remove_tag(self, name: str) -> None:
        """Удалить тег; ValueError, если его нет или на него ссылается аларм."""
        runtime = self._tags.get(name)
        if runtime is None:
            raise ValueError(f"тега {name!r} нет")
        users = [a.spec.name for a in self._alarms.values() if a.spec.tag == name]
        if users:
            raise ValueError(f"тег {name!r} используют алармы {users} — сначала удалите их")
        await self._cancel(runtime.task)
        del self._tags[name]
        self._writable_nodes.pop(runtime.node.nodeid, None)
        if runtime.spec.historize:
            await self._server.dehistorize_node_data_change(runtime.node)
        await self._server.delete_nodes([runtime.node])

    async def add_alarm(self, spec: AlarmSpec) -> None:
        """Добавить аларм; ValueError, если имя занято или тега нет."""
        assert self._alarms_folder is not None and self._sendbox is not None, "setup() не вызван"
        if spec.name in self._alarms:
            raise ValueError(f"аларм {spec.name!r} уже есть")
        tag = self._tags.get(spec.tag) if spec.tag else None
        if spec.tag is not None and tag is None:
            raise ValueError(f"аларм {spec.name!r}: тега {spec.tag!r} нет")
        node = await self._alarms_folder.add_object(self.alarm_node_id(spec.name), spec.name)
        etype = ua.ObjectIds.ExclusiveLimitAlarmType if spec.kind == "limit" else ua.ObjectIds.AlarmConditionType
        evgen = await self._server.get_event_generator(etype, self._sendbox.nodeid)
        runtime = _AlarmRuntime(spec=spec, evaluator=AlarmEvaluator(spec), evgen=evgen, node=node)
        source = tag.node if tag is not None else node
        evgen.event.SourceNode = source.nodeid
        evgen.event.SourceName = spec.tag or spec.name
        self._alarms[spec.name] = runtime
        if spec.kind == "limit" and tag is not None:
            await self._evaluate_limit(runtime, float(tag.value))
        if self._running:
            self._start_alarm(runtime)

    async def remove_alarm(self, name: str) -> None:
        """Удалить аларм; активный перед удалением сбрасывается событием."""
        runtime = self._alarms.get(name)
        if runtime is None:
            raise ValueError(f"аларма {name!r} нет")
        await self._cancel(runtime.task)
        if runtime.evaluator.active:
            await self._emit(runtime, runtime.evaluator.on_trigger(False))
        del self._alarms[name]
        await self._server.delete_nodes([runtime.node])

    async def trigger_alarm(self, name: str, active: bool | None = None) -> bool:
        """Ручной вызов аларма (любого вида); вернуть новое состояние."""
        runtime = self._alarms.get(name)
        if runtime is None:
            raise ValueError(f"аларма {name!r} нет")
        transition = runtime.evaluator.on_trigger(active)
        await self._emit(runtime, transition)
        return transition.active

    # --- работа -------------------------------------------------------------------

    def start(self) -> None:
        """Запустить задачи обновления тегов и периодических алармов."""
        self._running = True
        self._t0 = self._clock()
        for tag in self._tags.values():
            self._start_tag(tag)
        for alarm in self._alarms.values():
            self._start_alarm(alarm)

    async def stop(self) -> None:
        self._running = False
        for tag in self._tags.values():
            await self._cancel(tag.task)
            tag.task = None
        for alarm in self._alarms.values():
            await self._cancel(alarm.task)
            alarm.task = None

    def snapshot(self) -> dict[str, int | float | bool]:
        """Текущие значения всех тегов (для телеметрии)."""
        return {name: rt.value for name, rt in self._tags.items()}

    def alarm_states(self) -> dict[str, bool]:
        return {name: rt.evaluator.active for name, rt in self._alarms.items()}

    def elapsed(self) -> float:
        return self._clock() - self._t0

    async def update_tag(self, name: str) -> None:
        """Один шаг генератора тега (используется циклом и тестами)."""
        runtime = self._tags[name]
        try:
            raw = runtime.generator.update(self.elapsed())
            value = coerce_value(raw, runtime.spec.data_type)
        except Exception:  # ошибка генератора не роняет сервер: держим прежнее значение
            logger.exception("тег %s: ошибка генератора", name)
            return
        await self._write(runtime, value)

    async def tick_alarm(self, name: str) -> None:
        """Один шаг periodic-аларма (используется циклом и тестами)."""
        runtime = self._alarms[name]
        transition = runtime.evaluator.on_time(self.elapsed())
        if transition is not None:
            await self._emit(runtime, transition)

    # --- внутреннее ---------------------------------------------------------------

    def _start_tag(self, runtime: _TagRuntime) -> None:
        if runtime.generator.is_static or runtime.task is not None:
            return
        runtime.task = asyncio.create_task(self._tag_loop(runtime), name=f"tag:{runtime.spec.name}")

    def _start_alarm(self, runtime: _AlarmRuntime) -> None:
        if runtime.spec.kind != "periodic" or runtime.task is not None:
            return
        runtime.task = asyncio.create_task(self._alarm_loop(runtime), name=f"alarm:{runtime.spec.name}")

    async def _tag_loop(self, runtime: _TagRuntime) -> None:
        period = runtime.spec.update_period_s
        while True:
            await asyncio.sleep(period)
            await self.update_tag(runtime.spec.name)

    async def _alarm_loop(self, runtime: _AlarmRuntime) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.tick_alarm(runtime.spec.name)

    @staticmethod
    async def _cancel(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _write(self, runtime: _TagRuntime, value: float | bool) -> None:
        now = _utcnow()
        datavalue = ua.DataValue(
            ua.Variant(value, VARIANT_TYPES[runtime.spec.data_type]),
            SourceTimestamp=now,
            ServerTimestamp=now,
        )
        await self._server.write_attribute_value(runtime.node.nodeid, datavalue)
        await self._after_value(runtime, value)

    async def _after_value(self, runtime: _TagRuntime, value: float | bool) -> None:
        runtime.value = value
        for alarm in self._alarms.values():
            if alarm.spec.kind == "limit" and alarm.spec.tag == runtime.spec.name:
                await self._evaluate_limit(alarm, float(value))

    async def _evaluate_limit(self, alarm: _AlarmRuntime, value: float) -> None:
        transition = alarm.evaluator.on_value(value)
        if transition is not None:
            await self._emit(alarm, transition)

    async def _emit(self, alarm: _AlarmRuntime, transition: AlarmTransition) -> None:
        spec = alarm.spec
        async with alarm.lock:
            self._fill_condition(alarm, transition)
            message = self._message(spec, transition)
            await emit_event(self._server, alarm.evgen, self.notifiers, message)
        if self._on_event is not None:
            self._on_event(
                {
                    "type": "alarm",
                    "alarm": spec.name,
                    "active": transition.active,
                    "limit": transition.limit,
                    "value": transition.value,
                    "severity": spec.severity,
                    "message": message,
                }
            )

    def _fill_condition(self, alarm: _AlarmRuntime, transition: AlarmTransition) -> None:
        """Поля условия, которые читают просмотрщики A&C (ADR-002 п. 3b).

        Acknowledge/Confirm не поддерживаются, поэтому аларм сразу «квитирован».
        """
        event = alarm.evgen.event
        active = transition.active
        event.NodeId = alarm.node.nodeid  # ConditionId
        event.ConditionName = alarm.spec.name
        event.Retain = active
        event.Severity = alarm.spec.severity
        event.EnabledState = ua.LocalizedText("Enabled")
        setattr(event, "EnabledState/Id", True)
        event.ActiveState = ua.LocalizedText("Active" if active else "Inactive")
        setattr(event, "ActiveState/Id", active)
        event.AckedState = ua.LocalizedText("Acknowledged")
        setattr(event, "AckedState/Id", True)
        event.ConfirmedState = ua.LocalizedText("Confirmed")
        setattr(event, "ConfirmedState/Id", True)
        if alarm.spec.kind == "limit":
            if alarm.spec.high is not None:
                event.HighLimit = alarm.spec.high
            if alarm.spec.low is not None:
                event.LowLimit = alarm.spec.low

    @staticmethod
    def _message(spec: AlarmSpec, transition: AlarmTransition) -> str:
        if not transition.active:
            return f"{spec.name}: норма"
        if spec.message:
            return spec.message
        if transition.limit == "High":
            return f"{spec.name}: {spec.tag} выше {spec.high:g}"
        if transition.limit == "Low":
            return f"{spec.name}: {spec.tag} ниже {spec.low:g}"
        return f"{spec.name}: активен"

    async def _pre_write(self, event: ServerItemCallback, _dispatcher: Any) -> None:
        """Запись клиента в writable-тег получает SourceTimestamp до применения (ADR-002 п. 2)."""
        if not event.is_external:
            return
        now = _utcnow()
        for write in event.request_params.NodesToWrite:
            if (
                write.AttributeId == ua.AttributeIds.Value
                and write.NodeId in self._writable_nodes
                and write.Value is not None
                and write.Value.SourceTimestamp is None
            ):
                write.Value.SourceTimestamp = now

    async def _post_write(self, event: ServerItemCallback, _dispatcher: Any) -> None:
        """Успешная запись клиента: обновить значение, алармы и сообщить наверх."""
        if not event.is_external:
            return
        results = event.response_params or []
        for write, status in zip(event.request_params.NodesToWrite, results, strict=False):
            name = self._writable_nodes.get(write.NodeId)
            if name is None or write.AttributeId != ua.AttributeIds.Value or not status.is_good():
                continue
            runtime = self._tags.get(name)
            if runtime is None or write.Value is None or write.Value.Value is None:
                continue
            value = coerce_value(write.Value.Value.Value, runtime.spec.data_type)
            await self._after_value(runtime, value)
            if self._on_event is not None:
                self._on_event({"type": "write", "tag": name, "value": value})
