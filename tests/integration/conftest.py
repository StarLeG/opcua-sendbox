"""Фикстуры интеграционных тестов: порты тестового диапазона, сервер в процессе теста.

Порты выдаёт PortAllocator из 15000–15058: диапазон не пересекается с продовым
4840–5000 и с LDS на 4840 (15059–15100 — тестам менеджера сессий), занятые
порты пропускаются пробой bind.
"""
from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from asyncua import Server
from asyncua.crypto import cert_gen
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.opcua.address_space import AddressSpace, create_server
from app.opcua.port_allocator import PortAllocator
from app.opcua.security import ServerSecurity

TEST_PORT_MIN = 15000
TEST_PORT_MAX = 15058  # 15059 — общий сервер, 15060–15100 — сессии менеджера
PUBLIC_HOST = "opcua-sendbox.test"

#: Каталог сертификатов тестов: `ensure_server_cert` генерирует файлы один раз на прогон.
TEST_CERTS_DIR = Path(tempfile.mkdtemp(prefix="sendbox-certs-"))
#: Шифрование и демо-учётки тестов (ADR-004): те же, что в проде, сертификаты — во временном каталоге.
TEST_SECURITY = ServerSecurity(TEST_CERTS_DIR, "sendbox", "sendbox")

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


async def make_client_cert() -> tuple[Path, Path]:
    """Самоподписанный сертификат клиента для Basic256Sha256: один раз на прогон."""
    key_file = TEST_CERTS_DIR / "client_key.pem"
    cert_file = TEST_CERTS_DIR / "client_cert.der"
    if not (key_file.exists() and cert_file.exists()):
        await cert_gen.setup_self_signed_certificate(
            key_file,
            cert_file,
            app_uri="urn:electroman:opcua-sendbox:test-client",
            host_name="localhost",
            cert_use=[ExtendedKeyUsageOID.CLIENT_AUTH],
            subject_attrs={"commonName": "OPC UA SendBox test client"},
        )
    return cert_file, key_file


async def make_sandbox(port: int, *, read_only: bool = False) -> Sandbox:
    server = await create_server(
        endpoint_url=f"opc.tcp://{PUBLIC_HOST}:{port}/electroman/",
        port=port,
        bind_host=None,
        security=TEST_SECURITY,
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
