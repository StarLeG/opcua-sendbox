"""Контейнер сервисов web-слоя (как AppServices в Modbus SendBox)."""
from __future__ import annotations

from dataclasses import dataclass

from app.api.rate_limit import IpRateLimiter
from app.config import Config
from app.opcua.session_manager import SessionManager


@dataclass
class AppServices:
    config: Config
    manager: SessionManager
    action_limiter: IpRateLimiter


def build_services(config: Config | None = None, manager: SessionManager | None = None) -> AppServices:
    cfg = config if config is not None else Config.from_env()
    return AppServices(
        config=cfg,
        manager=manager if manager is not None else SessionManager(cfg),
        action_limiter=IpRateLimiter(cfg.WEB_MAX_WRITE_PER_MIN_PER_IP),
    )
