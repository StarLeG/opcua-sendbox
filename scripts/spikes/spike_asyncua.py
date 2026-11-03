"""Спайк ADR-002: проверка поведения asyncua до начала реализации.

Автоматические проверки (печатают PASS/FAIL и факты для ADR):
  1. endpoint за NAT: GetEndpoints и CreateSession/ActivateSession, в том числе через
     TCP-прокси с другим внешним портом (эмуляция переназначения портов Docker/NAT);
  2. HistoryRead на работающем сервере: внутренняя запись и запись внешним клиентом
     без SourceTimestamp;
  3. ExclusiveLimitAlarmType: какие поля события доходят до asyncua.Client;
  4. InternalSession._current_connections как счётчик активных сессий.

Ручная проверка (UaExpert, ModbusToolkit): `--serve` поднимает тот же сервер и ждёт.

Запуск:  uv run python scripts/spikes/spike_asyncua.py [--serve] [--public-host H]
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import logging
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

from asyncua import Client, Server, ua
from asyncua.server.history import HistoryDict
from asyncua.server.internal_session import InternalSession

NAMESPACE = "urn:electroman:opcua-sendbox"
PUBLIC_HOST_DEFAULT = "opcua-sendbox.electroman.by"

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    """Запомнить и напечатать результат проверки."""
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class ErrorCounter(logging.Handler):
    """Считает записи ERROR и выше из asyncua (исключения внутри сервера)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def start_tcp_proxy(listen_port: int, target_port: int) -> asyncio.Server:
    """TCP-прокси listen_port -> target_port: эмуляция переназначения порта NAT."""

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        server_r, server_w = await asyncio.open_connection("127.0.0.1", target_port)
        await asyncio.gather(pipe(client_r, server_w), pipe(server_r, client_w))

    return await asyncio.start_server(handle, "127.0.0.1", listen_port)


class SendBoxHistory(HistoryDict):
    """HistoryDict, устойчивый к DataValue без SourceTimestamp (запись UaExpert).

    Подставляет ServerTimestamp, иначе текущее UTC. Метку предыдущего значения
    не копирует: иначе история выглядела бы так, будто значение не менялось.
    """

    async def save_node_value(self, node_id: ua.NodeId, datavalue: ua.DataValue) -> None:
        if datavalue.SourceTimestamp is None:
            stamp = datavalue.ServerTimestamp or datetime.now(UTC)
            datavalue = dataclasses.replace(datavalue, SourceTimestamp=stamp)
        await super().save_node_value(node_id, datavalue)


async def emit_event(server: Server, evgen: Any, notifiers: list[ua.NodeId], message: str) -> None:
    """Одно событие (один EventId) — на каждый узел-нотификатор из списка.

    asyncua доставляет событие только мониторам ровно на emitting_node и не
    поднимает его по HasNotifier до узла Server, поэтому рассылаем сами.
    Каждому нотификатору уходит снимок-копия: SubscriptionService хранит для
    ConditionRefresh ссылку на последнее отправленное событие условия, и она не
    должна меняться при следующих изменениях evgen.event. Узел Server идёт
    последним — refresh адресуется подписчикам Server (так подписываются
    просмотрщики A&C по умолчанию).
    """
    await evgen.trigger(message=message)  # EventId, Time, Message; уходит на emitting_node
    service = server.iserver.isession.subscription_service
    server_id = ua.NodeId(ua.ObjectIds.Server)
    ordered = [n for n in notifiers if n != server_id] + [n for n in notifiers if n == server_id]
    for node_id in ordered:
        if node_id == evgen.event.emitting_node and node_id != server_id:
            continue  # trigger() уже доставил
        snapshot = copy.copy(evgen.event)
        snapshot.emitting_node = node_id
        await service.trigger_event(snapshot)


