"""HTML-страницы UI: каркас + «Обзор», «Моя сессия», «Документация», Swagger (TASK-203).

Подход Modbus SendBox (ADR-008 там): страницы рендерит сервер f-строками, живые
данные подтягивает vanilla JS из REST и WebSocket, сборки нет. Тексты RU/EN через
`t()`, язык — `?lang=` или cookie. На каждой странице: плашка с ограничениями,
призыв поддержать проект, футер с продуктами экосистемы и автором.
"""
from __future__ import annotations

import json
from html import escape
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.i18n import COOKIE_NAME, Lang, lang_links, resolve_lang, t
from app.api.routes import ServicesDep
from app.config import Config
from app.opcua.generators import BIT_KINDS
from app.opcua.tags import INTEGER_RANGES, MAX_UPDATE_PERIOD_S, MIN_UPDATE_PERIOD_S

#: HTML-страницы — не REST API, в OpenAPI не попадают.
pages_router = APIRouter(include_in_schema=False)

# --- ссылки экосистемы (план, раздел «Экосистема и донат в UI») -------------------------

MODBUSTOOLKIT_URL = "https://modbustoolkit.electroman.by/"
MODBUSTOOLKIT_DOCS_URL = "https://modbustoolkit.electroman.by/docs.html"
MODBUS_SENDBOX_URL = "https://modbus-sendbox.electroman.by/"
SUPPORT_URL = "https://modbustoolkit.electroman.by/support.html"
TELEGRAM_URL = "https://t.me/modbustoolkit"
ELECTROMAN_URL = "https://electroman.by"
AUTHOR_NAME_RU = "Олег Старинский"
AUTHOR_NAME_EN = "Oleg Starinsky"
AUTHOR_GITHUB_URL = "https://github.com/StarLeG"
GITHUB_URL = "https://github.com/StarLeG/opcua-sendbox"
GITHUB_CLONE_URL = f"{GITHUB_URL}.git"

_SWAGGER_CSS_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css"
_SWAGGER_JS_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"
_HLJS_JS_URL = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"

#: Разделы навигации: (URL, RU, EN).
NAV_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("/", "Обзор", "Overview"),
    ("/session", "Моя сессия", "My session"),
    ("/docs", "Документация", "Docs"),
    ("/api/docs", "REST API", "REST API"),
)

WARNING_BANNER_RU = (
    "Это демо-сервер для проверки клиентов, не используйте его для production-данных. "
    "Соединение без шифрования: SecurityPolicy=None, анонимный вход. Basic256Sha256 и "
    "логин/пароль появятся в следующей версии. Алармы приходят событиями A&C, "
    "Acknowledge/Confirm не поддерживается. Личная сессия закрывается после {idle} "
    "без активности и в любом случае через {life}. Подключённый OPC UA клиент "
    "считается активностью. Все данные живут в памяти."
)
WARNING_BANNER_EN = (
    "This is a demo server for testing clients, do not use it for production data. "
    "The connection is unencrypted: SecurityPolicy=None, anonymous login. Basic256Sha256 "
    "and user/password come in the next version. Alarms arrive as A&C events, "
    "Acknowledge/Confirm is not supported. A personal session closes after {idle} "
    "without activity and in any case after {life}. A connected OPC UA client counts "
    "as activity. All data lives in memory."
)

_PROMO_RU = (
    "Подключайтесь к песочнице из ModbusToolkit: дерево узлов, подписки, история, "
    "события, мост OPC UA ↔ Modbus. Бесплатно, 19 инструментов, Windows и Linux."
)
_PROMO_EN = (
    "Connect to the sandbox from ModbusToolkit: node tree, subscriptions, history, "
    "events, OPC UA ↔ Modbus bridge. Free, 19 tools, Windows and Linux."
)
_SIBLING_RU = (
    "Нужен Modbus TCP? Публичный сервер modbus-sendbox.electroman.by:5020: 10 слейвов, "
    "значения меняются сами, сбои по заказу."
)
_SIBLING_EN = (
    "Need Modbus TCP? Public server modbus-sendbox.electroman.by:5020: 10 slaves, "
    "values change on their own, faults on demand."
)
_SUPPORT_RU = (
    "SendBox бесплатный и без рекламы. Сервер и <b>испытательный стенд</b> "
    "оплачиваются из пожертвований."
)
_SUPPORT_EN = (
    "SendBox is free and ad-free. The server and the <b>test bench</b> are paid for "
    "by donations."
)

# --- генераторы для формы тега (те же kind'ы и параметры, что проверяет сервер) ----------

