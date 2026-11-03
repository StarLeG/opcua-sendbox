"""SecurityPolicy Basic256Sha256 и демо-учётка на настоящем сервере (ADR-004).

Имена `test_demo_user_manager_accepts_anonymous` и `test_bad_password_rejected`
зафиксированы в ADR-000 как гарды приватного API `InternalServer.set_user_manager`.
"""
from __future__ import annotations

import pytest
from asyncua import Client, ua

from app.opcua.tags import TagSpec
from tests.integration.conftest import Sandbox, make_client_cert

DEMO_USER = "sendbox"
DEMO_PASSWORD = "sendbox"


async def test_demo_user_manager_accepts_anonymous(sandbox: Sandbox) -> None:
    """Анонимный токен проходит наравне с демо-учёткой (ADR-004)."""
    await sandbox.space.add_tag(TagSpec(name="C", kind="constant", params={"value": 3}))
    async with Client(sandbox.url) as client:
        assert await client.get_node(sandbox.space.tag_node_id("C")).read_value() == 3.0


async def test_bad_password_rejected(sandbox: Sandbox) -> None:
    """Неверный пароль отклоняется на ActivateSession как BadUserAccessDenied."""
    cert_file, key_file = await make_client_cert()
    client = Client(sandbox.url)
    await client.set_security_string(f"Basic256Sha256,SignAndEncrypt,{cert_file},{key_file}")
    client.set_user(DEMO_USER)
    client.set_password("wrong")
    with pytest.raises(ua.UaStatusCodeError) as excinfo:
        await client.connect()
    assert "BadUserAccessDenied" in str(excinfo.value)
    client.disconnect_socket()


async def test_basic256sha256_sign_and_encrypt_login_reads_tag(sandbox: Sandbox) -> None:
    """SignAndEncrypt с демо-учёткой: клиент читает тег по шифрованному каналу."""
    await sandbox.space.add_tag(TagSpec(name="C", kind="constant", params={"value": 5}))
    cert_file, key_file = await make_client_cert()
    client = Client(sandbox.url)
    await client.set_security_string(f"Basic256Sha256,SignAndEncrypt,{cert_file},{key_file}")
    client.set_user(DEMO_USER)
    client.set_password(DEMO_PASSWORD)
    async with client:
        assert await client.get_node(sandbox.space.tag_node_id("C")).read_value() == 5.0


async def test_basic256sha256_sign_mode_works(sandbox: Sandbox) -> None:
    """Режим Sign работает: UaExpert часто выбирает его, а не SignAndEncrypt (ADR-004)."""
    await sandbox.space.add_tag(TagSpec(name="C", kind="constant", params={"value": 7}))
    cert_file, key_file = await make_client_cert()
    client = Client(sandbox.url)
    await client.set_security_string(f"Basic256Sha256,Sign,{cert_file},{key_file}")
    client.set_user(DEMO_USER)
    client.set_password(DEMO_PASSWORD)
    async with client:
        assert await client.get_node(sandbox.space.tag_node_id("C")).read_value() == 7.0
