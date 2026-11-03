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
server.socket_address = (None, bind_port)    # все интерфейсы: IPv4 и IPv6
server.set_match_discovery_client_ip(False)   # не подставлять sockname (IP контейнера)
# match_discovery_endpoint_url оставляем True: клиент получает тот адрес, по которому пришёл
```

**Bind только на `0.0.0.0` недостаточен** (найдено при ручной проверке). Сервер слушал
только IPv4, а на Windows `localhost` разрешается сначала в `::1`, поэтому UaExpert и
ModbusToolkit не подключались к `opc.tcp://localhost:…`. Python-клиент asyncua при этом
работал: он перебирает адреса и откатывается на IPv4, так что автоматика проблему не
поймала. С `host=None` asyncio открывает слушатели на всех интерфейсах обоих семейств;
проверено подключение к `localhost`, `127.0.0.1` и `[::1]`. Интеграционный тест обязан
проверять подключение по `::1`. Адрес прослушивания задаётся env `OPCUA_BIND_HOST`
(пусто = все интерфейсы).

`socket_address` — публичный атрибут `Server`, но в документации не описан, а значение
`None` для хоста не соответствует его аннотации `tuple[str, int]`. Закрываем unit-тестом
(ADR-000, «Зависимость от приватного API»).

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

**Ручная проверка ModbusToolkit 1.58.0** (2026-11-03, стенд `--serve` на :4850).
Подключение без шифрования, дерево `Objects/SendBox/{Sine, Writable}`, мониторинг и тренд
`Sine` работают. Страница «История» → экспорт CSV: 260 значений `Sine` подряд
(03:40:12–03:44:32, шаг 1 с, без пропусков), заполнены SourceTimestamp и ServerTimestamp,
статус Good. PASS.

**Запись внешним клиентом — ModbusToolkit 1.58.0, PASS.** Три записи в `Writable`
(15, 68, 11; `type=Double`, Good), клиент SourceTimestamp не передаёт. HistoryRead
вернул все три значения с подставленной `SendBoxHistory` меткой, равной ServerTimestamp
записи (04:03:10.509, 04:03:43.076, 04:04:24.286), сервер работал без ошибок.
Нюанс: у **текущего значения** узла SourceTimestamp остаётся пустым (снимок узлов
ModbusToolkit), метку подставляет только история. Для TASK-103: при приёме записи в
writable-тег сервер сам проставляет SourceTimestamp текущему значению, чтобы клиенты
видели время изменения.

Попутно: подписка на события у переменной (`addEventMonitoredItem` на `Tags.Writable`)
возвращает `BadServiceUnsupported` — корректный отказ (у переменных нет EventNotifier),
ошибка в действиях клиента, сервер не страдает.

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
- **ModbusToolkit 1.58.0 — PASS** (журнал обмена, 2026-11-03): подписка на события
  `ns=2;s=SendBox` и `ns=0;i=2253` (Server); события «Стенд: событие 44…51» приходят
  раз в 10 с. При подписке на оба узла одно событие приходит дважды (один и тот же
  EventId, по разу на каждый монитор) — это ожидаемо, клиент сам выбирает источник.
  Отсутствие `LimitState` ModbusToolkit не мешает: его фильтр поле не запрашивает.
- `LimitState` в событии отсутствует. **Ждёт ручной проверки:** UaExpert (Event View,
  Alarms & Conditions View); ModbusToolkit — PASS (см. выше). Если UaExpert отображает
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

Проверено на 2.1.0. Диапазон в `pyproject.toml`: `asyncua>=2.1,<3` (подтверждено владельцем
2026-11-03; ориентир `<2` из плана был дан до выхода ветки 2.x). Точная версия — в `uv.lock`.

## Журнал обмена ModbusToolkit: прочие наблюдения

- 03:37:30–03:37:51 `BadConnectionRejected` — период, когда стенд слушал только IPv4
  (`0.0.0.0`), а `localhost` у клиента разрешался в `::1`. После bind на все интерфейсы ушло.
- `BadTcpEndpointUrlInvalid` с адресом `localhost:4850/electroman/` — адрес введён без
  схемы `opc.tcp://`, ошибка на стороне клиента. Для UI SendBox: показывать endpoint
  целиком, с кнопкой «Копировать».
- 03:40:12.481 `BadIdentityTokenRejected` — единичный отказ ровно в момент рестарта
  стенда (сервер стартовал в 03:40:12.456), следующее подключение в 03:40:38 успешно.
  Вероятно, переподключение клиента попало на ещё не готовый сервер. Для сервиса:
  воркер сообщает `ready` только после `server.start()` и построения адресного
  пространства; интеграционный тест подключается сразу после `ready`.

## Замечание: порт 4840 на машине разработчика

4840 — стандартный порт OPC UA discovery. На Windows с установленным OPC Foundation
Local Discovery Server (`opcualds`) порт занят, и bind падает с WinError 10013. Порт
общего сервера поэтому настраивается через env (`OPCUA_SHARED_PORT`, по умолчанию 4840
для прода), локально используется другой (стенд спайка запускался на 4850).
