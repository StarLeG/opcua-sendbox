"""Реестр сессий: лимиты, жизненный цикл, общий сервер, автозакрытие (ADR-001).

Конкурентность (ТЗ, разд. 4):
- реестр меняется только под `asyncio.Lock`; запуск и остановка процессов —
  вне лока, чтобы медленный spawn одной сессии не тормозил остальные;
- порты выдаёт потокобезопасный PortAllocator.

Кто освобождает порт (инвариант 3 ISSUES.md — ни одна сессия не застревает):
- `create` — при любой неудаче или отмене запуска, а также если сессию удалили,
  пока она стартовала;
- `delete` — для работающей сессии после остановки процесса;
- обработчик аварии — для упавшего процесса.

Активность (инвариант 1): API-вызовы по сессии сдвигают `last_activity`; пока к
серверу подключены OPC UA клиенты (`client_count > 0`), idle-таймер стоит, а
переход в 0 запускает его заново. Потолок жизни действует всегда.
"""
from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Config
from app.opcua.keys import KeyRegistry
from app.opcua.port_allocator import PortAllocator, PortsExhaustedError
from app.opcua.security import ServerSecurity
from app.opcua.tags import AlarmSpec, TagSpec, check_session_content, demo_alarms, demo_tags
from app.opcua.worker import WorkerParams
from app.opcua.worker_handle import MessageCallback, WorkerError, WorkerHandle

logger = logging.getLogger(__name__)

#: Период проверок reaper'а, с.
REAPER_INTERVAL_S = 5.0
#: Перезапуск общего сервера после падения: начальная и максимальная пауза, с.
SHARED_RESTART_MIN_S = 1.0
SHARED_RESTART_MAX_S = 60.0

Listener = Callable[[dict[str, Any]], None]


