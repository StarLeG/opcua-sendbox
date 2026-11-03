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

Список пополняется по итогам спайка ADR-002. Каждый пункт закрыт unit-тестом, который
падает при изменении API. Обновление asyncua требует прогнать эти тесты и перечитать
пункты.

## Ссылки

- `docs/adr/ADR-001-architecture.md`
- `docs/adr/ADR-002-asyncua-spike.md`
