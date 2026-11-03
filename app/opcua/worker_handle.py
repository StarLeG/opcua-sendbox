"""Сторона родителя: процесс воркера, очереди, запросы с ответами (ADR-001).

WorkerHandle живёт в event loop FastAPI и не блокирует его (инвариант 4):
- блокирующие вызовы multiprocessing (join) идут через `asyncio.to_thread`;
- две reader-нити читают control и telemetry и передают сообщения в loop через
  `call_soon_threadsafe`.

Сообщения, кроме ответов на запросы и `ready`, уходят в `on_message`. Когда
процесс завершился (штатно или упал), `on_message` получает
`{"type": "exited", "exitcode": N, "expected": bool}` ровно один раз, а все
ждущие запросы получают WorkerGoneError.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import multiprocessing
import multiprocessing.process
import queue
import threading
from collections.abc import Callable
from typing import Any

from app.opcua.worker import WorkerParams, run_worker

#: Ёмкость очереди телеметрии (ADR-001 п. 5).
TELEMETRY_QUEUE_SIZE = 64
#: Таймаут ответа воркера на команду, с.
REQUEST_TIMEOUT_S = 10.0
#: Сколько ждать штатной остановки до terminate/kill, с.
STOP_TIMEOUT_S = 5.0

MessageCallback = Callable[[dict[str, Any]], None]


class WorkerError(RuntimeError):
    """Базовая ошибка воркера."""


class WorkerStartError(WorkerError):
    """Воркер не прислал `ready` (ошибка запуска, таймаут, процесс умер)."""


class WorkerCommandError(WorkerError):
    """Воркер выполнил команду с ошибкой (например, имя тега занято)."""


class WorkerGoneError(WorkerError):
    """Процесс воркера завершился, команду выполнить некому."""


class WorkerHandle:
    """Один дочерний процесс с OPC UA сервером."""

    def __init__(
        self,
        params: WorkerParams,
        *,
        on_message: MessageCallback | None = None,
        telemetry_queue_size: int = TELEMETRY_QUEUE_SIZE,
    ) -> None:
        self.params = params
        self._on_message = on_message
        ctx = multiprocessing.get_context("spawn")
        self._commands: multiprocessing.Queue[Any] = ctx.Queue()
        self._control: multiprocessing.Queue[Any] = ctx.Queue()
        self._telemetry: multiprocessing.Queue[Any] = ctx.Queue(maxsize=telemetry_queue_size)
        self._process: multiprocessing.process.BaseProcess = ctx.Process(
            target=run_worker,
            args=(params, self._commands, self._control, self._telemetry),
            name=f"opcua-worker:{params.port}",
            daemon=True,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: asyncio.Future[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._readers: list[threading.Thread] = []
        self._closing = threading.Event()
        self._stopping = False
        self._exited = False
        self._telemetry_paused = threading.Event()
        self.telemetry_dropped = 0

    # --- свойства ---------------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def exitcode(self) -> int | None:
        return self._process.exitcode

    def is_alive(self) -> bool:
        return self._process.is_alive()

    # --- жизненный цикл -----------------------------------------------------------

    async def start(self, timeout: float = 10.0) -> None:
        """Запустить процесс и дождаться `ready`; WorkerStartError — иначе (процесс убит)."""
        self._loop = asyncio.get_running_loop()
        self._ready = self._loop.create_future()
        await asyncio.to_thread(self._process.start)
        self._readers = [
            threading.Thread(target=self._read_control, name=f"ctl:{self.params.port}", daemon=True),
            threading.Thread(target=self._read_telemetry, name=f"tlm:{self.params.port}", daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout)
        except BaseException as exc:
            await self.kill()
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                raise WorkerStartError(f"воркер не ответил за {timeout:g} с") from None
            if isinstance(exc, WorkerError):
                raise
            raise

    async def request(self, cmd: str, timeout: float = REQUEST_TIMEOUT_S, **fields: Any) -> Any:
        """Команда воркеру с ожиданием ответа; результат или WorkerCommandError."""
        if self._loop is None or self._exited or self._stopping:
            raise WorkerGoneError("воркер не запущен или остановлен")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = self._loop.create_future()
        self._pending[request_id] = future
        try:
            await asyncio.to_thread(self._commands.put, {"id": request_id, "cmd": cmd, **fields})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise WorkerCommandError(f"воркер не ответил на {cmd!r} за {timeout:g} с") from None
        finally:
            self._pending.pop(request_id, None)

    async def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Штатная остановка: stop → join; не успел — terminate → kill."""
        if self._stopping:
            return
        self._stopping = True
        if self._process.is_alive():
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._commands.put, {"id": 0, "cmd": "stop"})
            await asyncio.to_thread(self._process.join, timeout)
        await self._force_stop()

    async def kill(self) -> None:
        """Немедленно убить процесс (ошибка запуска, зависание)."""
        self._stopping = True
        await self._force_stop()

    async def _force_stop(self) -> None:
        if self._process.pid is not None and self._process.is_alive():
            self._process.terminate()
            await asyncio.to_thread(self._process.join, 2.0)
        if self._process.pid is not None and self._process.is_alive():
            self._process.kill()
            await asyncio.to_thread(self._process.join, 2.0)
        self._closing.set()
        for reader in self._readers:
            await asyncio.to_thread(reader.join, 2.0)
        self._mark_exited()
        for q in (self._commands, self._control, self._telemetry):
            q.close()
            q.cancel_join_thread()

    # --- тестовые хуки -------------------------------------------------------------

    def pause_telemetry(self, paused: bool = True) -> None:
        """Перестать читать телеметрию (эмуляция медленного читателя в тестах)."""
        if paused:
            self._telemetry_paused.set()
        else:
            self._telemetry_paused.clear()

    # --- reader-нити ----------------------------------------------------------------

    def _read_control(self) -> None:
        self._read_loop(self._control, paused=None)

    def _read_telemetry(self) -> None:
        self._read_loop(self._telemetry, paused=self._telemetry_paused)

    def _read_loop(self, source: multiprocessing.Queue[Any], paused: threading.Event | None) -> None:
        while True:
            if paused is not None and paused.is_set():
                if self._closing.wait(0.1):
                    return
                continue
            try:
                message = source.get(timeout=0.2)
            except queue.Empty:
                if self._closing.is_set():
                    return
                if not self._process.is_alive() and self._process.exitcode is not None:
                    self._post(self._mark_exited)
                    if source is self._control:
                        return
                continue
            except (EOFError, OSError, ValueError):
                return  # очередь закрыта
            self._post(self._dispatch, message)

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # loop уже закрыт
            loop.call_soon_threadsafe(callback, *args)

    # --- в event loop ----------------------------------------------------------------

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "reply":
            future = self._pending.get(message.get("id", -1))
            if future is not None and not future.done():
                if message.get("ok"):
                    future.set_result(message.get("result"))
                else:
                    future.set_exception(WorkerCommandError(str(message.get("error"))))
            return
        if kind == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
            return
        if kind == "error" and message.get("fatal") and self._ready is not None and not self._ready.done():
            self._ready.set_exception(WorkerStartError(str(message.get("message"))))
        if kind == "stats":
            self.telemetry_dropped = int(message.get("telemetry_dropped", 0))
        self._emit(message)

    def _mark_exited(self) -> None:
        if self._exited:
            return
        self._exited = True
        gone = WorkerGoneError("процесс воркера завершился")
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(WorkerStartError("процесс воркера завершился до ready"))
        for future in self._pending.values():
            if not future.done():
                future.set_exception(gone)
        self._emit(
            {
                "type": "exited",
                "exitcode": self._process.exitcode,
                "expected": self._stopping,
            }
        )

    def _emit(self, message: dict[str, Any]) -> None:
        if self._on_message is not None:
            self._on_message(message)
