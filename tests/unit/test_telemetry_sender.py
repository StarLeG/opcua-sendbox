"""TelemetrySender: drop-newest при переполнении, счётчик дропов (ADR-001 п. 5)."""
from __future__ import annotations

import multiprocessing
import queue

from app.opcua.worker import TelemetrySender


def test_drop_newest_when_full() -> None:
    target: queue.Queue[dict[str, int]] = queue.Queue(maxsize=2)
    sender = TelemetrySender(target)
    assert sender.send({"n": 1}) is True
    assert sender.send({"n": 2}) is True
    assert sender.send({"n": 3}) is False  # новое сообщение выброшено
    assert sender.dropped == 1
    assert [target.get_nowait()["n"] for _ in range(2)] == [1, 2]  # старые на месте
    assert sender.send({"n": 4}) is True  # место освободилось
    assert sender.dropped == 1


def test_never_blocks_on_multiprocessing_queue() -> None:
    target: multiprocessing.Queue[dict[str, int]] = multiprocessing.get_context("spawn").Queue(maxsize=1)
    sender = TelemetrySender(target)
    for n in range(50):
        sender.send({"n": n})  # put_nowait: ни один вызов не ждёт читателя
    assert sender.dropped >= 48
    target.close()
    target.cancel_join_thread()
