# Деплой OPC UA SendBox

Сервис — один процесс Python (FastAPI + uvicorn) и по дочернему процессу на каждый
OPC UA сервер: общий демо-сервер и личные сессии (ADR-001). Состояние только в
памяти: перезапуск закрывает сессии и сбрасывает ключи.

## Что открыть наружу

| Порт | Протокол | Назначение |
|---|---|---|
| 80 / 443 | HTTP(S) | веб-интерфейс, REST, WebSocket (через nginx или напрямую на 8766) |
| 4840 | opc.tcp | общий демо-сервер |
| 4841–5000 | opc.tcp | личные сессии (диапазон `OPCUA_SESSION_PORT_MIN/MAX`) |

DNS: `opcua-sendbox.electroman.by` → IP сервера. Тот же хост задаётся в
`OPCUA_PUBLIC_HOST`: сервер объявляет его клиентам в GetEndpoints (ADR-002 п. 1),
и клиенты переподключаются именно на него.

Firewall (пример ufw):

```bash
ufw allow 80,443/tcp
ufw allow 4840:5000/tcp
```

## Вариант 1. Docker Compose (рекомендуется)

```bash
cp .env.example .env          # поправьте OPCUA_PUBLIC_HOST и лимиты
docker compose up -d --build
docker compose ps             # health: healthy
curl -s http://127.0.0.1:8766/healthz
```

Порты публикуются один к одному (`4840-5000:4840-5000`): endpoint, который сервер
объявляет клиентам, содержит тот же номер порта, поэтому переназначать порты нельзя.

Внутри контейнера `OPCUA_BIND_HOST=0.0.0.0`: Docker на сетях без IPv6 отключает его в
контейнере, а внешний IPv4/IPv6 обслуживает сам Docker.

**161 публикация порта.** Работает, но Docker запускает docker-proxy на каждый порт.
Для продакшена — один из вариантов:

- `"userland-proxy": false` в `/etc/docker/daemon.json` (порты через iptables, без docker-proxy);
- `network_mode: host` в `docker-compose.yml` (вместо `ports`); тогда задайте
  `OPCUA_BIND_HOST=` (пусто — все интерфейсы, IPv4 и IPv6) и `WEB_HOST=127.0.0.1`,
  если web смотрит наружу через nginx на хосте.

### Прокси-режим: web через nginx

```bash
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
```

nginx (`nginx/nginx.conf`) проксирует HTTP и WebSocket на приложение, OPC UA идёт
мимо него. TLS — certbot на хосте или `listen 443 ssl` в `nginx.conf`.
`FORWARDED_ALLOW_IPS="*"` в этом режиме безопасен: web-порт приложения не
опубликован, X-Forwarded-For приходит только от nginx.

## Вариант 2. systemd без Docker

```bash
useradd --system --home /opt/opcua-sendbox --shell /usr/sbin/nologin opcua-sendbox
git clone https://github.com/StarLeG/opcua-sendbox.git /opt/opcua-sendbox
cd /opt/opcua-sendbox && uv sync --frozen --no-dev
install -d /etc/opcua-sendbox && cp .env.example /etc/opcua-sendbox/env   # поправьте
cp deploy/opcua-sendbox.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now opcua-sendbox
journalctl -u opcua-sendbox -f
```

`KillMode=mixed`: SIGTERM получает главный процесс, он штатно закрывает сессии;
через `TimeoutStopSec` systemd добивает оставшиеся воркеры. Воркеры и без этого
завершаются сами, если родитель умер (ADR-001).

На хосте с OPC Foundation LDS порт 4840 занят — задайте другой `OPCUA_SHARED_PORT`
(ADR-002, замечание о порте 4840).

## Наблюдение

- `GET /healthz` — 200, пока общий сервер работает, иначе 503 (для балансировщика и Docker).
- `GET /metrics` — Prometheus: сессии, закрытия по причинам, свободные порты,
  клиенты, перезапуски общего сервера, дропы телеметрии.
- `python scripts/healthcheck.py --url http://127.0.0.1:8766/healthz --opcua-port 4840`
  — код выхода 0/1 для cron и мониторинга.
- `LOG_FORMAT=json` — одна строка JSON на событие (journald, `docker logs`, Loki).

Тревожные признаки: `sendbox_ports_free` около нуля, рост
`sendbox_session_start_failures_total` или `sendbox_sessions_closed_total{reason="crashed"}`,
`sendbox_shared_up 0`.

## Обновление

```bash
git pull && docker compose up -d --build     # или: uv sync --frozen --no-dev && systemctl restart opcua-sendbox
```

Перезапуск закрывает все личные сессии — делайте его в тихое время.

## Чек-лист приёмки перед выкладкой

1. `curl /healthz` → 200, `curl /metrics` → `sendbox_shared_up 1`.
2. **Endpoint за NAT/Docker (ADR-002 п. 1):** с внешней машины
   `GetEndpoints` на `opc.tcp://opcua-sendbox.electroman.by:4840/electroman/` отдаёт
   ровно этот адрес — без IP контейнера и без `0.0.0.0`; UaExpert подключается.
3. **UaExpert, Alarms & Conditions View (ADR-002 п. 3b):** подписка на Server общего
   сервера — алармы SineHigh и Heartbeat видны, переходы активен/норма отображаются,
   ConditionRefresh при открытии вида показывает активные; в журнале UaExpert нет ошибок.
4. ModbusToolkit, вкладка OPC UA: дерево, тренд, «История» и «События» на общем сервере.
5. Веб-интерфейс: создать личную сессию, подключиться к выданному endpoint, записать
   `Setpoint`, увидеть запись в журнале страницы, закрыть сессию.
6. Перезапуск: `docker compose restart` — сессии закрываются, общий сервер поднимается,
   `/healthz` снова 200.
