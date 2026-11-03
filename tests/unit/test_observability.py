"""Метрики Prometheus, /metrics, healthcheck, синхронность .env.example с Config."""
from __future__ import annotations

import importlib.util
import re
import socket
import threading
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.api.prometheus import MEDIA_TYPE, render_prometheus
from app.api.rate_limit import IpRateLimiter
from app.api.services import AppServices
from app.config import Config
from app.opcua.session_manager import NoCapacityError
from tests.fakes import Env

ROOT = Path(__file__).resolve().parents[2]


def _load_healthcheck() -> Any:
    """scripts/ — не пакет: загружаем скрипт по пути."""
    spec = importlib.util.spec_from_file_location("healthcheck", ROOT / "scripts" / "healthcheck.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


healthcheck = _load_healthcheck()


def sample(text: str, name: str, labels: str = "") -> float:
    match = re.search(rf"^{re.escape(name + labels)} (\S+)$", text, re.MULTILINE)
    assert match is not None, (name, labels)
    return float(match.group(1))


async def test_metrics_reflect_manager_state() -> None:
    env = Env()
    m = env.manager
    text = render_prometheus(m)
    assert sample(text, "sendbox_sessions_active") == 0
    assert sample(text, "sendbox_ports_free") == 20
    assert sample(text, "sendbox_shared_up") == 0

    session = await m.create("k", "ip")
    env.handles[0].on_message({"type": "client_count", "clients": 2})
    session.handle.telemetry_dropped = 7
    env.fail = True
    with pytest.raises(NoCapacityError):
        await m.create("k2", "ip")
    text = render_prometheus(m)
    assert sample(text, "sendbox_sessions_active") == 1
    assert sample(text, "sendbox_sessions_created_total") == 1
    assert sample(text, "sendbox_session_start_failures_total") == 1
    assert sample(text, "sendbox_ports_free") == 19
    assert sample(text, "sendbox_opcua_clients", '{server="sessions"}') == 2
    assert sample(text, "sendbox_ipc_telemetry_dropped_total", f'{{session="{session.id}"}}') == 7

    await m.delete(session.id, "k")
    text = render_prometheus(m)
    assert sample(text, "sendbox_sessions_closed_total", '{reason="deleted"}') == 1
    assert sample(text, "sendbox_sessions_closed_total", '{reason="idle"}') == 0


async def test_metrics_format_has_help_and_type_for_every_metric() -> None:
    text = render_prometheus(Env().manager)
    names = set(re.findall(r"^# TYPE (\S+) (?:gauge|counter)$", text, re.MULTILINE))
    helps = set(re.findall(r"^# HELP (\S+) ", text, re.MULTILINE))
    assert names == helps and len(names) == 11
    assert all(n.endswith("_total") for n in names if f"# TYPE {n} counter" in text)


async def test_metrics_endpoint() -> None:
    env = Env()
    services = AppServices(config=env.config, manager=env.manager, action_limiter=IpRateLimiter(10))
    app = create_app(services=services, start_manager=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == MEDIA_TYPE
    assert "sendbox_uptime_seconds" in response.text


def test_env_example_matches_config() -> None:
    """Каждое поле Config описано в .env.example с тем же дефолтом, и наоборот."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.MULTILINE))
    defaults = Config()
    for spec in fields(Config):
        assert spec.name in documented, spec.name
        assert documented[spec.name] == str(getattr(defaults, spec.name)), spec.name
    extra = set(documented) - {f.name for f in fields(Config)}
    assert extra == {"WEB_HOST", "WEB_PORT", "FORWARDED_ALLOW_IPS", "LOG_FORMAT", "LOG_LEVEL"}


class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.mark.parametrize(("status", "code"), [(200, 0), (503, 1)])
def test_healthcheck_http(status: int, code: int) -> None:
    handler = type("H", (_Handler,), {"status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        assert healthcheck.main(["--url", url]) == code
    finally:
        server.shutdown()


def test_healthcheck_tcp_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert healthcheck.check_tcp("127.0.0.1", port) is None
    assert healthcheck.check_tcp("127.0.0.1", port) is not None
    assert healthcheck.main(["--url", "http://127.0.0.1:1/healthz"]) == 1
