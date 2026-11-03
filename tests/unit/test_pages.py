"""Страницы UI без браузера: экосистема, донат, автор, плашка, RU/EN, статика."""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.api.pages import GENERATOR_FORMS
from app.api.rate_limit import IpRateLimiter
from app.api.services import AppServices
from app.api.stats import RequestStats
from app.opcua.generators import BIT_KINDS, NUMERIC_KINDS
from tests.fakes import Env

PAGES = ["/", "/session", "/session/abc123", "/docs", "/api/docs"]


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    env = Env()
    services = AppServices(
        config=env.config, manager=env.manager, action_limiter=IpRateLimiter(100), stats=RequestStats()
    )
    app = create_app(services=services, start_manager=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_every_page_has_ecosystem_donate_and_banner(client: httpx.AsyncClient, path: str, lang: str) -> None:
    response = await client.get(path, params={"lang": lang})
    assert response.status_code == 200
    html = response.text
    assert f'<html lang="{lang}">' in html
    for url in (
        "https://modbustoolkit.electroman.by/",
        "https://modbus-sendbox.electroman.by/",
        "https://modbustoolkit.electroman.by/support.html",
        "https://github.com/StarLeG",
        "https://github.com/StarLeG/opcua-sendbox",
    ):
        assert url in html, (path, lang, url)
    assert "OPC UA<span class=\"dot\">·</span>SANDBOX" in html
    assert 'class="btn donate"' in html
    banner = re.search(r'<div class="banner" role="alert"><strong>(.*?)</strong>', html, re.DOTALL)
    assert banner is not None
    if lang == "ru":
        # тестовый конфиг: простой 60 с, жизнь 600 с
        assert "SecurityPolicy=None" in banner.group(1)
        assert "после 1 мин" in banner.group(1) and "через 10 мин" in banner.group(1)
    else:
        assert "Acknowledge/Confirm is not supported" in banner.group(1)
        assert "after 1 min" in banner.group(1) and "after 10 min" in banner.group(1)


async def test_language_cookie_is_remembered(client: httpx.AsyncClient) -> None:
    response = await client.get("/", params={"lang": "en"})
    assert response.cookies.get("lang") == "en"
    again = await client.get("/")
    assert '<html lang="en">' in again.text


async def test_index_shows_shared_endpoint(client: httpx.AsyncClient) -> None:
    html = (await client.get("/")).text
    assert "opc.tcp://opcua-sendbox.electroman.by:100/electroman/" in html
    assert 'data-copy-text="opc.tcp://opcua-sendbox.electroman.by:100/electroman/"' in html
    assert "/static/overview.js" in html


@pytest.mark.parametrize(
    ("lang", "title", "rows"),
    [
        (
            "ru",
            "Счётчики сервера",
            ["Активные соединения", "Запросы/с", "Ошибки/с", "Всего запросов", "Всего ошибок", "Uptime"],
        ),
        (
            "en",
            "Server counters",
            ["Active connections", "Requests/s", "Errors/s", "Total requests", "Total errors", "Uptime"],
        ),
    ],
)
async def test_index_has_server_counters(
    client: httpx.AsyncClient, lang: str, title: str, rows: list[str]
) -> None:
    html = (await client.get("/", params={"lang": lang})).text
    assert title in html
    for row in rows:
        assert row in html
    for cell in ("cnt-clients", "cnt-rps", "cnt-eps", "cnt-total", "cnt-errors", "cnt-uptime"):
        assert f'id="{cell}"' in html


async def test_session_page_embeds_generator_config(client: httpx.AsyncClient) -> None:
    html = (await client.get("/session/abc123")).text
    assert 'window.SESSION_ID = "abc123";' in html
    match = re.search(r"window\.SENDBOX = (\{.*?\});</script>", html)
    assert match is not None
    config = json.loads(match.group(1))
    assert set(config["generators"]) == NUMERIC_KINDS | BIT_KINDS | {"constant"}
    assert sorted(config["bitKinds"]) == sorted(BIT_KINDS)
    assert config["limits"] == {"tags": 100, "alarms": 20}
    assert (await client.get("/session")).text.count("window.SESSION_ID = null;") == 1


def test_generator_form_defaults_are_valid() -> None:
    """Значения по умолчанию в форме тега проходят серверную проверку."""
    from app.opcua.tags import TagSpec

    for kind, (_ru, _en, params) in GENERATOR_FORMS.items():
        values: dict[str, Any] = {}
        for name, _pru, _pen, default in params:
            if name == "values":
                values[name] = [float(x) for x in str(default).split(",")]
            elif name == "points":
                # «время:значение, …» → [[t, v], ...] (как collectParams в session.js)
                values[name] = [[float(p) for p in item.split(":")] for item in str(default).split(",")]
            else:
                values[name] = default
        data_type = "Boolean" if kind in BIT_KINDS else "Double"
        TagSpec(name="T", kind=kind, data_type=data_type, params=values)


@pytest.mark.parametrize(
    ("lang", "labels"),
    [
        ("ru", ("Скачать JSON", "Загрузить JSON")),
        ("en", ("Download JSON", "Upload JSON")),
    ],
)
async def test_session_page_has_config_export_import(
    client: httpx.AsyncClient, lang: str, labels: tuple[str, str]
) -> None:
    html = (await client.get("/session/abc123", params={"lang": lang})).text
    assert "data-export-config" in html
    # кнопка «Загрузить JSON» — и в рабочей области, и в блоке «сессия закрыта»
    assert html.count("data-import-config") == 2
    assert 'id="config-file"' in html
    for label in labels:
        assert label in html


async def test_session_id_is_escaped(client: httpx.AsyncClient) -> None:
    html = (await client.get("/session/%3C%2Fscript%3E")).text
    assert "window.SESSION_ID = \"</script>\"" not in html


async def test_docs_has_examples_and_limits(client: httpx.AsyncClient) -> None:
    html = (await client.get("/docs")).text
    assert "git clone https://github.com/StarLeG/opcua-sendbox.git" in html
    assert "ns=2;s=Tags.Sine" in html
    assert "Сессий: 2 на ключ, 4 на IP, 10 всего." in html
    assert "Олег Старинский" in html


@pytest.mark.parametrize(
    "path", ["/static/app.css", "/static/common.js", "/static/session.js", "/static/overview.js", "/static/i18n.js"]
)
async def test_static_served(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 200


async def test_pages_not_in_openapi(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/api/openapi.json")).json()["paths"]
    assert "/" not in paths and "/docs" not in paths and "/session/{session_id}" not in paths


def test_duration_format() -> None:
    from app.api.pages import _duration

    assert _duration(1800, "ru") == "30 мин"
    assert _duration(14400, "ru") == "4 ч"
    assert _duration(14400, "en") == "4 h"
    assert _duration(5400, "en") == "90 min"
