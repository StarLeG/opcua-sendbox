# Образ OPC UA SendBox (multi-stage, как в Modbus SendBox).
#
# Сборка:  docker build -t opcua-sendbox .
# Запуск:  docker compose up -d   (см. docker-compose.yml)
#
# Стадия builder готовит venv через uv (файл uv.lock — источник версий),
# runtime-стадия минимальная: python-slim без тулчейна и исходников.
# Внутренние порты фиксированы (WEB_PORT=8766, OPC UA 4840 и 4841–5000),
# наружу маппятся в docker-compose.yml.

# --- builder: зависимости ----------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Сначала только метаданные проекта: слой зависимостей кэшируется,
# пока не меняются pyproject.toml / uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Исходники и установка самого пакета (app/) в venv. Без --no-editable uv
# ставит проект ссылкой на /build, которой в runtime-стадии нет.
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

# --- runtime: минимальный образ ----------------------------------------------
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="opcua-sendbox" \
      org.opencontainers.image.description="OPC UA SendBox - публичная песочница OPC UA (opcua-sendbox.electroman.by)" \
      org.opencontainers.image.source="https://github.com/StarLeG/opcua-sendbox"

# Приложение работает под непривилегированным пользователем.
RUN useradd --create-home --uid 10001 appuser

# Каталог сертификатов в образе принадлежит appuser: новый именованный том
# наследует эти права, а существующий чинит точка входа (ADR-004).
RUN mkdir -p /data/certs && chown appuser:appuser /data/certs

COPY --from=builder /opt/venv /opt/venv
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Порт только в документации EXPOSE: реальный маппинг — docker-compose.yml.
EXPOSE 4840-5000/tcp 8766/tcp

# Старт от root: точка входа выставляет права на каталог сертификатов
# и передаёт управление appuser (deploy/docker-entrypoint.sh).
USER root
WORKDIR /home/appuser

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app"]