async def build_server(
    public_host: str,
    public_port: int,
    bind_port: int,
    *,
    match_client_ip: bool,
    robust_history: bool = False,
) -> tuple[Server, dict[str, Any]]:
    """Сервер: слушает 0.0.0.0:bind_port, объявляет public_host:public_port."""
    server = Server()
    # ConditionRefresh/ConditionRefresh2 и события RefreshStart/End: в asyncua выключены
    # по умолчанию, включаются только до init(). Без них просмотрщики A&C не получают
    # список активных алармов при подключении.
    server.iserver.bind_condition_methods = True
    await server.init()
    if robust_history:
        server.iserver.history_manager.set_storage(SendBoxHistory())
    server.set_endpoint(f"opc.tcp://{public_host}:{public_port}/electroman/")
    # host=None: asyncio слушает все интерфейсы, IPv4 и IPv6. С "0.0.0.0" клиенты,
    # у которых localhost разрешается в ::1 (UaExpert, ModbusToolkit), не подключаются.
    server.socket_address = (None, bind_port)  # type: ignore[assignment]
    server.set_match_discovery_client_ip(match_client_ip)
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    server.set_server_name("OPC UA SendBox spike")
    idx = await server.register_namespace(NAMESPACE)

    sendbox = await server.nodes.objects.add_folder(ua.NodeId("SendBox", idx), "SendBox")
    await sendbox.set_event_notifier([ua.EventNotifier.SubscribeToEvents])
    # Папка-источник событий должна быть достижима от Server по HasNotifier,
    # иначе часть клиентов не видит её как источник событий.
    await server.nodes.server.add_reference(sendbox.nodeid, ua.ObjectIds.HasNotifier)
    sine = await sendbox.add_variable(ua.NodeId("Tags.Sine", idx), "Sine", 0.0)
    writable = await sendbox.add_variable(ua.NodeId("Tags.Writable", idx), "Writable", 0.0)
    await writable.set_writable()
    await server.historize_node_data_change(sine, period=None, count=1000)
    await server.historize_node_data_change(writable, period=None, count=1000)
    evgen = await server.get_event_generator(ua.ObjectIds.ExclusiveLimitAlarmType, sendbox)
    return server, {"idx": idx, "sendbox": sendbox, "sine": sine, "writable": writable, "evgen": evgen}


# --- 1. endpoint ----------------------------------------------------------


async def raw_get_endpoints(url: str, endpoint_url_param: str) -> list[str]:
    """GetEndpoints с заданным EndpointUrl в запросе (пустая строка — без него)."""
    client = Client(url)
    await client.connect_socket()
    try:
        await client.send_hello()
        await client.open_secure_channel()
        params = ua.GetEndpointsParameters()
        params.EndpointUrl = endpoint_url_param
        endpoints = await client.uaclient.get_endpoints(params)
        await client.close_secure_channel()
    finally:
        client.disconnect_socket()
    return [edp.EndpointUrl for edp in endpoints]


async def session_roundtrip(url: str, node_id: ua.NodeId) -> tuple[list[str], Any]:
    """CreateSession + ActivateSession + Read; вернуть ServerEndpoints и значение."""
    client = Client(url)
    await client.connect_socket()
    try:
        await client.send_hello()
        await client.open_secure_channel()
        created = await client.create_session()
        server_endpoints = [edp.EndpointUrl for edp in created.ServerEndpoints]
        await client.activate_session()
        value = await client.get_node(node_id).read_value()
        await client.close_session()
        await client.close_secure_channel()
    finally:
        client.disconnect_socket()
    return server_endpoints, value


async def check_endpoints(public_host: str) -> None:
    print("\n=== 1. Endpoint за NAT ===")
    bind_port, public_port = free_port(), free_port()
    public_url = f"opc.tcp://{public_host}:{public_port}/electroman/"

    # 1a. Поведение по умолчанию (match_discovery_client_ip=True): что отдаёт сервер
    # клиенту без EndpointUrl в запросе — это сценарий подмены на sockname (#232).
    server, nodes = await build_server(public_host, public_port, bind_port, match_client_ip=True)
    async with server:
        direct = f"opc.tcp://127.0.0.1:{bind_port}/electroman/"
        urls = await raw_get_endpoints(direct, "")
        report(
            "1a defaults, GetEndpoints без EndpointUrl",
            all(u == public_url for u in urls),
            f"получено {sorted(set(urls))}, ожидалось {public_url}",
        )

    # 1b..1e. Рекомендуемая настройка: socket_address=0.0.0.0, client_ip-подстановка выкл.
    server, nodes = await build_server(public_host, public_port, bind_port, match_client_ip=False)
    async with server:
        direct = f"opc.tcp://127.0.0.1:{bind_port}/electroman/"
        urls = await raw_get_endpoints(direct, "")
        report(
            "1b client_ip=off, GetEndpoints без EndpointUrl",
            all(u == public_url for u in urls),
            f"получено {sorted(set(urls))}",
        )
        urls = await raw_get_endpoints(direct, public_url)
        report(
            "1c client_ip=off, GetEndpoints с публичным EndpointUrl",
            all(u == public_url for u in urls),
            f"получено {sorted(set(urls))}",
        )
        # Через прокси с другим портом: клиент видит localhost:proxy, сервер слушает bind.
        proxy_port = free_port()
        proxy = await start_tcp_proxy(proxy_port, bind_port)
        try:
            via_proxy = f"opc.tcp://localhost:{proxy_port}/electroman/"
            urls = await raw_get_endpoints(via_proxy, via_proxy)
            report(
                "1d прокси (порт переназначен), GetEndpoints отражает адрес клиента",
                all(u == via_proxy for u in urls),
                f"получено {sorted(set(urls))}",
            )
            try:
                server_eps, value = await session_roundtrip(via_proxy, nodes["sine"].nodeid)
                report(
                    "1e прокси, CreateSession+ActivateSession+Read",
                    True,
                    f"ServerEndpoints={sorted(set(server_eps))}, value={value!r}",
                )
            except Exception as exc:  # noqa: BLE001 - спайк фиксирует любую ошибку
                report("1e прокси, CreateSession+ActivateSession+Read", False, repr(exc))
        finally:
            proxy.close()
            await proxy.wait_closed()