#: kind → (название RU, EN, [(параметр, подпись RU, EN, значение по умолчанию)]).
GENERATOR_FORMS: dict[str, tuple[str, str, list[tuple[str, str, str, Any]]]] = {
    "sine": ("синус", "sine", [
        ("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 100),
        ("period_s", "Период, с", "Period, s", 10), ("phase", "Фаза (доля периода)", "Phase (fraction)", 0),
    ]),
    "sawtooth": ("пила", "sawtooth", [
        ("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 100),
        ("period_s", "Период, с", "Period, s", 10), ("phase", "Фаза (доля периода)", "Phase (fraction)", 0),
    ]),
    "triangle": ("треугольник", "triangle", [
        ("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 100),
        ("period_s", "Период, с", "Period, s", 10), ("phase", "Фаза (доля периода)", "Phase (fraction)", 0),
    ]),
    "meander": ("меандр", "meander", [
        ("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 100),
        ("period_s", "Период, с", "Period, s", 10), ("duty", "Скважность (0..1)", "Duty (0..1)", 0.5),
    ]),
    "counter": ("счётчик", "counter", [
        ("start", "Старт", "Start", 0), ("step", "Шаг", "Step", 1),
        ("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 1000),
        ("interval_s", "Период шага, с", "Step interval, s", 1), ("wrap", "Цикл (wrap)", "Wrap", True),
    ]),
    "random_uniform": ("случайное", "random", [("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 100)]),
    "random_walk": ("блуждание", "random walk", [
        ("min", "Минимум", "Min", 0), ("max", "Максимум", "Max", 100),
        ("step", "Шаг", "Step", 1), ("mean_reversion", "Возврат к среднему (0..1)", "Mean reversion (0..1)", 0),
    ]),
    "ramp_oneshot": ("переход (ramp)", "ramp", [
        ("from", "От", "From", 0), ("to", "До", "To", 100),
        ("duration_s", "Длительность, с", "Duration, s", 10), ("then", "Дальше: hold, loop или число", "Then: hold, loop or a number", "hold"),
    ]),
    "countdown": ("обратный отсчёт", "countdown", [
        ("start", "Старт", "Start", 100), ("step", "Шаг", "Step", 1),
        ("min", "Минимум", "Min", 0), ("interval_s", "Период шага, с", "Step interval, s", 1),
    ]),
    "step": ("ступеньки", "steps", [
        ("values", "Значения через запятую", "Values, comma-separated", "0,25,50,75,100"),
        ("interval_s", "Период шага, с", "Step interval, s", 2),
    ]),
    "profile": ("профиль по точкам", "profile (points)", [
        ("points", "Точки «время:значение» через запятую", "Points «time:value», comma-separated", "0:20, 60:80, 180:80, 240:20"),
        ("repeat", "Повторять по кругу", "Repeat in a loop", False),
    ]),
    "constant": ("константа", "constant", [("value", "Значение", "Value", 0)]),
    "toggle": ("переключение", "toggle", [("period_s", "Период, с", "Period, s", 2)]),
    "pwm": ("ШИМ", "PWM", [("period_s", "Период, с", "Period, s", 4), ("duty", "Скважность (0..1)", "Duty (0..1)", 0.25)]),
    "random_bit": ("случайный бит", "random bit", [("p", "Вероятность 1 (0..1)", "Probability of 1 (0..1)", 0.5)]),
    "counter_bits": ("бит счётчика секунд", "seconds-counter bit", [("bit", "Номер бита", "Bit number", 0)]),
}


def _project_version() -> str:
    try:
        return package_version("opcua-sendbox")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0"


def _duration(seconds: int, lang: Lang) -> str:
    """Длительность для текста: целые часы — «4 ч», иначе минуты — «30 мин»."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600} {t(lang, 'ч', 'h')}"
    return f"{max(1, round(seconds / 60))} {t(lang, 'мин', 'min')}"


def _client_config(config: Config, lang: Lang) -> str:
    """Данные для JS: генераторы, типы, лимиты (JSON в <script>)."""
    data = {
        "generators": {
            kind: {
                "label": ru if lang == "ru" else en,
                "params": [
                    {"name": n, "label": pru if lang == "ru" else pen, "default": d} for n, pru, pen, d in params
                ],
            }
            for kind, (ru, en, params) in GENERATOR_FORMS.items()
        },
        "bitKinds": sorted(BIT_KINDS),
        "dataTypes": ["Double", "Float", *INTEGER_RANGES, "Boolean"],
        "period": {"min": MIN_UPDATE_PERIOD_S, "max": MAX_UPDATE_PERIOD_S},
        "limits": {"tags": config.TAGS_PER_SESSION, "alarms": config.ALARMS_PER_SESSION},
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"<script>window.SENDBOX = {payload};</script>"


# --- блоки -----------------------------------------------------------------------------


def _support_block(lang: Lang) -> str:
    return (
        '<div class="hero-support">\n'
        f'  <a class="btn donate" href="{SUPPORT_URL}">{t(lang, "Поддержать проект", "Support the project")}</a>\n'
        f"  <p>{t(lang, _SUPPORT_RU, _SUPPORT_EN)}</p>\n"
        "</div>\n"
    )


def _promo_block(lang: Lang) -> str:
    return (
        '<div class="docs-banner">\n  <div>\n'
        '    <span class="eyebrow">ModbusToolkit</span>\n'
        f"    <h2>{t(lang, 'Проверяйте клиенты в ModbusToolkit', 'Test clients in ModbusToolkit')}</h2>\n"
        f"    <p>{t(lang, _PROMO_RU, _PROMO_EN)}</p>\n"
        '  </div>\n  <div class="cta-row">\n'
        f'    <a class="btn" href="{MODBUSTOOLKIT_URL}">{t(lang, "Скачать", "Download")}</a>\n'
        f'    <a class="btn ghost" href="{MODBUSTOOLKIT_DOCS_URL}">{t(lang, "Документация", "Docs")}</a>\n'
        f'    <a class="btn ghost" href="{TELEGRAM_URL}">Telegram</a>\n'
        "  </div>\n</div>\n"
    )


def _sibling_block(lang: Lang) -> str:
    return (
        '<div class="card">\n'
        '  <span class="eyebrow">Modbus SendBox</span>\n'
        f"  <h3>{t(lang, 'Песочница Modbus TCP', 'Modbus TCP sandbox')}</h3>\n"
        f"  <p>{t(lang, _SIBLING_RU, _SIBLING_EN)}</p>\n"
        f'  <a class="btn ghost" href="{MODBUS_SENDBOX_URL}">{t(lang, "Открыть Modbus SendBox", "Open Modbus SendBox")}</a>\n'
        "</div>\n"
    )


def _author_block(lang: Lang) -> str:
    return f"""
<div class="panel author">
  <div>
    <span class="eyebrow">{t(lang, "Об авторе", "About the author")}</span>
    <p class="who">{t(lang, AUTHOR_NAME_RU, AUTHOR_NAME_EN)}</p>
    <p>{t(lang,
          "Инженер-наладчик автоматики: пишу инструменты, которыми пользуюсь сам на объектах. "
          "C++/Qt, Python, STM32. Экосистема Electroman — ModbusToolkit, Modbus SendBox и "
          "OPC UA SendBox — бесплатные инструменты для наладчиков АСУ ТП.",
          "Commissioning engineer for industrial automation: I build the tools I use on site "
          "myself. C++/Qt, Python, STM32. The Electroman ecosystem — ModbusToolkit, Modbus "
          "SendBox and OPC UA SendBox — is a set of free tools for control-system engineers.")}</p>
    <p><a href="{AUTHOR_GITHUB_URL}">GitHub</a> · <a href="{ELECTROMAN_URL}">electroman.by</a> ·
       <a href="{TELEGRAM_URL}">Telegram</a></p>
  </div>
</div>
"""


def _footer(lang: Lang) -> str:
    version = escape(_project_version())
    return f"""<footer>
<div class="wrap">
  <div class="foot-top">
    <div class="foot-brand">OPC UA<span class="dot">·</span>SANDBOX</div>
    <div class="foot-version">v{version}</div>
  </div>
  <div class="foot-grid">
    <div class="foot-group">
      <span class="eyebrow">ModbusToolkit</span>
      <ul>
        <li><a href="{MODBUSTOOLKIT_URL}">{t(lang, "Скачать для Windows и Linux", "Download for Windows and Linux")}</a></li>
        <li><a href="{MODBUSTOOLKIT_DOCS_URL}">{t(lang, "Документация ModbusToolkit", "ModbusToolkit docs")}</a></li>
        <li><a href="{TELEGRAM_URL}">{t(lang, "Telegram-канал", "Telegram channel")}</a></li>
      </ul>
    </div>
    <div class="foot-group">
      <span class="eyebrow">{t(lang, "Экосистема Electroman", "Electroman ecosystem")}</span>
      <ul>
        <li><a href="{MODBUS_SENDBOX_URL}">Modbus SendBox</a></li>
        <li><a href="{MODBUSTOOLKIT_URL}">ModbusToolkit</a></li>
        <li><a href="{ELECTROMAN_URL}">electroman.by</a></li>
      </ul>
    </div>
    <div class="foot-group">
      <span class="eyebrow">{t(lang, "Поддержка", "Support")}</span>
      <ul>
        <li><a href="{SUPPORT_URL}">{t(lang, "Поддержать проект", "Support the project")}</a></li>
        <li>{t(lang, "Автор", "Author")}: <a href="{AUTHOR_GITHUB_URL}">{t(lang, AUTHOR_NAME_RU, AUTHOR_NAME_EN)}</a></li>
      </ul>
    </div>
    <div class="foot-group">
      <span class="eyebrow">{t(lang, "OPC UA SendBox · этот сервер", "OPC UA SendBox · this server")}</span>
      <ul>
        <li><a href="/api/docs">REST API (Swagger)</a></li>
        <li><a href="/docs">{t(lang, "Документация сервера", "Server docs")}</a></li>
        <li><a href="{GITHUB_URL}">{t(lang, "Исходный код на GitHub", "Source code on GitHub")}</a></li>
      </ul>
    </div>
  </div>
</div>
</footer>"""


def _shell(
    title: str, active: str, body: str, lang: Lang, config: Config, extra_head: str = ""
) -> str:
    nav = "".join(
        f'<a href="{href}" class="nav-link{" active" if href == active else ""}">{t(lang, ru, en)}</a>'
        for href, ru, en in NAV_SECTIONS
    )
    banner = (WARNING_BANNER_RU if lang == "ru" else WARNING_BANNER_EN).format(
        idle=_duration(config.SESSION_IDLE_TIMEOUT_S, lang), life=_duration(config.SESSION_MAX_LIFETIME_S, lang)
    )
    site = t(lang, "OPC UA SendBox - публичная песочница OPC UA", "OPC UA SendBox - public OPC UA sandbox")
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · {escape(site)}</title>
<meta name="description" content="{escape(t(lang, "Бесплатный публичный OPC UA сервер для проверки клиентов: теги, история, алармы A&C, личные серверы.", "Free public OPC UA server for testing clients: tags, history, A&C alarms, personal servers."))}">
<link rel="icon" href="/static/favicon.ico" sizes="any">
<link rel="stylesheet" href="/static/app.css">
{extra_head}
<script>window.LANG = "{lang}";</script>
<script src="/static/i18n.js"></script>
</head>
<body>
<header class="topbar">
  <div class="wrap topbar-row">
    <a class="brand" href="/">OPC UA<span class="dot">·</span>SANDBOX</a>
    <span class="topbar-badge">{t(lang, "Бесплатный публичный сервер", "Free public server")}</span>
    <p class="lang-switch">{lang_links(lang, active)}</p>
  </div>
</header>
<nav><div class="wrap">{nav}</div></nav>
<main class="wrap">
<h1>{escape(title)}</h1>
<div class="banner" role="alert"><strong>{escape(banner)}</strong></div>
{body}
{_support_block(lang)}
</main>
{_footer(lang)}
</body>
</html>
"""


def _page(
    request: Request, title: str, active: str, body: str, lang: Lang, config: Config, extra_head: str = ""
) -> HTMLResponse:
    response = HTMLResponse(_shell(title, active, body, lang, config, extra_head))
    if "lang" in request.query_params:
        response.set_cookie(COOKIE_NAME, lang, max_age=365 * 24 * 3600, samesite="lax")
    return response


# --- страницы ---------------------------------------------------------------------------


def _index_body(config: Config, lang: Lang) -> str:
    shared = escape(config.endpoint_url(config.OPCUA_SHARED_PORT))
    return f"""
<p>{t(lang,
      "Живой OPC UA сервер, к которому можно подключиться прямо сейчас: теги меняются сами, "
      "история читается через HistoryRead, алармы приходят событиями Alarms &amp; Conditions. "
      "Ничего ставить не нужно — подключите свой клиент: UaExpert, SCADA, HMI или ModbusToolkit.",
      "A live OPC UA server you can connect to right now: tags change on their own, history "
      "is available via HistoryRead, alarms arrive as Alarms &amp; Conditions events. Nothing "
      "to install — connect your client: UaExpert, SCADA, HMI or ModbusToolkit.")}</p>

<h2>{t(lang, "Общий демо-сервер", "Shared demo server")}</h2>
<p>{t(lang, "Работает всегда, только чтение. Ключ не нужен.", "Always on, read-only. No key needed.")}
   <span id="shared-status" class="pill">…</span></p>
<div class="endpoint">
  <code id="shared-endpoint">{shared}</code>
  <button type="button" class="copy-btn" data-copy-text="{shared}">{t(lang, "Копировать", "Copy")}</button>
</div>
<div class="grid-2">
  <div class="panel">
    <h3>{t(lang, "Теги", "Tags")} <span class="note mono">ns=2;s=Tags.*</span></h3>
    <table class="values"><thead><tr>
      <th>{t(lang, "Тег", "Tag")}</th><th>{t(lang, "Значение", "Value")}</th><th>{t(lang, "Тренд 60 с", "60 s trend")}</th>
    </tr></thead><tbody id="shared-tags"><tr><td colspan="3" class="note">{t(lang, "Подключение…", "Connecting…")}</td></tr></tbody></table>
  </div>
  <div class="panel">
    <h3>{t(lang, "Алармы", "Alarms")} <span class="note mono">ns=2;s=Alarms.*</span></h3>
    <table class="values"><thead><tr><th>{t(lang, "Аларм", "Alarm")}</th><th>{t(lang, "Состояние", "State")}</th></tr></thead>
      <tbody id="shared-alarms"></tbody></table>
    <p class="note">{t(lang,
        "Подписывайтесь на события узла Server или папки SendBox. SineHigh срабатывает, когда Sine выше 80, Heartbeat переключается каждые 10 с.",
        "Subscribe to events of the Server node or the SendBox folder. SineHigh fires when Sine is above 80, Heartbeat toggles every 10 s.")}</p>
  </div>
</div>

<h2>{t(lang, "Личный сервер", "Personal server")}</h2>
<div class="cards">
  <div class="card">
    <span class="eyebrow">{t(lang, "Ваша песочница", "Your sandbox")}</span>
    <h3>{t(lang, "Свой OPC UA сервер за секунду", "Your own OPC UA server in a second")}</h3>
    <p>{t(lang,
         "Отдельный сервер на своём порту: добавляйте теги с генераторами, записывайте значения из клиента, вызывайте алармы вручную.",
         "A separate server on its own port: add tags with generators, write values from your client, trigger alarms by hand.")}</p>
    <button type="button" id="create-session" class="btn">{t(lang, "Создать личный сервер", "Create personal server")}</button>
    <p id="create-status" class="form-status"></p>
    <ul id="my-sessions" class="note"></ul>
  </div>
  {_sibling_block(lang)}
</div>

<h2>{t(lang, "Счётчики сервера", "Server counters")}</h2>
<div class="panel">
  <table class="values"><tbody>
    <tr><td>{t(lang, "Активные соединения", "Active connections")}</td><td id="cnt-clients" class="mono">…</td></tr>
    <tr><td>{t(lang, "Запросы/с", "Requests/s")}</td><td id="cnt-rps" class="mono">…</td></tr>
    <tr><td>{t(lang, "Ошибки/с", "Errors/s")}</td><td id="cnt-eps" class="mono">…</td></tr>
    <tr><td>{t(lang, "Всего запросов", "Total requests")}</td><td id="cnt-total" class="mono">…</td></tr>
    <tr><td>{t(lang, "Всего ошибок", "Total errors")}</td><td id="cnt-errors" class="mono">…</td></tr>
    <tr><td>Uptime</td><td id="cnt-uptime" class="mono">…</td></tr>
  </tbody></table>
  <p class="note">{t(lang,
      "Запросы считаются по HTTP API, ошибкой считается ответ со статусом 400 и выше. "
      "Активные соединения — OPC UA клиенты общего сервера и личных сессий.",
      "Requests are counted by the HTTP API; a response with status 400 and above counts as an error. "
      "Active connections are OPC UA clients of the shared server and personal sessions.")}</p>
</div>
{_promo_block(lang)}
<script type="module" src="/static/overview.js"></script>
"""


def _session_body(config: Config, lang: Lang, session_id: str | None) -> str:
    sid = json.dumps(session_id)
    return f"""
{_client_config(config, lang)}
<script>window.SESSION_ID = {sid};</script>
<div id="session-missing" hidden>
  <p id="missing-text">{t(lang, "Сессия не найдена: она закрыта или создана в другом браузере.", "Session not found: it is closed or was created in another browser.")}</p>
  <button type="button" id="create-session" class="btn">{t(lang, "Создать личный сервер", "Create personal server")}</button>
  <button type="button" class="btn ghost" data-import-config>{t(lang, "Загрузить JSON", "Upload JSON")}</button>
  <p id="create-status" class="form-status"></p>
  <ul id="my-sessions" class="note"></ul>
</div>
<input type="file" id="config-file" accept=".json,application/json" hidden>
<div id="session-view" hidden>
  <div class="endpoint">
    <code id="endpoint"></code>
    <button type="button" class="copy-btn" id="copy-endpoint">{t(lang, "Копировать", "Copy")}</button>
  </div>
  <div class="facts">
    <span>{t(lang, "Состояние", "State")}: <b id="state">…</b></span>
    <span>{t(lang, "OPC UA клиентов", "OPC UA clients")}: <b id="clients">0</b></span>
    <span>{t(lang, "Закроется по простою", "Idle close")}: <b id="idle-left">—</b></span>
    <span>{t(lang, "Закроется в любом случае", "Hard close")}: <b id="life-left">—</b></span>
    <button type="button" id="delete-session" class="danger">{t(lang, "Закрыть сессию", "Close session")}</button>
    <button type="button" class="ghost" data-export-config>{t(lang, "Скачать JSON", "Download JSON")}</button>
    <button type="button" class="ghost" data-import-config>{t(lang, "Загрузить JSON", "Upload JSON")}</button>
  </div>
  {_support_block(lang)}
  <div class="panel">
    <h3>{t(lang, "Теги", "Tags")} <span class="note mono">ns=2;s=Tags.*</span></h3>
    <table class="values"><thead><tr>
      <th>{t(lang, "Тег", "Tag")}</th><th>{t(lang, "Тип", "Type")}</th><th>{t(lang, "Генератор", "Generator")}</th>
      <th>{t(lang, "Значение", "Value")}</th><th>{t(lang, "Тренд 60 с", "60 s trend")}</th><th></th>
    </tr></thead><tbody id="tags"></tbody></table>
    <details>
      <summary>{t(lang, "Добавить тег", "Add tag")}</summary>
      <form id="tag-form">
        <div class="form-row">
          <label>{t(lang, "Имя (латиница, цифры, _)", "Name (latin, digits, _)")}<input name="name" required pattern="[A-Za-z][A-Za-z0-9_]{{0,63}}"></label>
          <label>{t(lang, "Тип данных", "Data type")}<select name="data_type"></select></label>
          <label>{t(lang, "Генератор", "Generator")}<select name="kind"></select></label>
          <label>{t(lang, "Период обновления, с", "Update period, s")}<input name="update_period_s" type="number" step="0.1" value="1"></label>
          <label class="check"><input name="writable" type="checkbox"> {t(lang, "Клиент может писать (только константа)", "Client may write (constant only)")}</label>
        </div>
        <div class="form-row" id="tag-params"></div>
        <div class="form-actions"><button type="submit">{t(lang, "Добавить", "Add")}</button></div>
        <p class="form-status" id="tag-status"></p>
      </form>
    </details>
  </div>
  <div class="grid-2">
    <div class="panel">
      <h3>{t(lang, "Алармы", "Alarms")} <span class="note mono">ns=2;s=Alarms.*</span></h3>
      <table class="values"><thead><tr><th>{t(lang, "Аларм", "Alarm")}</th><th>{t(lang, "Условие", "Condition")}</th><th>{t(lang, "Состояние", "State")}</th><th></th></tr></thead>
        <tbody id="alarms"></tbody></table>
      <details>
        <summary>{t(lang, "Добавить аларм", "Add alarm")}</summary>
        <form id="alarm-form">
          <div class="form-row">
            <label>{t(lang, "Имя", "Name")}<input name="name" required pattern="[A-Za-z][A-Za-z0-9_]{{0,63}}"></label>
            <label>{t(lang, "Вид", "Kind")}<select name="kind">
              <option value="limit">{t(lang, "по порогу тега", "tag threshold")}</option>
              <option value="periodic">{t(lang, "периодический", "periodic")}</option>
              <option value="manual">{t(lang, "ручной", "manual")}</option></select></label>
            <label data-for="limit">{t(lang, "Тег", "Tag")}<select name="tag"></select></label>
            <label data-for="limit">{t(lang, "Верхний порог", "High limit")}<input name="high" type="number" step="any"></label>
            <label data-for="limit">{t(lang, "Нижний порог", "Low limit")}<input name="low" type="number" step="any"></label>
            <label data-for="limit">{t(lang, "Гистерезис", "Deadband")}<input name="deadband" type="number" step="any" value="0"></label>
            <label data-for="periodic">{t(lang, "Период, с", "Period, s")}<input name="period_s" type="number" step="1" value="10"></label>
            <label>{t(lang, "Severity (1..1000)", "Severity (1..1000)")}<input name="severity" type="number" value="700" min="1" max="1000"></label>
            <label>{t(lang, "Сообщение", "Message")}<input name="message" maxlength="200"></label>
          </div>
          <div class="form-actions"><button type="submit">{t(lang, "Добавить", "Add")}</button></div>
          <p class="form-status" id="alarm-status"></p>
        </form>
      </details>
    </div>
    <div class="panel">
      <h3>{t(lang, "Журнал", "Live log")}</h3>
      <ul class="log" id="log"></ul>
    </div>
  </div>
</div>
<script type="module" src="/static/session.js"></script>
"""


def _example(code_id: str, language: str, code: str, lang: Lang) -> str:
    return (
        f'<pre><code id="{code_id}" class="language-{language}">{escape(code)}</code></pre>\n'
        f'<p><button type="button" class="copy-btn" data-copy="{code_id}">{t(lang, "Копировать", "Copy")}</button></p>\n'
    )


def _docs_body(config: Config, lang: Lang, site_url: str) -> str:
    shared = config.endpoint_url(config.OPCUA_SHARED_PORT)
    python = f'''import asyncio
from asyncua import Client, ua

async def main():
    async with Client("{shared}") as client:
        sine = client.get_node("ns=2;s=Tags.Sine")
        print("Sine =", await sine.read_value())

asyncio.run(main())'''
    curl = f'''KEY=$(curl -s -X POST {site_url}/api/keys | jq -r .key)
curl -s -X POST -H "X-API-Key: $KEY" {site_url}/api/sessions'''
    return f"""
<h2>{t(lang, "Подключение", "Connecting")}</h2>
<dl>
  <dt>{t(lang, "Общий сервер", "Shared server")}</dt><dd><code>{escape(shared)}</code></dd>
  <dt>{t(lang, "Личные серверы", "Personal servers")}</dt><dd>{t(lang, "порты", "ports")} <code>{config.OPCUA_SESSION_PORT_MIN}–{config.OPCUA_SESSION_PORT_MAX}</code></dd>
  <dt>{t(lang, "Безопасность", "Security")}</dt><dd><code>None</code>, {t(lang, "анонимный вход", "anonymous login")}</dd>
  <dt>{t(lang, "Пространство имён", "Namespace")}</dt><dd><code>urn:electroman:opcua-sendbox</code> (ns=2)</dd>
  <dt>{t(lang, "Узлы", "Nodes")}</dt><dd><code>Objects/SendBox/Tags/*</code>, <code>Objects/SendBox/Alarms/*</code></dd>
</dl>
<h3>UaExpert</h3>
<p>{t(lang,
      "Add Server → Custom Discovery → вставьте адрес, политика None, Anonymous. События: Document → Add → Event View, перетащите узел Server; алармы — Alarms &amp; Conditions View.",
      "Add Server → Custom Discovery → paste the address, policy None, Anonymous. Events: Document → Add → Event View, drag the Server node; alarms — Alarms &amp; Conditions View.")}</p>
<h3>ModbusToolkit</h3>
<p>{t(lang,
      "Вкладка OPC UA: вставьте адрес в поле «Адрес», безопасность None, «Подключиться». История — вкладка «История», события — «События» → подписка на SendBox или Server.",
      "OPC UA tab: paste the address, security None, Connect. History is on the History page, events on Events → subscribe to SendBox or Server.")}</p>
<h3>Python (asyncua)</h3>
{_example("example-python", "python", python, lang)}
<h3>REST API</h3>
{_example("example-curl", "bash", curl, lang)}
<p class="note" id="example-status"></p>

<h2>{t(lang, "Что умеет сервер", "What the server does")}</h2>
<ul>
  <li>{t(lang, "Теги Double, Float, Int16/32, UInt16/32, Boolean с генераторами: синус, пила, треугольник, меандр, счётчик, шум, блуждание, ступеньки, свой профиль по точкам и другие.", "Double, Float, Int16/32, UInt16/32 and Boolean tags with generators: sine, sawtooth, triangle, meander, counter, noise, random walk, steps, your own point profile and more.")}</li>
  <li>{t(lang, "Запись в теги-константы с флагом «клиент может писать».", "Writes to constant tags marked as writable.")}</li>
  <li>{t(lang, "История HistoryRead Raw: последние {n} значений каждого тега.", "HistoryRead Raw: the last {n} values of every tag.").format(n=config.HISTORY_SIZE)}</li>
  <li>{t(lang, "Алармы ExclusiveLimitAlarmType и AlarmConditionType: порог с гистерезисом, периодический, ручной; ConditionRefresh поддерживается.", "ExclusiveLimitAlarmType and AlarmConditionType alarms: threshold with deadband, periodic, manual; ConditionRefresh is supported.")}</li>
</ul>

<h2>{t(lang, "Ограничения", "Limits")}</h2>
<ul>
  <li>{t(lang, "Сессий: {k} на ключ, {i} на IP, {n} всего.", "Sessions: {k} per key, {i} per IP, {n} in total.").format(k=config.SESSIONS_PER_KEY, i=config.SESSIONS_PER_IP, n=config.SESSIONS_TOTAL)}</li>
  <li>{t(lang, "Тегов до {t} и алармов до {a} на сессию.", "Up to {t} tags and {a} alarms per session.").format(t=config.TAGS_PER_SESSION, a=config.ALARMS_PER_SESSION)}</li>
  <li>{t(lang, "Acknowledge и Confirm не поддерживаются: аларм сразу считается квитированным. LimitState в событии нет.", "Acknowledge and Confirm are not supported: an alarm is treated as acknowledged at once. Events carry no LimitState.")}</li>
  <li>{t(lang, "Шифрование и вход по логину появятся в следующей версии.", "Encryption and user login come in the next version.")}</li>
</ul>

<h2>{t(lang, "Исходный код", "Source code")}</h2>
<p>{t(lang, "Сервер открыт: можно поднять свою копию в локальной сети.", "The server is open source: you can run your own copy on a local network.")}</p>
{_example("example-clone", "bash", f"git clone {GITHUB_CLONE_URL}", lang)}
{_author_block(lang)}
{_promo_block(lang)}
<script type="module" src="/static/examples.js"></script>
"""


@pages_router.get("/", response_class=HTMLResponse)
def get_index(request: Request, services: ServicesDep) -> HTMLResponse:
    lang = resolve_lang(request)
    return _page(
        request, t(lang, "Обзор", "Overview"), "/", _index_body(services.config, lang), lang, services.config
    )


@pages_router.get("/session", response_class=HTMLResponse)
def get_my_sessions(request: Request, services: ServicesDep) -> HTMLResponse:
    lang = resolve_lang(request)
    body = _session_body(services.config, lang, None)
    return _page(request, t(lang, "Моя сессия", "My session"), "/session", body, lang, services.config)


@pages_router.get("/session/{session_id}", response_class=HTMLResponse)
def get_session_page(session_id: str, request: Request, services: ServicesDep) -> HTMLResponse:
    lang = resolve_lang(request)
    body = _session_body(services.config, lang, session_id)
    return _page(request, t(lang, "Моя сессия", "My session"), "/session", body, lang, services.config)


@pages_router.get("/docs", response_class=HTMLResponse)
def get_docs(request: Request, services: ServicesDep) -> HTMLResponse:
    lang = resolve_lang(request)
    return _page(
        request,
        t(lang, "Документация", "Docs"),
        "/docs",
        # адрес сайта — как его видит пользователь (за nginx — из X-Forwarded-*)
        _docs_body(services.config, lang, str(request.base_url).rstrip("/")),
        lang,
        services.config,
        extra_head=f'<script defer src="{_HLJS_JS_URL}"></script>',
    )


_API_DOCS_JS = """\
window.addEventListener("load", function () {
  window.ui = SwaggerUIBundle({
    url: "/api/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    docExpansion: "list",
    defaultModelsExpandDepth: 1
  });
});
"""


@pages_router.get("/api/docs", response_class=HTMLResponse)
def get_api_docs(request: Request, services: ServicesDep) -> HTMLResponse:
    lang = resolve_lang(request)
    note = t(
        lang,
        "Все команды REST API с описаниями. Ключ получите через POST /api/keys и передавайте "
        "в заголовке X-API-Key (кнопка Authorize). Схема OpenAPI — "
        '<a href="/api/openapi.json">/api/openapi.json</a>.',
        "Every REST API command with descriptions. Get a key via POST /api/keys and pass it "
        "in the X-API-Key header (Authorize button). OpenAPI schema — "
        '<a href="/api/openapi.json">/api/openapi.json</a>.',
    )
    body = (
        f'<p class="note">{note}</p>\n<div id="swagger-ui"></div>\n'
        f'<script src="{_SWAGGER_JS_URL}"></script>\n<script>{_API_DOCS_JS}</script>'
    )
    head = f'<link rel="stylesheet" href="{_SWAGGER_CSS_URL}">\n<link rel="stylesheet" href="/static/swagger-dark.css">'
    return _page(request, "REST API", "/api/docs", body, lang, services.config, extra_head=head)
