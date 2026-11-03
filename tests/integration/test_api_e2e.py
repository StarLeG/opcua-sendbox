"""Сквозной сценарий ТЗ: REST API → настоящий процесс OPC UA → клиент asyncua."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from asyncua import Client, ua

from app.api.app import create_app
from app.api.services import build_services
from app.config import Config
from app.opcua.session_manager import SessionManager
from tests.integration.conftest import TEST_CERTS_DIR


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    config = replace(
        Config(),
        OPCUA_PUBLIC_HOST="localhost",
        OPCUA_SHARED_PORT=15059,
        OPCUA_SESSION_PORT_MIN=15060,
        OPCUA_SESSION_PORT_MAX=15100,
        OPCUA_CERTS_DIR=str(TEST_CERTS_DIR),
        WORKER_START_TIMEOUT_S=30,
    )
    manager = SessionManager(config)
    app = create_app(services=build_services(config, manager), start_manager=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await manager.close()


async def test_create_session_via_api_and_read_changing_value(http: httpx.AsyncClient) -> None:
    key = (await http.post("/api/keys")).json()["key"]
    headers = {"X-API-Key": key}
    created = await http.post("/api/sessions", headers=headers)
    assert created.status_code == 201, created.text
    session: dict[str, Any] = created.json()

    async with Client(session["endpoint_url"]) as client:
        counter = client.get_node(ua.NodeId("Tags.Counter", 2))
        first = await counter.read_value()
        await asyncio.sleep(1.3)
        assert await counter.read_value() > first

        added = await http.post(
            f"/api/sessions/{session['id']}/tags",
            headers=headers,
            json={"name": "FromApi", "kind": "constant", "data_type": "Int32", "params": {"value": 7}},
        )
        assert added.status_code == 201
        assert await client.get_node(ua.NodeId("Tags.FromApi", 2)).read_value() == 7

        detail = (await http.get(f"/api/sessions/{session['id']}", headers=headers)).json()
        assert detail["clients"] >= 0 and "FromApi" in detail["values"]

    deleted = await http.delete(f"/api/sessions/{session['id']}", headers=headers)
    assert deleted.status_code == 204
    with pytest.raises((OSError, asyncio.TimeoutError, TimeoutError, ua.UaError)):
        async with Client(session["endpoint_url"], timeout=2):
            pass


def test_websocket_streams_values_from_real_worker() -> None:
    """Снимки значений из настоящего процесса доходят до WebSocket UI."""
    from starlette.testclient import TestClient

    config = replace(
        Config(),
        OPCUA_PUBLIC_HOST="localhost",
        OPCUA_SHARED_PORT=15059,
        OPCUA_SESSION_PORT_MIN=15060,
        OPCUA_SESSION_PORT_MAX=15100,
        WORKER_START_TIMEOUT_S=30,
    )
    manager = SessionManager(config)
    app = create_app(services=build_services(config, manager), start_manager=False)
    with TestClient(app) as client:
        key = client.post("/api/keys").json()["key"]
        sid = client.post("/api/sessions", headers={"X-API-Key": key}).json()["id"]
        try:
            with client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as ws:
                assert ws.receive_json()["type"] == "hello"
                frame = ws.receive_json()
                while frame["type"] != "tag_values":
                    frame = ws.receive_json()
                assert "Sine" in frame["values"] and "SineHigh" in frame["alarms"]
        finally:
            assert client.delete(f"/api/sessions/{sid}", headers={"X-API-Key": key}).status_code == 204
            assert client.portal is not None
            client.portal.call(manager.close)
