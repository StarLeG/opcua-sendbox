"""WebSocket: доступ по ключу, кадры, закрытие сессии, авария, медленный клиент."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import ws as ws_module
from app.api.app import create_app
from app.api.rate_limit import IpRateLimiter
from app.api.services import AppServices
from tests.fakes import Env


class Ctx:
    def __init__(self, env: Env, client: TestClient) -> None:
        self.env = env
        self.client = client

    def new_session(self) -> tuple[str, str]:
        key = self.client.post("/api/keys").json()["key"]
        created = self.client.post("/api/sessions", headers={"X-API-Key": key})
        assert created.status_code == 201
        return key, created.json()["id"]

    def inject(self, handle_index: int, message: dict[str, Any]) -> None:
        """Сообщение воркера — в event loop приложения, как от reader-нити."""
        handle = self.env.handles[handle_index]
        assert self.client.portal is not None
        self.client.portal.call(handle.on_message, message)


@pytest.fixture
def ctx() -> Iterator[Ctx]:
    env = Env()
    services = AppServices(config=env.config, manager=env.manager, action_limiter=IpRateLimiter(1000))
    app = create_app(services=services, start_manager=False)
    with TestClient(app) as client:
        yield Ctx(env, client)


def test_session_stream_hello_and_messages(ctx: Ctx) -> None:
    key, sid = ctx.new_session()
    with ctx.client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["session"]["id"] == sid
        ctx.inject(0, {"type": "tag_values", "values": {"Sine": 50.0}, "alarms": {}})
        ctx.inject(0, {"type": "reply", "id": 1, "ok": True})  # внутреннее — не уходит
        ctx.inject(0, {"type": "alarm", "alarm": "SineHigh", "active": True, "message": "Sine выше 80"})
        assert ws.receive_json()["values"] == {"Sine": 50.0}
        assert ws.receive_json()["alarm"] == "SineHigh"


def test_requires_key_and_ownership(ctx: Ctx) -> None:
    key, sid = ctx.new_session()
    other_key, _ = ctx.new_session()
    for url in (f"/ws/sessions/{sid}", f"/ws/sessions/{sid}?key={other_key}", f"/ws/sessions/nope?key={key}"):
        with pytest.raises(WebSocketDisconnect) as exc, ctx.client.websocket_connect(url) as ws:
            ws.receive_json()
        assert exc.value.code == ws_module.WS_CLOSE_POLICY, url


def test_session_delete_closes_stream(ctx: Ctx) -> None:
    key, sid = ctx.new_session()
    with ctx.client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as ws:
        ws.receive_json()  # hello
        assert ctx.client.delete(f"/api/sessions/{sid}", headers={"X-API-Key": key}).status_code == 204
        assert ws.receive_json() == {"type": "closed", "reason": "deleted"}
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == ws_module.WS_CLOSE_NORMAL


def test_crash_closes_with_1011(ctx: Ctx) -> None:
    key, sid = ctx.new_session()
    with ctx.client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as ws:
        ws.receive_json()
        assert ctx.client.portal is not None
        ctx.client.portal.call(ctx.env.handles[0].crash)
        kinds = []
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                kinds.append(ws.receive_json()["type"])
        assert "crashed" in kinds
        assert exc.value.code == ws_module.WS_CLOSE_SERVER_ERROR


def test_slow_client_disconnected_others_keep_receiving(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Медленный клиент отключается 1013, второй клиент продолжает получать кадры."""
    key, sid = ctx.new_session()
    session = ctx.env.manager.get(sid, key)
    with ctx.client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as fast:
        fast.receive_json()
        # второй клиент с крошечной очередью: продюсер переполнит её за один вызов
        monkeypatch.setattr(ws_module, "WS_CLIENT_QUEUE", 2)
        with ctx.client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as slow:
            assert ctx.client.portal is not None

            def burst() -> None:
                for n in range(10):
                    session.publish({"type": "tag_values", "values": {"n": n}})

            ctx.client.portal.call(burst)
            with pytest.raises(WebSocketDisconnect) as exc:
                while True:
                    slow.receive_json()
            assert exc.value.code == ws_module.WS_CLOSE_SLOW
        received = [fast.receive_json()["values"]["n"] for _ in range(10)]
        assert received == list(range(10))


def test_listener_removed_after_disconnect(ctx: Ctx) -> None:
    key, sid = ctx.new_session()
    session = ctx.env.manager.get(sid, key)
    with ctx.client.websocket_connect(f"/ws/sessions/{sid}?key={key}") as ws:
        ws.receive_json()
        assert len(session.listeners) == 1
    for _ in range(50):
        if not session.listeners:
            break
        assert ctx.client.portal is not None
        ctx.client.portal.call(lambda: None)
    assert session.listeners == set()


def test_shared_stream_without_key(ctx: Ctx) -> None:
    with ctx.client.websocket_connect("/ws/shared") as ws:
        hello = ws.receive_json()
        assert hello["shared"]["read_only"] is True
        assert ctx.client.portal is not None
        ctx.client.portal.call(
            ctx.env.manager._on_shared_message, {"type": "tag_values", "values": {"Sine": 1.0}}
        )
        assert ws.receive_json()["values"] == {"Sine": 1.0}
