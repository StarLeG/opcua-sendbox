"""WebSocket live-канал сессии и общего сервера (TASK-202, как ADR-005 Modbus SendBox).

- `/ws/sessions/{id}?key=...` — ключ в query: браузер не ставит заголовки WebSocket;
- `/ws/shared` — общий сервер, без ключа.

Первый кадр — `{"type": "hello", ...}` с описанием сессии/сервера; дальше —
сообщения воркера как есть: `tag_values` (снимок не чаще WS_STATE_HZ), `alarm`,
`write`, `client_count`, `log`; при закрытии сессии — `closed` или `crashed`, после
чего сервер закрывает WebSocket (1000 / 1011). В `/ws/shared` дополнительно уходит
кадр `counters` (счётчики сервера для UI, TASK-205) раз в COUNTERS_INTERVAL_S.

Backpressure: у каждого клиента ограниченная очередь; продюсер не ждёт клиента
(`put_nowait`). Переполнение = клиент не успевает → отключение кодом 1013, остальные
получают кадры дальше. Отправка ограничена таймаутом. Открытый WebSocket не
продлевает жизнь сессии: активность — API-вызовы и OPC UA клиенты (инвариант 1).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.routes import session_out
from app.api.services import AppServices
from app.api.stats import counters_payload
from app.opcua.session_manager import SessionNotFoundError

logger = logging.getLogger(__name__)

ws_router = APIRouter()

#: Очередь исходящих кадров одного клиента.
WS_CLIENT_QUEUE = 64
#: Таймаут отправки кадра, с.
WS_SEND_TIMEOUT_S = 5.0
WS_CLOSE_NORMAL = 1000
WS_CLOSE_POLICY = 1008  # нет ключа / чужая сессия
WS_CLOSE_SERVER_ERROR = 1011  # сервер сессии упал
WS_CLOSE_SLOW = 1013  # клиент не успевает

#: Сообщения воркера, которые клиенту не нужны (внутренние).
_INTERNAL = frozenset({"reply", "ready", "stats", "exited", "error"})

#: Период отправки кадра `counters` в `/ws/shared`, с.
COUNTERS_INTERVAL_S = 2.0


class WsChannel:
    """Одно WS-соединение: очередь кадров, задача отправки, закрытие."""

    def __init__(self, ws: WebSocket, *, queue_size: int | None = None) -> None:
        self.ws = ws
        size = WS_CLIENT_QUEUE if queue_size is None else queue_size  # читается при создании
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=size)
        self.close_code = WS_CLOSE_NORMAL
        self.slow = False
        #: После первого кадра закрытия (closed/crashed) или отключения за медленность
        #: канал ничего не принимает: иначе `closed` после `crashed` подменил бы код 1011.
        self.finishing = False

    def offer(self, message: dict[str, Any]) -> None:
        """Положить кадр, не дожидаясь клиента; переполнение — отключение 1013."""
        kind = message.get("type")
        if kind in _INTERNAL or self.finishing:
            return
        try:
            self._queue.put_nowait(json.dumps(message, ensure_ascii=False, default=str))
        except asyncio.QueueFull:
            self.slow = True
            self.finishing = True
            self.close_code = WS_CLOSE_SLOW
            self._finish_now()
            return
        if kind in ("closed", "crashed"):
            self.finishing = True
            self.close_code = WS_CLOSE_SERVER_ERROR if kind == "crashed" else WS_CLOSE_NORMAL
            self._finish()

    def _finish(self) -> None:
        """Закрыть после отправки уже поставленных кадров."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    def _finish_now(self) -> None:
        """Медленный клиент: выбросить очередь и закрыть."""
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    async def run(self) -> None:
        """Отправлять кадры, пока клиент на связи или канал не закрыт."""
        receiver = asyncio.create_task(self._drain_incoming())
        try:
            while True:
                getter = asyncio.create_task(self._queue.get())
                done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:  # клиент ушёл
                    getter.cancel()
                    return
                frame = getter.result()
                if frame is None:
                    break
                await asyncio.wait_for(self.ws.send_text(frame), timeout=WS_SEND_TIMEOUT_S)
            with contextlib.suppress(Exception):
                await self.ws.close(code=self.close_code)
        except (WebSocketDisconnect, TimeoutError, RuntimeError):
            logger.debug("ws: клиент отключился или не успевает", exc_info=True)
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver

    async def _drain_incoming(self) -> None:
        """Входящие кадры клиента не нужны; ждём только отключения."""
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return


def _services(ws: WebSocket) -> AppServices:
    services: AppServices = ws.app.state.services
    return services


@ws_router.websocket("/ws/sessions/{session_id}")
async def ws_session(ws: WebSocket, session_id: str) -> None:
    services = _services(ws)
    key = ws.query_params.get("key")
    info = services.manager.keys.check(key)
    try:
        if info is None:
            raise SessionNotFoundError("нужен ключ")
        session = services.manager.get(session_id, info.key, touch=False)
    except SessionNotFoundError:
        await ws.close(code=WS_CLOSE_POLICY, reason="сессия не найдена")
        return
    await ws.accept()
    channel = WsChannel(ws)
    channel.offer({"type": "hello", "session": session_out(session, services).model_dump()})
    session.listeners.add(channel.offer)
    try:
        await channel.run()
    finally:
        session.listeners.discard(channel.offer)


@ws_router.websocket("/ws/shared")
async def ws_shared(ws: WebSocket) -> None:
    services = _services(ws)
    shared = services.manager.shared
    await ws.accept()
    channel = WsChannel(ws)
    channel.offer(
        {
            "type": "hello",
            "shared": {"endpoint_url": shared.endpoint_url, "status": shared.status, "read_only": True},
        }
    )
    shared.listeners.add(channel.offer)

    async def _push_counters() -> None:
        """Кадр `counters` раз в COUNTERS_INTERVAL_S: счётчики UI не считаются по HTTP."""
        while True:
            payload = counters_payload(services.manager, services.stats).model_dump()
            channel.offer({"type": "counters", **payload})
            await asyncio.sleep(COUNTERS_INTERVAL_S)

    counters_task = asyncio.create_task(_push_counters())
    try:
        await channel.run()
    finally:
        counters_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await counters_task
        shared.listeners.discard(channel.offer)
