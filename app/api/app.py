"""FastAPI-приложение: фабрика и lifespan.

lifespan запускает общий сервер и reaper (`SessionManager.start`) и при остановке
закрывает все сессии. Тесты передают готовые сервисы и `start_manager=False`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from app.api.pages import pages_router
from app.api.routes import health_router, router
from app.api.services import AppServices, build_services
from app.api.ws import ws_router
from app.config import Config

#: Статика UI (CSS, JS, шрифты).
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    config: Config | None = None,
    services: AppServices | None = None,
    *,
    start_manager: bool = True,
) -> FastAPI:
    """Создать приложение (uvicorn: `app.api.app:create_app --factory`)."""
    if services is None:
        services = build_services(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_manager:
            await services.manager.start()
        try:
            yield
        finally:
            if start_manager:
                await services.manager.close()

    app = FastAPI(
        title="OPC UA SendBox API",
        description=(
            "REST API OPC UA SendBox - публичной песочницы OPC UA серверов. "
            "Контракт версионированный: /api/v1/... (пути /api/... работают как совместимый "
            "алиас). Получите ключ через POST /api/v1/keys, создайте личный сервер через "
            "POST /api/v1/sessions и подключите к выданному endpoint свой клиент: "
            "UaExpert, SCADA, ModbusToolkit. Общий демо-сервер работает всегда, "
            "его адрес отдаёт GET /api/v1/shared, возможности — GET /api/v1/capabilities."
        ),
        lifespan=lifespan,
        # Swagger — своя страница /api/docs в оформлении сайта (pages.py).
        docs_url=None,
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {"name": "Ключи", "description": "API-ключ без регистрации."},
            {"name": "Сессии", "description": "Личные OPC UA серверы."},
            {"name": "Теги", "description": "Переменные с генераторами значений."},
            {"name": "Алармы", "description": "События Alarms & Conditions."},
            {"name": "Общий сервер", "description": "Постоянный демо-сервер только для чтения."},
            {"name": "Служебное", "description": "Здоровье процесса."},
        ],
    )
    app.state.services = services

    @app.middleware("http")
    async def _count_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Считать HTTP-ответы для счётчиков сервера (TASK-205)."""
        try:
            response = await call_next(request)
        except Exception:
            services.stats.hit(500)
            raise
        services.stats.hit(response.status_code)
        return response

    @app.middleware("http")
    async def _revalidate_static(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Статика UI — всегда сверять с сервером (TASK-208).

        Без Cache-Control браузер кэширует CSS/JS по эвристике надолго и после
        выкладки продолжает показывать старый интерфейс. `no-cache` означает
        «кэшируй, но сверяй»: файл уходит с ETag/Last-Modified, неизменённый
        возвращается как 304 без тела.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Двойной префикс: /api/v1 — канонический контракт в OpenAPI, /api — совместимый
    # алиас для UI, README и старых скриптов (ADR-003).
    app.include_router(router, prefix="/api/v1")
    app.include_router(router, prefix="/api", include_in_schema=False)
    app.include_router(health_router)
    app.include_router(ws_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
