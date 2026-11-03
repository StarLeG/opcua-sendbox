# ADR-000 · Стек

- **Статус:** принято
- **Дата:** 2026-11-03
- **Задача:** TASK-001

## Контекст

Проект greenfield: на старте в каталоге были только ТЗ (`Doc/prompt.md`) и CLAUDE.md.
OPC UA SendBox — третий продукт экосистемы Electroman после ModbusToolkit (C++/Qt) и
Modbus SendBox (`modbus-test-server`, Python/FastAPI). Стек и соглашения согласованы с
владельцем так, чтобы новый сервис был продолжением Modbus SendBox.

## Решение

- **Python 3.11+**, `asyncio`; CI — матрица 3.11 и 3.14.
- **asyncua** — ядро OPC UA (сервер в каждом дочернем процессе, клиент в тестах).
  Диапазон в `pyproject.toml`, точная версия — в `uv.lock`. Спайк ADR-002, CI и прод
  работают на одной зафиксированной версии. Граница диапазона уточняется по итогам
  спайка ADR-002.
- **FastAPI + uvicorn** — REST, WebSocket и HTML-страницы UI.
- **uv** — venv и зависимости (`pyproject.toml` + `uv.lock`, сборка hatchling,
  dev-зависимости в `[dependency-groups]`).
- **pytest + pytest-asyncio** (`asyncio_mode=auto`, loop scope `function`), тесты в
  `tests/unit/` и `tests/integration/` (маркер `integration`).
- **ruff** (line-length 100), **mypy** strict.
- **stdlib `logging`**, формат `LOG_FORMAT=text|json`.
- **Docker / compose, nginx, systemd** — деплой.

Версия проекта задаётся только в `pyproject.toml` (SemVer, старт `0.1.0`), в коде
читается через `importlib.metadata`.

## Зависимость от приватного API asyncua

Каждый пункт закрыт тестом, который падает при изменении API. Обновление asyncua
требует прогнать эти тесты и перечитать пункты (`app/opcua/address_space.py`).

| Что используется | Зачем | Тест |
|---|---|---|
| `Server.socket_address = (None, port)` (аннотация — `str`) | bind на все интерфейсы, IPv4 и IPv6, отдельно от endpoint | `test_ipv6_loopback_connects`, `test_get_endpoints_announces_public_host` |
| `iserver.bind_condition_methods = True` до `init()` | ConditionRefresh для просмотрщиков A&C | `test_limit_alarm_events_and_condition_refresh` |
| `isession.subscription_service.trigger_event(event)` и поле `event.emitting_node` | рассылка события на папку SendBox и на Server | `test_limit_alarm_events_and_condition_refresh`, `test_alarm_events_reach_sendbox_folder_too` |
| `InternalSession._current_connections` | `client_count` для idle-таймера | `test_demo_set_builds_and_client_count` |
| наследование `HistoryDict.save_node_value` | подстановка SourceTimestamp | `tests/unit/test_history.py`, `test_external_write_without_source_timestamp` |
| `ServerItemCallback.request_params.NodesToWrite` в PreWrite (мутация `DataValue`) | SourceTimestamp у записи клиента | `test_external_write_without_source_timestamp` |
| `InternalServer.set_user_manager(UserManager)` (ADR-004) | демо-учётки: вход по логину/паролю, `get_user` зовётся и для анонимного токена | `test_demo_user_manager_accepts_anonymous`, `test_bad_password_rejected` |

## Ссылки

- `docs/adr/ADR-001-architecture.md`
- `docs/adr/ADR-002-asyncua-spike.md`
- `docs/adr/ADR-004-security.md`