# --- 2. history -----------------------------------------------------------


async def check_history(errors: ErrorCounter, *, robust: bool) -> None:
    label = "SendBoxHistory" if robust else "HistoryDict"
    print(f"\n=== 2. HistoryRead на работающем сервере ({label}) ===")
    bind_port = free_port()
    server, nodes = await build_server(
        "127.0.0.1", bind_port, bind_port, match_client_ip=False, robust_history=robust
    )
    async with server:
        start = datetime.now(UTC) - timedelta(seconds=1)
        for i in range(5):
            await nodes["sine"].write_value(float(i))
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.5)
        url = f"opc.tcp://127.0.0.1:{bind_port}/electroman/"
        async with Client(url) as client:
            sine = client.get_node(nodes["sine"].nodeid)
            history = await sine.read_raw_history(starttime=start, endtime=datetime.now(UTC))
            values = [dv.Value.Value for dv in history]
            report(
                f"2a [{label}] внутренняя запись → HistoryRead",
                values[-5:] == [0.0, 1.0, 2.0, 3.0, 4.0],
                f"значения={values}",
            )

            before = len(errors.records)
            writable = client.get_node(nodes["writable"].nodeid)
            # Внешняя запись без SourceTimestamp — как UaExpert.
            await writable.write_attribute(
                ua.AttributeIds.Value, ua.DataValue(ua.Variant(42.0, ua.VariantType.Double))
            )
            await asyncio.sleep(1.5)
            new_errors = errors.records[before:]
            try:
                hist = await writable.read_raw_history(
                    starttime=start, endtime=datetime.now(UTC) + timedelta(seconds=5)
                )
                hist_detail = [(dv.Value.Value, dv.SourceTimestamp) for dv in hist]
                hist_ok = any(dv.Value.Value == 42.0 for dv in hist)
            except Exception as exc:  # noqa: BLE001
                hist_detail, hist_ok = [repr(exc)], False
            report(
                f"2b [{label}] внешняя запись без SourceTimestamp → HistoryRead",
                hist_ok and not new_errors,
                f"история={hist_detail}; ошибок в логе={len(new_errors)} "
                f"{[r.getMessage()[:120] for r in new_errors]}",
            )
            alive = await sine.read_value()
            report(f"2c [{label}] сервер отвечает после внешней записи", True, f"sine={alive!r}")


# --- 3. alarms ------------------------------------------------------------


class EventCollector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event_notification(self, event: Any) -> None:
        self.events.append(event.get_event_props_as_fields_dict())


