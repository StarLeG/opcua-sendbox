"""Фикстуры интеграционных тестов: порты тестового диапазона, сервер в процессе теста.

Порты выдаёт PortAllocator из 15000–15100: диапазон не пересекается с продовым
4840–5000 и с LDS на 4840, занятые порты пропускаются пробой bind.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from asyncua import Server

from app.opcua.address_space import AddressSpace, create_server
from app.opcua.port_allocator import PortAllocator

TEST_PORT_MIN = 15000
TEST_PORT_MAX = 15100
PUBLIC_HOST = "opcua-sendbox.test"

_allocator = PortAllocator(TEST_PORT_MIN, TEST_PORT_MAX)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Все тесты каталога integration помечаются маркером `integration`."""
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def port() -> Iterator[int]:
    value = _allocator.acquire()
    try:
        yield value
    finally:
        _allocator.release(value)


@dataclass
class Sandbox:
    """Сервер с адресным пространством в процессе теста."""

    server: Server
    space: AddressSpace
    port: int
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"opc.tcp://127.0.0.1:{self.port}/electroman/"


async def make_sandbox(port: int, *, read_only: bool = False) -> Sandbox:
    server = await create_server(
        endpoint_url=f"opc.tcp://{PUBLIC_HOST}:{port}/electroman/",
        port=port,
        bind_host=None,
    )
    events: list[dict[str, Any]] = []
    space = AddressSpace(server, history_size=100, read_only=read_only, on_event=events.append)
    await space.setup()
    return Sandbox(server=server, space=space, port=port, events=events)


@pytest.fixture
async def sandbox(port: int) -> AsyncIterator[Sandbox]:
    """Пустая песочница, сервер запущен; теги и алармы добавляет тест."""
    box = await make_sandbox(port)
    async with box.server:
        yield box
        await box.space.stop()
