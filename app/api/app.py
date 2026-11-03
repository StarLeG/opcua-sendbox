"""FastAPI-приложение: фабрика и lifespan.

lifespan запускает общий сервер и reaper (`SessionManager.start`) и при остановке
закрывает все сессии. Тесты передают готовые сервисы и `start_manager=False`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health_router, router
from app.api.services import AppServices, build_services
from app.api.ws import ws_router
from app.config import Config


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
            "Получите ключ через POST /api/keys, создайте личный сервер через "
            "POST /api/sessions и подключите к выданному endpoint свой клиент: "
            "UaExpert, SCADA, ModbusToolkit. Общий демо-сервер работает всегда, "
            "его адрес отдаёт GET /api/shared."
        ),
        lifespan=lifespan,
        docs_url="/api/swagger",
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
    app.include_router(router)
    app.include_router(health_router)
    app.include_router(ws_router)
    return app