async def check_alarms() -> None:
    print("\n=== 3. ExclusiveLimitAlarmType ===")
    bind_port = free_port()
    server, nodes = await build_server("127.0.0.1", bind_port, bind_port, match_client_ip=False)
    evgen = nodes["evgen"]
    fields = sorted(name for name in vars(evgen.event) if not name.startswith("_"))
    print(f"    поля evgen.event: {fields}")
    async with server:
        url = f"opc.tcp://127.0.0.1:{bind_port}/electroman/"
        async with Client(url) as client:
            for label, method in (
                ("subscribe_events", "subscribe_events"),
                ("subscribe_alarms_and_conditions", "subscribe_alarms_and_conditions"),
            ):
                for source_label, source in (
                    ("папка SendBox", nodes["sendbox"].nodeid),
                    ("узел Server", ua.ObjectIds.Server),
                ):
                    collector = EventCollector()
                    sub = await client.create_subscription(100, collector)
                    await getattr(sub, method)(source, ua.ObjectIds.ExclusiveLimitAlarmType)
                    await asyncio.sleep(0.3)
                    evgen.event.Severity = 700
                    evgen.event.ActiveState = ua.LocalizedText("Active")
                    await emit_event(
                        server,
                        evgen,
                        [nodes["sendbox"].nodeid, ua.NodeId(ua.ObjectIds.Server)],
                        "Sine > 80",
                    )
                    await asyncio.sleep(0.8)
                    await sub.delete()
                    got = collector.events
                    detail = "событий нет"
                    if got:
                        ev = got[-1]
                        detail = (
                            f"событий={len(got)}; Message={ev.get('Message')!r}; "
                            f"Severity={ev.get('Severity')!r}; ActiveState={ev.get('ActiveState')!r}; "
                            f"LimitState={ev.get('LimitState', '—')!r}; поля={sorted(ev)}"
                        )
                    report(f"3 {label} от {source_label}", bool(got), detail)


def set_condition_state(evgen: Any, condition_id: ua.NodeId, *, active: bool) -> None:
    """Поля условия, которые читают просмотрщики A&C (UaExpert и подобные).

    Retain=True держит активное условие в ConditionRefresh; AckedState/Confirmed —
    True, потому что Acknowledge не поддерживается: аларм сразу «квитирован».
    """
    event = evgen.event
    event.NodeId = condition_id  # ConditionId (select clause AttributeId=NodeId)
    event.ConditionName = "SineHigh"
    event.Retain = active
    event.EnabledState = ua.LocalizedText("Enabled")
    setattr(event, "EnabledState/Id", True)
    event.ActiveState = ua.LocalizedText("Active" if active else "Inactive")
    setattr(event, "ActiveState/Id", active)
    event.AckedState = ua.LocalizedText("Acknowledged")
    setattr(event, "AckedState/Id", True)
    event.ConfirmedState = ua.LocalizedText("Confirmed")
    setattr(event, "ConfirmedState/Id", True)
    event.Severity = 700 if active else 100


def field(ev: dict[str, Any], name: str) -> Any:
    raw = ev.get(name)
    return raw.Value if isinstance(raw, ua.Variant) else raw


async def check_condition_viewer() -> None:
    print("\n=== 3b. Поведение как у просмотрщика A&C: ConditionId, Retain, ConditionRefresh ===")
    bind_port = free_port()
    server, nodes = await build_server("127.0.0.1", bind_port, bind_port, match_client_ip=False)
    evgen = nodes["evgen"]
    condition_id = ua.NodeId("Alarms.SineHigh", nodes["idx"])
    notifiers = [nodes["sendbox"].nodeid, ua.NodeId(ua.ObjectIds.Server)]
    async with server:
        url = f"opc.tcp://127.0.0.1:{bind_port}/electroman/"
        async with Client(url) as client:
            collector = EventCollector()
            sub = await client.create_subscription(100, collector)
            await sub.subscribe_alarms_and_conditions(
                ua.ObjectIds.Server, ua.ObjectIds.ExclusiveLimitAlarmType
            )
            await asyncio.sleep(0.3)

            set_condition_state(evgen, condition_id, active=True)
            await emit_event(server, evgen, notifiers, "Sine > 80")
            await asyncio.sleep(0.8)
            got = [e for e in collector.events if field(e, "Message")]
            ev = got[-1] if got else {}
            report(
                "3b-1 активация: ConditionId/Retain/ActiveState.Id/EnabledState.Id",
                bool(got)
                and field(ev, "NodeId") == condition_id
                and field(ev, "Retain") is True
                and field(ev, "ActiveState/Id") is True
                and field(ev, "EnabledState/Id") is True,
                f"ConditionId={field(ev, 'NodeId')}, Retain={field(ev, 'Retain')}, "
                f"Active.Id={field(ev, 'ActiveState/Id')}, Enabled.Id={field(ev, 'EnabledState/Id')}, "
                f"Acked.Id={field(ev, 'AckedState/Id')}, ConditionName={field(ev, 'ConditionName')}",
            )

            async def refresh() -> list[dict[str, Any]]:
                collector.events.clear()
                condition_type = client.get_node(ua.ObjectIds.ConditionType)
                await condition_type.call_method(
                    ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh),
                    ua.Variant(sub.subscription_id, ua.VariantType.UInt32),
                )
                await asyncio.sleep(0.8)
                return list(collector.events)

            try:
                refreshed = await refresh()
                conditions = [e for e in refreshed if field(e, "NodeId") == condition_id]
                report(
                    "3b-2 ConditionRefresh при активном аларме возвращает условие",
                    len(conditions) == 1,
                    f"событий в ответе={len(refreshed)}, условий SineHigh={len(conditions)}",
                )
            except Exception as exc:  # noqa: BLE001
                report("3b-2 ConditionRefresh при активном аларме", False, repr(exc))

            set_condition_state(evgen, condition_id, active=False)
            await emit_event(server, evgen, notifiers, "Sine в норме")
            await asyncio.sleep(0.5)
            try:
                refreshed = await refresh()
                conditions = [e for e in refreshed if field(e, "NodeId") == condition_id]
                report(
                    "3b-3 после сброса (Retain=False) ConditionRefresh условие не возвращает",
                    not conditions,
                    f"событий в ответе={len(refreshed)}, условий SineHigh={len(conditions)}",
                )
            except Exception as exc:  # noqa: BLE001
                report("3b-3 ConditionRefresh после сброса", False, repr(exc))


