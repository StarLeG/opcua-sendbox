# ADR-002 · Спайк asyncua: endpoint, история, алармы, счётчик клиентов

- **Статус:** автоматическая часть принята; ручная проверка (Docker, UaExpert, ModbusToolkit) ждёт подтверждения
- **Дата:** 2026-11-03
- **Задача:** TASK-002
- **Версия:** asyncua 2.1.0 (`uv.lock`), Python 3.14, Windows 11
- **Скрипт:** `scripts/spikes/spike_asyncua.py` (автоматика), `--serve` — ручной стенд

## 1. Endpoint за NAT/Docker

**Факт.** В asyncua 2.1 есть два флага подстановки адреса в GetEndpoints/CreateSession
(`InternalServer._mangle_endpoint_url`):

- `match_discovery_endpoint_url` (по умолчанию True): если клиент прислал EndpointUrl,
  хост и порт ответа берутся из него;
- `match_discovery_source_ip` (по умолчанию True): иначе хост и порт подменяются на
  `sockname` транспорта. В Docker это IP контейнера — это и есть issue #232.

Адрес прослушивания задаётся отдельно от endpoint: `Server.socket_address`.

| Проверка | Результат |
|---|---|
| 1a настройки по умолчанию, GetEndpoints без EndpointUrl | **FAIL (ожидаемо):** вернул `127.0.0.1:<bind>` вместо публичного URL, то есть подмена на sockname воспроизведена |
| 1b `set_match_discovery_client_ip(False)`, без EndpointUrl | PASS: ровно `opc.tcp://opcua-sendbox.electroman.by:<port>/electroman/` |
| 1c то же, EndpointUrl = публичный URL | PASS |
| 1d TCP-прокси с другим внешним портом (эмуляция NAT) | PASS: ответ отражает адрес, по которому пришёл клиент |
| 1e через прокси: CreateSession + ActivateSession + Read | PASS: `BadTcpEndpointUrlInvalid` нет, сервер endpointUrl не проверяет |

**Решение — штатные средства asyncua, без патча и без переопределения `get_endpoints`:**

```python
server.set_endpoint(f"opc.tcp://{public_host}:{public_port}/electroman/")
server.socket_address = ("0.0.0.0", bind_port)
server.set_match_discovery_client_ip(False)   # не подставлять sockname (IP контейнера)
# match_discovery_endpoint_url оставляем True: клиент получает тот адрес, по которому пришёл
```

`socket_address` — публичный атрибут `Server`, но в документации не описан; закрываем
unit-тестом (ADR-000, «Зависимость от приватного API»).

**Не проверено автоматически:** запуск внутри Docker-контейнера (Docker daemon на
машине не был запущен) и поведение UaExpert/ModbusToolkit.

## 2. HistoryRead на работающем сервере

| Проверка | Результат |
|---|---|
| 2a `HistoryDict`, внутренняя запись → HistoryRead через 1,5 с | PASS: все 5 значений, сервер не останавливали |
| 2b `HistoryDict`, внешняя запись без SourceTimestamp | **FAIL (ожидаемо):** HistoryRead → `BadInternalError`; в логе asyncua уровня ERROR ничего нет, исключение превращается в статус |
| 2c сервер отвечает после этого | PASS |
| 2a/2b/2c с `SendBoxHistory` | PASS: внешнее значение 42.0 в истории с подставленной меткой |

**Решение.** `SendBoxHistory(HistoryDict)` — обязательный компонент: в `save_node_value`
при `SourceTimestamp is None` подставляет `ServerTimestamp` (asyncua проставляет его при
записи, так как `force_server_timestamp=True`), иначе текущее UTC. Метку предыдущего
значения не копирует. Подключается через `server.iserver.history_manager.set_storage(...)`.

## 3. ExclusiveLimitAlarmType

**Факты.**
- `get_event_generator(ExclusiveLimitAlarmType, emitting_node)` создаёт событие со всеми
  полями типа (AckedState, ActiveState/*, BaseHigh/LowLimit, ConfirmedState, EnabledState,
  Severity, Message, …), но без `LimitState`: `ExclusiveLimitStateMachine` в asyncua
  бросает `NotImplementedError`. Тип служит только оболочкой события.
- Событие доставляется **только мониторам, подписанным ровно на `emitting_node`**
  (`MonitoredItemService.trigger_event`). По HasNotifier оно не всплывает: подписка на
  узел Server события с папки SendBox не получала (FAIL до исправления).

| Проверка (после исправления) | Результат |
|---|---|
| `subscribe_events` от папки SendBox | PASS: Message, Severity=700, ActiveState доходят |
| `subscribe_events` от узла Server | PASS |
| `subscribe_alarms_and_conditions` от папки и от Server | PASS |

**Решения.**
- `emit_event(...)`: одно событие (один EventId) рассылается на каждый узел-нотификатор,
  то есть на папку SendBox **и** на Server. Клиенты по умолчанию (UaExpert) подписываются
  на Server.
- Логику порогов, гистерезиса, переходов Active/Inactive и текстового уровня ведёт наш
  `AlarmEvaluator`; перед отправкой он выставляет `ActiveState`, `Severity`, `Message`.
- `LimitState` в событии отсутствует. **Ждёт ручной проверки:** UaExpert (Event View,
  Alarms & Conditions View) и ModbusToolkit («События»). Если какой-то из них отображает
  событие некорректно, задача TASK-103 не начинается, и выбор (OffNormalAlarmType / ручной
  LimitState / патч) выносится к владельцу.

## 4. Счётчик клиентов

`InternalSession._current_connections` (класс-переменная; в нашем процессе один сервер,
поэтому значение относится к нему): 2 клиента → 2, после disconnect → 1, после обрыва
сокета без CloseSession → 0 уже через секунду (сессия закрывается при разрыве
соединения; таймаут сессии — запасной путь). PASS.

**Решение:** основной источник `client_count`, опрос раз в секунду. Приватный API —
пункт в ADR-000 и unit-тест.

## Версия asyncua

Проверено на 2.1.0. Предлагаемый диапазон в `pyproject.toml`: `asyncua>=2.1,<3`
(ориентир `<2` из плана был дан до выхода ветки 2.x; ждёт подтверждения владельца).
