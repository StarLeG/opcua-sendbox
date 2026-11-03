"""REST API на поддельных воркерах: эндпоинты, коды ошибок, ключи, лимиты."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.api.rate_limit import IpRateLimiter
from app.api.services import AppServices
from app.api.stats import RequestStats
from tests.fakes import Env


class Api:
    def __init__(self, env: Env, client: httpx.AsyncClient) -> None:
        self.env = env
        self.client = client

    async def key(self) -> dict[str, str]:
        response = await self.client.post("/api/keys")
        assert response.status_code == 201
        return {"X-API-Key": response.json()["key"]}

    async def session(self, headers: dict[str, str], body: Any = None) -> dict[str, Any]:
        response = await self.client.post("/api/sessions", headers=headers, json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())


async def make_api(env: Env, action_limit: int = 1000) -> AsyncIterator[Api]:
    services = AppServices(
        config=env.config,
        manager=env.manager,
        action_limiter=IpRateLimiter(action_limit),
        stats=RequestStats(),
    )
    app = create_app(services=services, start_manager=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(env, client)
    await env.manager.close()


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async for value in make_api(Env()):
        yield value


async def test_key_then_session_lifecycle(api: Api) -> None:
    headers = await api.key()
    created = await api.session(headers)
    assert created["state"] == "running"
    assert created["endpoint_url"].startswith("opc.tcp://opcua-sendbox.electroman.by:")
    assert created["web_ui_url"] == f"/session/{created['id']}"
    assert created["created_at"].endswith("Z") and created["expires_at"] > created["created_at"]
    assert created["idle_closes_at"] is not None  # клиентов нет — таймер простоя идёт

    listed = await api.client.get("/api/sessions", headers=headers)
    assert [s["id"] for s in listed.json()] == [created["id"]]

    detail = await api.client.get(f"/api/sessions/{created['id']}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert "Sine" in body["values"] and {a["name"] for a in body["alarms"]} == {"SineHigh", "Heartbeat"}

    deleted = await api.client.delete(f"/api/sessions/{created['id']}", headers=headers)
    assert deleted.status_code == 204
    again = await api.client.delete(f"/api/sessions/{created['id']}", headers=headers)
    assert again.status_code == 404


async def test_auth_required_and_unknown_key(api: Api) -> None:
    assert (await api.client.post("/api/sessions")).status_code == 401
    response = await api.client.get("/api/sessions", headers={"X-API-Key": "made-up-key"})
    assert response.status_code == 401
    assert "POST /api/keys" in response.json()["detail"]


async def test_foreign_session_is_404(api: Api) -> None:
    owner, stranger = await api.key(), await api.key()
    created = await api.session(owner)
    for method, url in (
        ("GET", f"/api/sessions/{created['id']}"),
        ("DELETE", f"/api/sessions/{created['id']}"),
        ("POST", f"/api/sessions/{created['id']}/alarms/Heartbeat/trigger"),
    ):
        response = await api.client.request(method, url, headers=stranger)
        assert response.status_code == 404, (method, url)


async def test_custom_content_and_validation(api: Api) -> None:
    headers = await api.key()
    body = {"tags": [{"name": "Flow", "kind": "sine", "data_type": "Float"}], "alarms": []}
    created = await api.session(headers, body)
    detail = (await api.client.get(f"/api/sessions/{created['id']}", headers=headers)).json()
    assert [t["name"] for t in detail["tags"]] == ["Flow"]

    bad = await api.client.post("/api/sessions", headers=headers, json={"tags": [{"name": "1bad"}]})
    assert bad.status_code == 422
    dangling = await api.client.post(
        "/api/sessions",
        headers=headers,
        json={"tags": [], "alarms": [{"name": "A", "tag": "Missing", "high": 1}]},
    )
    assert dangling.status_code == 422
    assert "несуществующий" in dangling.json()["detail"]


async def test_tags_and_alarms_endpoints(api: Api) -> None:
    headers = await api.key()
    sid = (await api.session(headers))["id"]
    base = f"/api/sessions/{sid}"

    added = await api.client.post(f"{base}/tags", headers=headers, json={"name": "Level", "kind": "triangle"})
    assert added.status_code == 201 and added.json()["name"] == "Level"
    duplicate = await api.client.post(f"{base}/tags", headers=headers, json={"name": "Level"})
    assert duplicate.status_code == 422  # проверка набора до воркера: имя встречается дважды

    alarm = {"name": "LevelHigh", "kind": "limit", "tag": "Level", "high": 40}
    assert (await api.client.post(f"{base}/alarms", headers=headers, json=alarm)).status_code == 201
    in_use = await api.client.delete(f"{base}/tags/Level", headers=headers)
    assert in_use.status_code == 409

    trigger = await api.client.post(f"{base}/alarms/LevelHigh/trigger", headers=headers, json={"active": True})
    assert trigger.json() == {"alarm": "LevelHigh", "active": True}
    toggled = await api.client.post(f"{base}/alarms/LevelHigh/trigger", headers=headers)
    assert toggled.json()["active"] is False
    missing = await api.client.post(f"{base}/alarms/Nope/trigger", headers=headers)
    assert missing.status_code == 409

    assert (await api.client.delete(f"{base}/alarms/LevelHigh", headers=headers)).status_code == 204
    assert (await api.client.delete(f"{base}/tags/Level", headers=headers)).status_code == 204
    gone = await api.client.delete(f"{base}/tags/Level", headers=headers)
    assert gone.status_code == 409


async def test_tag_limit_counts_existing() -> None:
    async for api in make_api(Env(TAGS_PER_SESSION=2)):
        headers = await api.key()
        sid = (await api.session(headers, {"tags": [{"name": "A"}], "alarms": []}))["id"]
        added = await api.client.post(f"/api/sessions/{sid}/tags", headers=headers, json={"name": "B"})
        assert added.status_code == 201
        over = await api.client.post(f"/api/sessions/{sid}/tags", headers=headers, json={"name": "C"})
        assert over.status_code == 422 and "лимита" in over.json()["detail"]


async def test_session_limit_is_429(api: Api) -> None:
    headers = await api.key()
    await api.session(headers)
    await api.session(headers)
    third = await api.client.post("/api/sessions", headers=headers)
    assert third.status_code == 429


async def test_start_failure_is_503(api: Api) -> None:
    headers = await api.key()
    api.env.fail = True
    response = await api.client.post("/api/sessions", headers=headers)
    assert response.status_code == 503
    assert api.env.manager.sessions() == []


async def test_crashed_session_is_404(api: Api) -> None:
    headers = await api.key()
    sid = (await api.session(headers))["id"]
    api.env.handles[-1].crash()
    response = await api.client.get(f"/api/sessions/{sid}", headers=headers)
    assert response.status_code == 404


async def test_action_rate_limit() -> None:
    async for api in make_api(Env(SESSIONS_PER_KEY=4), action_limit=2):
        headers = await api.key()
        await api.session(headers)
        await api.session(headers)
        limited = await api.client.post("/api/sessions", headers=headers)
        assert limited.status_code == 429
        assert "слишком много изменений" in limited.json()["detail"]
        assert (await api.client.get("/api/sessions", headers=headers)).status_code == 200  # чтение не считается


async def test_key_issue_limit() -> None:
    async for api in make_api(Env(KEYS_PER_HOUR_PER_IP=2)):
        await api.key()
        await api.key()
        assert (await api.client.post("/api/keys")).status_code == 429


async def test_shared_and_healthz(api: Api) -> None:
    shared = (await api.client.get("/api/shared")).json()
    assert shared["endpoint_url"] == "opc.tcp://opcua-sendbox.electroman.by:100/electroman/"
    assert shared["read_only"] is True
    health = await api.client.get("/healthz")
    assert health.status_code == 503  # общий сервер в этом тесте не запущен
    assert health.json()["shared"] == "down"
    await api.env.manager.start_shared()
    health = await api.client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok" and health.json()["ports_free"] == 20


async def test_stats_counts_http_requests(api: Api) -> None:
    await api.key()  # 201
    assert (await api.client.get("/api/nope")).status_code == 404
    body = (await api.client.get("/api/stats")).json()
    assert body["requests_total"] == 2  # ключ и 404; сам запрос /api/stats ещё не учтён
    assert body["errors_total"] == 1  # ошибкой считается статус 400 и выше
    assert body["clients"] == 0
    assert body["uptime_s"] >= 0
    assert body["requests_per_s"] >= 0 and body["errors_per_s"] >= 0


async def test_openapi_documents_error_model(api: Api) -> None:
    schema = (await api.client.get("/api/openapi.json")).json()
    post = schema["paths"]["/api/v1/sessions"]["post"]
    assert {"401", "429", "503"} <= set(post["responses"])
    assert "/api/sessions" not in schema["paths"]  # алиас /api/... скрыт из схемы (ADR-003)
    assert (await api.client.get("/api/docs")).status_code == 200


async def test_capabilities_and_api_alias(api: Api) -> None:
    """Контракт /api/v1/... и алиас /api/... отдают одно и то же (TASK-204, ADR-003)."""
    for path in ("/api/capabilities", "/api/v1/capabilities"):
        response = await api.client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["service"] == "opcua-sendbox"
        assert body["api_version"] == 1
        assert body["version"] == (await api.client.get("/healthz")).json()["version"]
        assert "alarms" in body["features"] and "shared_server" in body["features"]
        assert "security_basic256sha256" in body["features"] and "username_identity" in body["features"]
        assert body["limits"] == {
            "sessions_per_key": 2,
            "sessions_per_ip": 4,
            "sessions_total": 10,
            "tags_per_session": 100,
            "alarms_per_session": 20,
            "idle_timeout_s": 60,
            "max_lifetime_s": 600,
        }
    for path in ("/api/keys", "/api/v1/keys"):
        created = await api.client.post(path)
        assert created.status_code == 201, path