# --- 4. client count ------------------------------------------------------


async def check_client_count() -> None:
    print("\n=== 4. InternalSession._current_connections ===")
    bind_port = free_port()
    server, _ = await build_server("127.0.0.1", bind_port, bind_port, match_client_ip=False)
    async with server:
        url = f"opc.tcp://127.0.0.1:{bind_port}/electroman/"
        base = InternalSession._current_connections
        c1, c2 = Client(url), Client(url)
        await c1.connect()
        await c2.connect()
        two = InternalSession._current_connections - base
        await c1.disconnect()
        one = InternalSession._current_connections - base
        # Обрыв без CloseSession: закрываем сокет напрямую.
        c2.uaclient.protocol.transport.abort()  # type: ignore[union-attr]
        await asyncio.sleep(1.0)
        after_abort = InternalSession._current_connections - base
        report(
            "4 счётчик сессий",
            (two, one) == (2, 1),
            f"2 клиента → {two}; после disconnect → {one}; после обрыва (1 с) → {after_abort} "
            "(обрыв досчитывается по таймауту сессии)",
        )


async def serve(public_host: str, port: int) -> None:
    """Ручная проверка: теги, история и цикл аларма SineHigh (10 с активен / 10 с норма)."""
    server, nodes = await build_server(
        public_host, port, port, match_client_ip=False, robust_history=True
    )
    notifiers = [nodes["sendbox"].nodeid, ua.NodeId(ua.ObjectIds.Server)]
    condition_id = ua.NodeId("Alarms.SineHigh", nodes["idx"])
    async with server:
        print(f"Сервер: слушает 0.0.0.0:{port}, объявляет opc.tcp://{public_host}:{port}/electroman/")
        n = 0
        while True:
            n += 1
            await nodes["sine"].write_value(float(n % 100))
            if n % 10 == 0:
                # Цикл аларма для просмотрщиков A&C: 10 с активен, 10 с в норме.
                active = (n // 10) % 2 == 1
                set_condition_state(nodes["evgen"], condition_id, active=active)
                text = "активен" if active else "в норме"
                await emit_event(
                    server, nodes["evgen"], notifiers, f"Стенд: аларм SineHigh {text} ({n // 10})"
                )
            print(f"  клиентов: {InternalSession._current_connections}", end="\r")
            await asyncio.sleep(1)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="поднять сервер для ручной проверки")
    parser.add_argument("--public-host", default=PUBLIC_HOST_DEFAULT)
    parser.add_argument("--port", type=int, default=4840)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    errors = ErrorCounter()
    logging.getLogger("asyncua").addHandler(errors)
    if args.serve:
        await serve(args.public_host, args.port)
        return
    await check_endpoints(args.public_host)
    await check_history(errors, robust=False)
    await check_history(errors, robust=True)
    await check_alarms()
    await check_condition_viewer()
    await check_client_count()
    print("\n=== Итог ===")
    for name, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