class Handle(Protocol):
    """То, что SessionManager использует у WorkerHandle (подменяется в тестах)."""

    params: WorkerParams

    @property
    def pid(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    async def start(self, timeout: float = 10.0) -> None: ...

    async def request(self, cmd: str, timeout: float = 10.0, **fields: Any) -> Any: ...

    async def stop(self, timeout: float = 5.0) -> None: ...

    async def kill(self) -> None: ...


HandleFactory = Callable[[WorkerParams, MessageCallback], Handle]


def default_handle_factory(params: WorkerParams, on_message: MessageCallback) -> Handle:
    return WorkerHandle(params, on_message=on_message)


class SessionError(RuntimeError):
    """Базовая ошибка менеджера сессий."""


class SessionNotFoundError(SessionError):
    """Нет такой сессии у этого ключа (чужая сессия — тоже «нет», 404)."""


class SessionLimitError(SessionError):
    """Превышен лимит сессий (429). `scope`: key | ip | total."""

    def __init__(self, scope: str, message: str) -> None:
        super().__init__(message)
        self.scope = scope


class NoCapacityError(SessionError):
    """Нет свободных портов или сервер сессии не запустился (503)."""


class SessionState(enum.StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass
class Session:
    id: str
    key: str
    ip: str
    port: int
    endpoint_url: str
    created: float
    last_activity: float
    state: SessionState = SessionState.STARTING
    clients: int = 0
    handle: Handle | None = None
    listeners: set[Listener] = field(default_factory=set)

    def publish(self, message: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                listener(message)
            except Exception:  # слушатель (WS) не должен ломать доставку остальным
                logger.exception("слушатель сессии %s", self.id)


@dataclass
class SharedServer:
    """Состояние общего демо-сервера."""

    port: int
    endpoint_url: str
    handle: Handle | None = None
    status: str = "down"  # running | restarting | down
    restarts: int = 0
    clients: int = 0
    listeners: set[Listener] = field(default_factory=set)


class SessionManager:
    """Сессии, общий сервер, ключи и reaper."""

    def __init__(
        self,
        config: Config,
        *,
        keys: KeyRegistry | None = None,
        allocator: PortAllocator | None = None,
        handle_factory: HandleFactory = default_handle_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.keys = keys or KeyRegistry(per_ip_per_hour=config.KEYS_PER_HOUR_PER_IP, clock=clock)
        self.allocator = allocator or PortAllocator(config.OPCUA_SESSION_PORT_MIN, config.OPCUA_SESSION_PORT_MAX)
        self._factory = handle_factory
        self._clock = clock
        self._lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False
        self._started_at = clock()
        #: Счётчики для /metrics (с запуска процесса).
        self.created_total = 0
        self.start_failures_total = 0
        self.closed_total: dict[str, int] = {}
        self.shared = SharedServer(
            port=config.OPCUA_SHARED_PORT,
            endpoint_url=config.endpoint_url(config.OPCUA_SHARED_PORT),
        )

    def uptime(self) -> float:
        return self._clock() - self._started_at

    # --- запросы ----------------------------------------------------------------------

    def sessions(self) -> list[Session]:
        """Снимок всех сессий (для метрик и тестов)."""
        return list(self._sessions.values())

    def for_key(self, key: str) -> list[Session]:
        return [s for s in self._sessions.values() if s.key == key]

    def get(self, session_id: str, key: str, *, touch: bool = True) -> Session:
        session = self._sessions.get(session_id)
        if session is None or session.key != key:
            raise SessionNotFoundError("сессия не найдена")
        if touch:
            session.last_activity = self._clock()
        return session

    async def request(self, session_id: str, key: str, cmd: str, **fields: Any) -> Any:
        """Команда воркеру сессии (добавить тег, вызвать аларм…)."""
        session = self.get(session_id, key)
        if session.state is not SessionState.RUNNING or session.handle is None:
            raise SessionNotFoundError("сессия ещё запускается или уже закрывается")
        return await session.handle.request(cmd, **fields)

    # --- создание и удаление ------------------------------------------------------------

    async def create(
        self,
        key: str,
        ip: str,
        tags: list[TagSpec] | None = None,
        alarms: list[AlarmSpec] | None = None,
    ) -> Session:
        """Создать сессию и дождаться её сервера.

        tags/alarms по умолчанию — демо-набор. Ошибки: ValueError (содержимое),
        SessionLimitError (429), NoCapacityError (503).
        """
        if self._closed:
            raise NoCapacityError("сервис останавливается")
        tags = demo_tags(shared=False) if tags is None else tags
        alarms = demo_alarms() if alarms is None else alarms
        check_session_content(
            tags,
            alarms,
            max_tags=self.config.TAGS_PER_SESSION,
            max_alarms=self.config.ALARMS_PER_SESSION,
        )
        async with self._lock:
            self._check_limits(key, ip)
            try:
                port = self.allocator.acquire()
            except PortsExhaustedError as exc:
                raise NoCapacityError("свободных портов нет, попробуйте позже") from exc
            now = self._clock()
            session = Session(
                id=secrets.token_hex(8),
                key=key,
                ip=ip,
                port=port,
                endpoint_url=self.config.endpoint_url(port),
                created=now,
                last_activity=now,
            )
            self._sessions[session.id] = session

        params = WorkerParams(
            port=port,
            endpoint_url=session.endpoint_url,
            bind_host=self.config.bind_host,
            server_name="OPC UA SendBox",
            history_size=self.config.HISTORY_SIZE,
            state_hz=float(self.config.WS_STATE_HZ),
            tags=tuple(t.model_dump() for t in tags),
            alarms=tuple(a.model_dump() for a in alarms),
            security=ServerSecurity.from_config(self.config),
        )
        handle = self._factory(params, lambda message: self._on_session_message(session.id, message))
        session.handle = handle
        started = False
        try:
            await handle.start(timeout=float(self.config.WORKER_START_TIMEOUT_S))
            async with self._lock:
                if self._sessions.get(session.id) is not session:
                    raise SessionNotFoundError("сессию удалили во время запуска")
                session.state = SessionState.RUNNING
                session.last_activity = self._clock()
            started = True
            self.created_total += 1
            return session
        except WorkerError as exc:
            self.start_failures_total += 1
            raise NoCapacityError(f"сервер сессии не запустился: {exc}") from exc
        finally:
            if not started:
                # Инвариант 3: любая неудача или отмена (CancelledError) откатывает всё.
                await asyncio.shield(self._rollback(session, handle))

    async def _rollback(self, session: Session, handle: Handle) -> None:
        async with self._lock:
            if self._sessions.get(session.id) is session:
                del self._sessions[session.id]
        with contextlib.suppress(Exception):
            await handle.kill()
        self.allocator.release(session.port)

    def _check_limits(self, key: str, ip: str) -> None:
        by_key = sum(1 for s in self._sessions.values() if s.key == key)
        if by_key >= self.config.SESSIONS_PER_KEY:
            raise SessionLimitError(
                "key", f"у ключа уже {by_key} сессий — максимум {self.config.SESSIONS_PER_KEY}"
            )
        by_ip = sum(1 for s in self._sessions.values() if s.ip == ip)
        if by_ip >= self.config.SESSIONS_PER_IP:
            raise SessionLimitError("ip", f"с адреса {ip} уже {by_ip} сессий — максимум {self.config.SESSIONS_PER_IP}")
        if len(self._sessions) >= self.config.SESSIONS_TOTAL:
            raise SessionLimitError("total", "песочница заполнена, попробуйте позже")

    async def delete(self, session_id: str, key: str) -> None:
        """Остановить сессию и освободить порт. Повторный вызов — SessionNotFoundError."""
        async with self._lock:
            session = self.get(session_id, key, touch=False)
            del self._sessions[session_id]
            was_running = session.state is SessionState.RUNNING
            session.state = SessionState.STOPPING
        if not was_running:
            return  # стартующую сессию добивает create (сессии уже нет в реестре)
        await self._stop_session(session, reason="deleted")

    async def _stop_session(self, session: Session, *, reason: str) -> None:
        try:
            if session.handle is not None:
                await session.handle.stop()
        finally:
            self.allocator.release(session.port)
            self.closed_total[reason] = self.closed_total.get(reason, 0) + 1
            session.publish({"type": "closed", "reason": reason})
            session.listeners.clear()

    # --- сообщения воркеров ---------------------------------------------------------------

    def _on_session_message(self, session_id: str, message: dict[str, Any]) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        kind = message.get("type")
        if kind == "client_count":
            clients = int(message.get("clients", 0))
            if clients == 0 and session.clients > 0:
                session.last_activity = self._clock()  # idle-таймер заново (инвариант 1)
            session.clients = clients
        elif kind == "exited" and not message.get("expected") and session.state is SessionState.RUNNING:
            self._spawn(self._handle_crash(session, message))
        session.publish(message)

    async def _handle_crash(self, session: Session, message: dict[str, Any]) -> None:
        async with self._lock:
            if self._sessions.get(session.id) is not session:
                return
            del self._sessions[session.id]
            session.state = SessionState.STOPPING
        logger.warning("сессия %s: процесс воркера упал (exitcode=%s)", session.id, message.get("exitcode"))
        session.publish({"type": "crashed", "exitcode": message.get("exitcode")})
        await self._stop_session(session, reason="crashed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- reaper ---------------------------------------------------------------------------

    async def reap_once(self) -> list[tuple[str, str]]:
        """Закрыть просроченные сессии; вернуть [(id, причина)]."""
        now = self._clock()
        expired: list[tuple[Session, str]] = []
        async with self._lock:
            for session in list(self._sessions.values()):
                if session.state is not SessionState.RUNNING:
                    continue
                if now - session.created >= self.config.SESSION_MAX_LIFETIME_S:
                    reason = "lifetime"
                elif session.clients == 0 and now - session.last_activity >= self.config.SESSION_IDLE_TIMEOUT_S:
                    reason = "idle"
                elif session.handle is not None and not session.handle.is_alive():
                    reason = "crashed"
                else:
                    continue
                del self._sessions[session.id]
                session.state = SessionState.STOPPING
                expired.append((session, reason))
        for session, reason in expired:
            await self._stop_session(session, reason=reason)
        in_use = {s.key for s in self._sessions.values()}
        self.keys.forget_inactive(float(self.config.SESSION_IDLE_TIMEOUT_S), in_use)
        return [(s.id, reason) for s, reason in expired]

    async def _reaper_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_once()
            except Exception:  # reaper не должен умирать от одной ошибки
                logger.exception("reaper")

    # --- общий сервер ----------------------------------------------------------------------

    def _shared_params(self) -> WorkerParams:
        return WorkerParams(
            port=self.shared.port,
            endpoint_url=self.shared.endpoint_url,
            bind_host=self.config.bind_host,
            server_name="OPC UA SendBox (общий)",
            history_size=self.config.HISTORY_SIZE,
            read_only=True,
            state_hz=float(self.config.WS_STATE_HZ),
            tags=tuple(t.model_dump() for t in demo_tags(shared=True)),
            alarms=tuple(a.model_dump() for a in demo_alarms()),
            security=ServerSecurity.from_config(self.config),
        )

    async def start_shared(self) -> None:
        """Запустить общий сервер; при неудаче — повторы с backoff в фоне."""
        if not await self._try_start_shared():
            self._spawn(self._restart_shared())

    async def _try_start_shared(self) -> bool:
        handle = self._factory(self._shared_params(), self._on_shared_message)
        self.shared.handle = handle
        try:
            await handle.start(timeout=float(self.config.WORKER_START_TIMEOUT_S))
        except WorkerError as exc:
            logger.error("общий сервер не запустился: %s", exc)
            self.shared.status = "restarting"
            return False
        self.shared.status = "running"
        return True

    def _on_shared_message(self, message: dict[str, Any]) -> None:
        if message.get("type") == "client_count":
            self.shared.clients = int(message.get("clients", 0))
        if message.get("type") == "exited" and not message.get("expected") and not self._closed:
            self.shared.status = "restarting"
            self.shared.clients = 0
            self._spawn(self._restart_shared())
        for listener in list(self.shared.listeners):
            with contextlib.suppress(Exception):
                listener(message)

    async def _restart_shared(self) -> None:
        delay = SHARED_RESTART_MIN_S
        while not self._closed:
            await asyncio.sleep(delay)
            if self._closed:
                return
            if self.shared.handle is not None:
                with contextlib.suppress(Exception):
                    await self.shared.handle.kill()
            self.shared.restarts += 1
            if await self._try_start_shared():
                logger.warning("общий сервер перезапущен (попытка %d)", self.shared.restarts)
                return
            delay = min(delay * 2, SHARED_RESTART_MAX_S)

    # --- жизненный цикл сервиса -------------------------------------------------------------

    async def start(self, *, reaper_interval: float = REAPER_INTERVAL_S, shared: bool = True) -> None:
        if shared:
            await self.start_shared()
        self._reaper = asyncio.create_task(self._reaper_loop(reaper_interval), name="reaper")

    async def close(self) -> None:
        """Остановить reaper, все сессии и общий сервер."""
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                session.state = SessionState.STOPPING
        await asyncio.gather(
            *(self._stop_session(s, reason="shutdown") for s in sessions if s.handle is not None),
            return_exceptions=True,
        )
        if self.shared.handle is not None:
            with contextlib.suppress(Exception):
                await self.shared.handle.stop()
        self.shared.status = "down"
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
