"""Config: дефолты, чтение env, отказ на несогласованных настройках."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Config


def test_defaults_are_consistent() -> None:
    config = Config()
    assert config.OPCUA_SHARED_PORT == 4840
    assert config.session_port_count == 160
    assert config.bind_host is None  # все интерфейсы, IPv4 и IPv6


def test_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPCUA_SHARED_PORT", "15000")
    monkeypatch.setenv("OPCUA_SESSION_PORT_MIN", "15001")
    monkeypatch.setenv("OPCUA_SESSION_PORT_MAX", "15100")
    monkeypatch.setenv("OPCUA_PUBLIC_HOST", " localhost ")
    monkeypatch.setenv("OPCUA_BIND_HOST", "127.0.0.1")
    config = Config.from_env()
    assert (config.OPCUA_SHARED_PORT, config.session_port_count) == (15000, 100)
    assert config.OPCUA_PUBLIC_HOST == "localhost"
    assert config.bind_host == "127.0.0.1"


def test_from_env_empty_value_means_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSIONS_TOTAL", "  ")
    assert Config.from_env().SESSIONS_TOTAL == Config().SESSIONS_TOTAL


def test_from_env_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSIONS_TOTAL", "много")
    with pytest.raises(ValueError, match="SESSIONS_TOTAL"):
        Config.from_env()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"SESSIONS_TOTAL": 0}, "SESSIONS_TOTAL"),
        ({"OPCUA_SESSION_PORT_MAX": 70000}, "1..65535"),
        ({"OPCUA_SESSION_PORT_MIN": 5000, "OPCUA_SESSION_PORT_MAX": 4900}, "больше"),
        ({"OPCUA_SHARED_PORT": 4900}, "внутри диапазона"),
        ({"SESSIONS_PER_KEY": 5, "SESSIONS_PER_IP": 4}, "SESSIONS_PER_KEY"),
        ({"SESSION_IDLE_TIMEOUT_S": 20000}, "SESSION_IDLE_TIMEOUT_S"),
        ({"OPCUA_PUBLIC_HOST": ""}, "OPCUA_PUBLIC_HOST"),
    ],
)
def test_inconsistent_config_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(Config(), **changes)


def test_endpoint_url() -> None:
    assert Config().endpoint_url(4840) == "opc.tcp://opcua-sendbox.electroman.by:4840/electroman/"
    ipv6 = replace(Config(), OPCUA_PUBLIC_HOST="::1")
    assert ipv6.endpoint_url(4841) == "opc.tcp://[::1]:4841/electroman/"
