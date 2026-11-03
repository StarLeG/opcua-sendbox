"""REST API: ключи, сессии, теги, алармы, общий сервер, здоровье.

Коды ошибок (тело — `{"detail": "..."}` по-русски):
- 401 — нет ключа или ключ неизвестен (после перезапуска сервиса ключи сбрасываются);
- 404 — сессии нет или она чужая (не 403: не раскрываем, что такой id существует);
- 409 — воркер отклонил команду (имя занято, тег используется алармом…);
- 422 — неверное тело запроса или превышен лимит тегов/алармов;
- 429 — лимит сессий или действий с IP;
- 503 — нет свободных портов или сервер сессии не запустился.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response, status

from app.api.models import (
    AlarmStateOut,
    AlarmTrigger,
    ErrorOut,
    HealthOut,
    KeyOut,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
    SharedOut,
)
from app.api.services import AppServices
from app.opcua.keys import KeyInfo, KeyRateLimitError
from app.opcua.session_manager import (
    NoCapacityError,
    Session,
    SessionLimitError,
    SessionNotFoundError,
)
from app.opcua.tags import AlarmSpec, TagSpec, check_session_content
from app.opcua.worker_handle import WorkerCommandError, WorkerError, WorkerGoneError

router = APIRouter(prefix="/api")
health_router = APIRouter()

_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorOut} for code in (401, 404, 409, 422, 429, 503)
}


def get_services(request: Request) -> AppServices:
    services: AppServices = request.app.state.services
    return services


ServicesDep = Annotated[AppServices, Depends(get_services)]


def client_ip(request: Request) -> str:
    """IP клиента; за nginx uvicorn подставляет X-Forwarded-For (FORWARDED_ALLOW_IPS)."""
    return request.client.host if request.client else "unknown"


IpDep = Annotated[str, Depends(client_ip)]


def require_key(
    services: ServicesDep,
    x_api_key: Annotated[str | None, Header(description="Ключ из POST /api/keys")] = None,
) -> KeyInfo:
    info = services.manager.keys.check(x_api_key)
    if info is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "нужен API-ключ: получите его через POST /api/keys")
    return info


KeyDep = Annotated[KeyInfo, Depends(require_key)]


def limit_action(services: ServicesDep, ip: IpDep) -> str:
    """Одно окно на IP для всех изменений; превышение — 429."""
    if not services.action_limiter.allow(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "слишком много изменений, повторите позже")
    return ip


ActionDep = Annotated[str, Depends(limit_action)]


@contextmanager
def map_errors() -> Iterator[None]:
    """Ошибки ядра → HTTP-коды (см. docstring модуля)."""
    try:
        yield
    except SessionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except WorkerGoneError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "сессия закрыта: сервер сессии остановился") from exc
    except WorkerCommandError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except SessionLimitError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except (NoCapacityError, WorkerError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def session_out(session: Session, services: AppServices) -> SessionOut:
    """Монотонные времена менеджера → настенные (ISO UTC)."""
    config = services.config
    offset = time.time() - time.monotonic()
    idle = None
    if session.clients == 0:
        idle = _iso(session.last_activity + config.SESSION_IDLE_TIMEOUT_S + offset)
    return SessionOut(
        id=session.id,
        state=session.state.value,
        endpoint_url=session.endpoint_url,
        port=session.port,
        web_ui_url=f"/session/{session.id}",
        created_at=_iso(session.created + offset),
        expires_at=_iso(session.created + config.SESSION_MAX_LIFETIME_S + offset),
        idle_closes_at=idle,
        clients=session.clients,
    )


def _project_version() -> str:
    try:
        return package_version("opcua-sendbox")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0"


# --- ключи -------------------------------------------------------------------------------


@router.post("/keys", status_code=201, response_model=KeyOut, responses=_ERRORS, tags=["Ключи"])
def issue_key(services: ServicesDep, ip: IpDep) -> KeyOut:
    """Выдать API-ключ без регистрации. Ключ живёт в памяти сервиса."""
    try:
        info = services.manager.keys.issue(ip)
    except KeyRateLimitError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    return KeyOut(key=info.key)


# --- сессии ------------------------------------------------------------------------------


@router.post("/sessions", status_code=201, response_model=SessionOut, responses=_ERRORS, tags=["Сессии"])
async def create_session(
    services: ServicesDep,
    key: KeyDep,
    ip: ActionDep,
    body: Annotated[SessionCreate | None, Body()] = None,
) -> SessionOut:
    """Создать личный OPC UA сервер. Без тела — демо-набор тегов и алармов."""
    body = body or SessionCreate()
    with map_errors():
        session = await services.manager.create(key.key, ip, tags=body.tags, alarms=body.alarms)
    return session_out(session, services)


@router.get("/sessions", response_model=list[SessionOut], responses=_ERRORS, tags=["Сессии"])
def list_sessions(services: ServicesDep, key: KeyDep) -> list[SessionOut]:
    """Сессии этого ключа."""
    return [session_out(s, services) for s in services.manager.for_key(key.key)]


@router.get("/sessions/{session_id}", response_model=SessionDetailOut, responses=_ERRORS, tags=["Сессии"])
async def get_session(session_id: str, services: ServicesDep, key: KeyDep) -> SessionDetailOut:
    """Сессия с тегами, алармами и текущими значениями."""
    with map_errors():
        session = services.manager.get(session_id, key.key)
        described = await services.manager.request(session_id, key.key, "describe")
    base = session_out(session, services)
    return SessionDetailOut(
        **base.model_dump(),
        tags=[TagSpec.model_validate(t) for t in described["tags"]],
        alarms=[AlarmSpec.model_validate(a) for a in described["alarms"]],
        values=described["values"],
        alarm_states=described["alarm_states"],
    )


@router.delete("/sessions/{session_id}", status_code=204, responses=_ERRORS, tags=["Сессии"])
async def delete_session(session_id: str, services: ServicesDep, key: KeyDep, _ip: ActionDep) -> Response:
    """Остановить сервер сессии и освободить порт."""
    with map_errors():
        await services.manager.delete(session_id, key.key)
    return Response(status_code=204)


# --- теги и алармы --------------------------------------------------------------------------


async def _check_content(
    services: AppServices,
    session_id: str,
    key: str,
    *,
    new_tag: TagSpec | None = None,
    new_alarm: AlarmSpec | None = None,
) -> None:
    """Лимиты и ссылки с учётом уже существующих тегов и алармов."""
    described = await services.manager.request(session_id, key, "describe")
    tags = [TagSpec.model_validate(t) for t in described["tags"]]
    alarms = [AlarmSpec.model_validate(a) for a in described["alarms"]]
    if new_tag is not None:
        tags.append(new_tag)
    if new_alarm is not None:
        alarms.append(new_alarm)
    check_session_content(
        tags,
        alarms,
        max_tags=services.config.TAGS_PER_SESSION,
        max_alarms=services.config.ALARMS_PER_SESSION,
    )


@router.post("/sessions/{session_id}/tags", status_code=201, response_model=TagSpec, responses=_ERRORS, tags=["Теги"])
async def add_tag(session_id: str, spec: TagSpec, services: ServicesDep, key: KeyDep, _ip: ActionDep) -> TagSpec:
    """Добавить тег: узел `ns=2;s=Tags.<name>` появится сразу."""
    with map_errors():
        await _check_content(services, session_id, key.key, new_tag=spec)
        await services.manager.request(session_id, key.key, "add_tag", spec=spec.model_dump())
    return spec


@router.delete("/sessions/{session_id}/tags/{name}", status_code=204, responses=_ERRORS, tags=["Теги"])
async def remove_tag(session_id: str, name: str, services: ServicesDep, key: KeyDep, _ip: ActionDep) -> Response:
    """Удалить тег (сначала удалите алармы, которые на него ссылаются)."""
    with map_errors():
        await services.manager.request(session_id, key.key, "remove_tag", name=name)
    return Response(status_code=204)


@router.post(
    "/sessions/{session_id}/alarms", status_code=201, response_model=AlarmSpec, responses=_ERRORS, tags=["Алармы"]
)
async def add_alarm(
    session_id: str, spec: AlarmSpec, services: ServicesDep, key: KeyDep, _ip: ActionDep
) -> AlarmSpec:
    """Добавить аларм: limit по порогу тега, periodic по таймеру или manual."""
    with map_errors():
        await _check_content(services, session_id, key.key, new_alarm=spec)
        await services.manager.request(session_id, key.key, "add_alarm", spec=spec.model_dump())
    return spec


@router.delete("/sessions/{session_id}/alarms/{name}", status_code=204, responses=_ERRORS, tags=["Алармы"])
async def remove_alarm(
    session_id: str, name: str, services: ServicesDep, key: KeyDep, _ip: ActionDep
) -> Response:
    """Удалить аларм; активный перед удалением сбрасывается событием."""
    with map_errors():
        await services.manager.request(session_id, key.key, "remove_alarm", name=name)
    return Response(status_code=204)


@router.post(
    "/sessions/{session_id}/alarms/{name}/trigger",
    response_model=AlarmStateOut,
    responses=_ERRORS,
    tags=["Алармы"],
)
async def trigger_alarm(
    session_id: str,
    name: str,
    services: ServicesDep,
    key: KeyDep,
    _ip: ActionDep,
    body: Annotated[AlarmTrigger | None, Body()] = None,
) -> AlarmStateOut:
    """Активировать, сбросить или переключить аларм вручную — для проверки реакции SCADA."""
    active = (body or AlarmTrigger()).active
    with map_errors():
        result = await services.manager.request(session_id, key.key, "trigger_alarm", name=name, active=active)
    return AlarmStateOut(alarm=name, active=bool(result))


# --- общий сервер и служебное ------------------------------------------------------------------


@router.get("/shared", response_model=SharedOut, tags=["Общий сервер"])
def shared_info(services: ServicesDep) -> SharedOut:
    """Постоянный общий демо-сервер: ключ не нужен, только чтение."""
    shared = services.manager.shared
    return SharedOut(endpoint_url=shared.endpoint_url, status=shared.status, restarts=shared.restarts)


@health_router.get("/healthz", response_model=HealthOut, tags=["Служебное"])
def healthz(services: ServicesDep, response: Response) -> HealthOut:
    """Здоровье: 200, если общий сервер работает, иначе 503."""
    manager = services.manager
    shared = manager.shared.status
    if shared != "running":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(
        status="ok" if shared == "running" else "degraded",
        version=_project_version(),
        shared=shared,
        sessions=len(manager.sessions()),
        ports_free=manager.allocator.capacity - len(manager.allocator.in_use),
    )
