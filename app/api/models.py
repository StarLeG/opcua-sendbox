"""Схемы REST API (ответы и тела запросов, которых нет в app.opcua.tags)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.opcua.tags import AlarmSpec, TagSpec


class ErrorOut(BaseModel):
    detail: str = Field(description="Что пошло не так, по-русски")


class KeyOut(BaseModel):
    key: str = Field(description="API-ключ: передавайте в заголовке X-API-Key")


class SessionCreate(BaseModel):
    """Тело POST /api/sessions. Без полей — демо-набор тегов и алармов."""

    tags: list[TagSpec] | None = Field(default=None, description="Теги; null — демо-набор")
    alarms: list[AlarmSpec] | None = Field(default=None, description="Алармы; null — демо-набор")


class SessionOut(BaseModel):
    id: str
    state: str = Field(description="starting | running | stopping")
    endpoint_url: str = Field(description="Адрес для OPC UA клиента")
    port: int
    web_ui_url: str = Field(description="Страница сессии в веб-интерфейсе")
    created_at: str = Field(description="Время создания, ISO 8601 UTC")
    expires_at: str = Field(description="Когда сессию закроет потолок жизни, ISO 8601 UTC")
    idle_closes_at: str | None = Field(
        description="Когда сессию закроет простой; null — пока подключены OPC UA клиенты"
    )
    clients: int = Field(description="Подключённые OPC UA сессии")


class SessionDetailOut(SessionOut):
    tags: list[TagSpec]
    alarms: list[AlarmSpec]
    values: dict[str, Any] = Field(description="Текущие значения тегов")
    alarm_states: dict[str, bool] = Field(description="Активен ли каждый аларм")


class AlarmTrigger(BaseModel):
    active: bool | None = Field(default=None, description="true/false — задать, null — переключить")


class AlarmStateOut(BaseModel):
    alarm: str
    active: bool


class SharedOut(BaseModel):
    endpoint_url: str
    status: str = Field(description="running | restarting | down")
    restarts: int
    read_only: bool = True


class HealthOut(BaseModel):
    status: str
    version: str
    shared: str
    sessions: int
    ports_free: int


class StatsOut(BaseModel):
    """Счётчики сервера для UI (TASK-205)."""

    clients: int = Field(description="OPC UA клиенты общего сервера и личных сессий")
    requests_per_s: float = Field(description="HTTP-запросы в секунду за окно 5 с")
    errors_per_s: float = Field(description="Ответы со статусом 400+ в секунду за окно 5 с")
    requests_total: int = Field(description="Всего HTTP-запросов с запуска")
    errors_total: int = Field(description="Всего ответов со статусом 400+ с запуска")
    uptime_s: float = Field(description="Время работы сервиса, с")


class CapabilitiesOut(BaseModel):
    """Возможности сервиса для честной деградации клиентов (TASK-204, ADR-003)."""

    service: str = Field(description="Имя сервиса")
    version: str = Field(description="Версия сервиса, SemVer")
    api_version: int = Field(description="Версия контракта REST API; путь /api/v{N}/...")
    features: list[str] = Field(
        description="Поддерживаемые возможности; отсутствие в списке = не поддерживается"
    )
    limits: dict[str, int] = Field(description="Лимиты песочницы (сессии, теги, алармы, таймауты)")
