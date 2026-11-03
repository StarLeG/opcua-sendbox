# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## О проекте

**OPC UA SendBox** (opcua-sendbox.electroman.by, github.com/StarLeG/opcua-sendbox) —
публичная песочница OPC UA серверов экосистемы Electroman. Предшественник и образец
соглашений — Modbus SendBox (`D:\Qt\QtProject\modbus-test-server`); клиент экосистемы —
ModbusToolkit (`D:\Qt\QtProject\ModbusToolkit`, C++/Qt, open62541). Исходное ТЗ —
`Doc/prompt.md`; задачи и инварианты — `ISSUES.md`; решения — `docs/adr/`.

## Команды

```bash
uv sync                                   # зависимости (uv.lock — источник версий)
uv run python -m app                      # web на 127.0.0.1:8766, общий сервер на :4840
uv run pytest                             # все тесты (~1,5 мин: настоящие процессы и серверы)
uv run pytest tests/unit                  # быстрые
uv run pytest tests/unit/test_generators.py::test_sine_shape_at_key_points   # один тест
uv run ruff check .
uv run mypy app tests                     # strict; в tests отключены arg-type/union-attr (аннотации asyncua)
docker compose up -d --build              # см. docs/DEPLOY.md
```

На Windows с OPC Foundation LDS порт 4840 занят (WinError 10013) — локально запускать с
`OPCUA_SHARED_PORT=4850 OPCUA_PUBLIC_HOST=localhost` (или диапазон 4870–4880, как в
ручных проверках). Вывод с кириллицей в консоль — `PYTHONUTF8=1`.

## Архитектура

Один web-процесс (FastAPI) и по **дочернему процессу на каждый OPC UA сервер**
(`multiprocessing`, spawn): общий демо-сервер (read-only, `OPCUA_SHARED_PORT`) и личные
сессии (`OPCUA_SESSION_PORT_MIN..MAX`). Слои, снизу вверх:

- `app/opcua/generators.py` — генераторы значений, перенесены из Modbus SendBox
  (без `follow*`); `tags.py` — `TagSpec`/`AlarmSpec`: проверка параметров пробным
  созданием генератора, поэтому ошибка доходит до клиента как 422, а не роняет воркер.
- `address_space.py` — всё, что касается asyncua, внутри процесса воркера: сервер с
  настройками ADR-002, узлы `ns=2;s=Tags.*`/`Alarms.*`, `SendBoxHistory`, хуки
  PreWrite/PostWrite, `AlarmEvaluator` (пороги, гистерезис — state machine в asyncua нет),
  `emit_event` (рассылка события на SendBox **и** Server снимками).
- `security.py` — SecurityPolicy Basic256Sha256 и демо-учётка (ADR-004):
  `ServerSecurity` (frozen dataclass, едет в `WorkerParams` через pickle),
  `DemoUserManager` (`get_user` зовётся и для анонимов — их пускаем явно),
  `ensure_server_cert` (самоподписанный сертификат в `OPCUA_CERTS_DIR`, гонка
  воркеров разрешается tmp-каталогом и `rename`).
- `worker.py` (в дочернем процессе) ↔ `worker_handle.py` (в родителе): IPC из трёх
  очередей — команды с ответами, control (не теряется), telemetry (ограничена,
  drop-newest). Reader-нити → event loop через `call_soon_threadsafe`.
- `session_manager.py` — реестр под `asyncio.Lock`, запуск/остановка процессов вне
  лока, лимиты, reaper, общий сервер с перезапуском; `port_allocator.py` — порты по
  кругу с пробой bind; `keys.py` — API-ключи без регистрации.
- `app/api/` — REST (`routes.py`, коды ошибок в docstring), WebSocket (`ws.py`),
  страницы (`pages.py`, f-строки + vanilla JS в `static/`, RU/EN через `t()`),
  `/metrics` (`prometheus.py`).

## Инварианты (не нарушать)

1. **Подключённый OPC UA клиент = активная сессия**: `client_count > 0` останавливает
   idle-таймер, переход в 0 запускает его заново; потолок жизни действует всегда.
   Открытый WebSocket UI сессию не продлевает.
2. **Телеметрия может теряться, control-события — нет** (ADR-001 п. 5).
3. **Ни одна сессия не остаётся в `starting`**: порт освобождает тот, кто последним
   владеет сессией (`create` при неудаче/отмене, `delete`, обработчик аварии).
4. **Event loop FastAPI не блокируется**: блокирующие вызовы multiprocessing — через
   `asyncio.to_thread` или reader-нити.
5. **Общий сервер read-only.**
6. **Bind на все интерфейсы (IPv4 и IPv6)** — `socket_address=(None, port)`: на Windows
   `localhost` резолвится в `::1`. В Docker — `OPCUA_BIND_HOST=0.0.0.0` (IPv6 снаружи
   обслуживает Docker); пустое значение работает и при отключённом IPv6.
7. **Endpoint объявляет `OPCUA_PUBLIC_HOST`**, `set_match_discovery_client_ip(False)` —
   иначе в GetEndpoints уходит IP контейнера (ADR-002 п. 1).

## asyncua: приватный API и грабли

Версия — `asyncua>=2.1,<3`. Код опирается на приватный API (таблица и тесты —
`docs/adr/ADR-000-stack.md`): `socket_address`, `bind_condition_methods` (до `init()`!),
`subscription_service.trigger_event` + `event.emitting_node`,
`InternalSession._current_connections`, наследование `HistoryDict`. При обновлении
asyncua прогнать перечисленные там тесты. Подробности поведения — `docs/adr/ADR-002-asyncua-spike.md`;
стенд для ручной проверки — `scripts/spikes/spike_asyncua.py --serve`.

## Тесты

- `tests/unit/` — быстрые; SessionManager и API — на поддельном воркере `tests/fakes.py`
  (`Env`, `FakeHandle` с управляемыми задержками, сбоями, авариями).
- `tests/integration/` — настоящие серверы asyncua и процессы; маркер `integration`
  ставится автоматически. Порты: 15000–15058 фикстура `port`, 15059 общий сервер и
  15060–15100 сессии тестов менеджера — не пересекаются с продовыми 4840–5000.
- WebSocket тестируется через `starlette.testclient.TestClient`; сообщения воркера
  внедряются в loop приложения через `client.portal.call(...)`.
- `pytest-timeout` — 120 с на тест; `.env.example` сверяется с полями `Config` тестом.

## Соглашения

- Версия только в `pyproject.toml` (SemVer), в коде — `importlib.metadata`.
- Каждая задача ISSUES.md — отдельный коммит `feat(opcua): …` / `fix(opcua): …` /
  `docs(opcua): …`, после закрытой задачи — push и проверка CI.
- Архитектурное решение — сначала запись в `docs/adr/`.
- Новая переменная окружения — поле `Config` + `.env.example` + `docker-compose.yml`.
- UI-тексты — парами RU/EN; строки JS — в `app/api/static/i18n.js`. На каждой странице:
  плашка ограничений, донат (`SUPPORT_URL`), ссылки ModbusToolkit/Modbus SendBox, автор.
- Скриншоты ручной приёмки — `ui-*.png` в корне (в .gitignore).
