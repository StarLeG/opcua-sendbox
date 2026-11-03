"""Конфигурация сервиса из переменных окружения.

Секретов нет. Все переменные и их дефолты — таблица «Env» в CLAUDE.md и
`.env.example`; единственный источник дефолтов — значения полей `Config`.
Несогласованная конфигурация (пересекающиеся порты, нулевые лимиты) отклоняется
при создании: сервис не стартует молча с бессмысленными настройками.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} должна быть целым числом, получено: {raw!r}") from None


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


@dataclass(frozen=True)
class Config:
    """Настройки runtime; значения по умолчанию — для продакшена."""

    #: Хост, который сервер объявляет клиентам в endpoint (ADR-002 п. 1).
    OPCUA_PUBLIC_HOST: str = "opcua-sendbox.electroman.by"
    #: Адрес прослушивания OPC UA; пусто — все интерфейсы, IPv4 и IPv6 (ADR-002).
    OPCUA_BIND_HOST: str = ""
    #: Порт общего демо-сервера (4840 — стандарт OPC UA; на машине с LDS занят).
    OPCUA_SHARED_PORT: int = 4840
    #: Диапазон портов личных сессий (включительно).
    OPCUA_SESSION_PORT_MIN: int = 4841
    OPCUA_SESSION_PORT_MAX: int = 5000
    #: Лимиты сессий.
    SESSIONS_PER_KEY: int = 2
    SESSIONS_PER_IP: int = 4
    SESSIONS_TOTAL: int = 50
    #: Лимиты содержимого сессии.
    TAGS_PER_SESSION: int = 100
    ALARMS_PER_SESSION: int = 20
    #: Закрытие сессии: простой без API-вызовов и OPC UA клиентов; потолок жизни.
    SESSION_IDLE_TIMEOUT_S: int = 1800
    SESSION_MAX_LIFETIME_S: int = 14400
    #: Глубина истории каждого тега (значений).
    HISTORY_SIZE: int = 1000
    #: Ожидание `ready` от запущенного воркера.
    WORKER_START_TIMEOUT_S: int = 10
    #: Частота снимков значений тегов (IPC и WebSocket).
    WS_STATE_HZ: int = 2
    #: Мутирующие действия из UI/REST на IP в минуту (как ADR-012 Modbus SendBox).
    WEB_MAX_WRITE_PER_MIN_PER_IP: int = 50
    #: Выдача API-ключей на IP в час.
    KEYS_PER_HOUR_PER_IP: int = 10

    def __post_init__(self) -> None:
        for spec in fields(self):
            value = getattr(self, spec.name)
            if isinstance(value, int) and value <= 0:
                raise ValueError(f"{spec.name} должна быть > 0, получено: {value}")
        for name in ("OPCUA_SHARED_PORT", "OPCUA_SESSION_PORT_MIN", "OPCUA_SESSION_PORT_MAX"):
            port = getattr(self, name)
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} вне диапазона 1..65535: {port}")
        if self.OPCUA_SESSION_PORT_MIN > self.OPCUA_SESSION_PORT_MAX:
            raise ValueError(
                "OPCUA_SESSION_PORT_MIN больше OPCUA_SESSION_PORT_MAX: "
                f"{self.OPCUA_SESSION_PORT_MIN} > {self.OPCUA_SESSION_PORT_MAX}"
            )
        if self.OPCUA_SESSION_PORT_MIN <= self.OPCUA_SHARED_PORT <= self.OPCUA_SESSION_PORT_MAX:
            raise ValueError(
                f"порт общего сервера {self.OPCUA_SHARED_PORT} внутри диапазона личных сессий"
            )
        if self.SESSIONS_PER_KEY > self.SESSIONS_PER_IP:
            raise ValueError("SESSIONS_PER_KEY не может быть больше SESSIONS_PER_IP")
        if self.SESSION_IDLE_TIMEOUT_S > self.SESSION_MAX_LIFETIME_S:
            raise ValueError("SESSION_IDLE_TIMEOUT_S не может быть больше SESSION_MAX_LIFETIME_S")
        if not self.OPCUA_PUBLIC_HOST:
            raise ValueError("OPCUA_PUBLIC_HOST не может быть пустым")

    @property
    def session_port_count(self) -> int:
        """Сколько портов в диапазоне личных сессий."""
        return self.OPCUA_SESSION_PORT_MAX - self.OPCUA_SESSION_PORT_MIN + 1

    @property
    def bind_host(self) -> str | None:
        """Хост для `Server.socket_address`: None — все интерфейсы (IPv4 и IPv6)."""
        return self.OPCUA_BIND_HOST or None

    def endpoint_url(self, port: int) -> str:
        """Публичный endpoint сервера на порту `port`."""
        host = self.OPCUA_PUBLIC_HOST
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6-литерал в URL — в квадратных скобках
        return f"opc.tcp://{host}:{port}/electroman/"

    @classmethod
    def from_env(cls) -> Config:
        """Создать конфигурацию из env; не заданные переменные берут дефолт."""
        base = cls()
        values: dict[str, object] = {}
        for spec in fields(cls):
            default = getattr(base, spec.name)
            if isinstance(default, int):
                values[spec.name] = _env_int(spec.name, default)
            else:
                values[spec.name] = _env_str(spec.name, default)
        return cls(**values)  # type: ignore[arg-type]
