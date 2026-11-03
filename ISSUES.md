# ISSUES — OPC UA SendBox

Формат: `[P0] [ ] TASK-001 · Заголовок`
Приоритеты: **P0** — блокер релиза, **P1** — нужно к релизу, **P2** — можно после.
Каждая задача — отдельный коммит `feat(opcua): …` / `test(opcua): …`. Отмечать `[x]` после коммита.
План целиком — в ADR-001 и разделах ниже.

## Инварианты (не нарушать)

1. **Подключённый клиент = активная сессия.** `client_count` — активные OPC UA сессии, не
   TCP-соединения. Пока `> 0`, idle-таймер стоит; переход в 0 запускает его заново с полного
   `SESSION_IDLE_TIMEOUT_S`. Потолок жизни `SESSION_MAX_LIFETIME_S` действует всегда.
2. **Телеметрия может теряться (drop-newest), control-события — нет** (ADR-001 п. 5).
3. **Ни одна сессия не остаётся в `starting`/`stopping`**: откат и освобождение порта в `finally`.
4. **Event loop FastAPI не блокируется.**
5. **Общий сервер read-only**: команды изменения отклоняются.

---

## Эпик 0 · Фундамент

- [x] [P0] **TASK-001 · git, скаффолд, ADR-000/001, CI**
- [x] [P0] **TASK-002 · Спайк asyncua (ADR-002)** — открыто: Docker (TASK-302), UaExpert A&C (приёмка)
  - endpoint за NAT/Docker: GetEndpoints и CreateSession/ActivateSession с публичным хостом
  - HistoryRead до остановки сервера: внутренняя запись и запись внешним клиентом без SourceTimestamp
  - ExclusiveLimitAlarmType: какие поля доходят до asyncua.Client, ModbusToolkit, UaExpert
  - `InternalSession._current_connections` как источник `client_count`

## Эпик 1 · Ядро OPC UA

- [x] [P0] **TASK-101 · `config.py`, `tags.py` (схемы тегов/алармов), `generators.py` + unit-тесты**
- [x] [P0] **TASK-102 · `PortAllocator` + unit-тесты (конкурентность на потоках)**
- [x] [P0] **TASK-103 · `address_space.py`: узлы, обновление, writable, `SendBoxHistory`, `AlarmEvaluator`**
- [x] [P0] **TASK-104 · `worker.py` + `worker_handle.py`: процесс, IPC, backpressure telemetry**
- [x] [P0] **TASK-105 · `SessionManager` + `keys.py`: лимиты, reaper, crash, общий сервер**

## Эпик 2 · Web-слой

- [x] [P0] **TASK-201 · REST API (`routes.py`, `models.py`) + rate limit**
- [ ] [P0] **TASK-202 · WebSocket (`ws.py`, `state_bus.py`)**
- [ ] [P0] **TASK-203 · UI: каркас, «Обзор», «Моя сессия», «Документация», Swagger, промо и донат**

## Эпик 3 · Эксплуатация

- [ ] [P1] **TASK-301 · Логи `LOG_FORMAT`, `/metrics`, `/healthz`**
- [ ] [P1] **TASK-302 · Docker/compose, nginx, systemd, `docs/DEPLOY.md`**
- [ ] [P1] **TASK-303 · README, обновление CLAUDE.md**
