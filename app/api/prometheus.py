"""Экспозиция метрик в формате Prometheus (как в Modbus SendBox).

Текстовый формат 0.0.4 без сторонних зависимостей: метрик немного, проще отдать
строкой. Рендер — чистая функция над SessionManager (тестируется без сервера).
"""
from __future__ import annotations

from app.api.stats import RequestStats
from app.opcua.session_manager import SessionManager

#: Медиа-тип по спецификации Prometheus.
MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Причины закрытия сессий, которые выводятся всегда (нули — тоже).
CLOSE_REASONS = ("deleted", "idle", "lifetime", "crashed", "shutdown")


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(manager: SessionManager, stats: RequestStats) -> str:
    """Собрать текст экспозиции из текущего состояния менеджера и счётчиков HTTP."""
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value:g}")

    sessions = manager.sessions()
    allocator = manager.allocator
    shared = manager.shared

    metric("sendbox_sessions_active", "gauge", "Личные сессии в реестре.", [("", len(sessions))])
    metric("sendbox_sessions_created_total", "counter", "Созданные сессии с запуска.", [("", manager.created_total)])
    metric(
        "sendbox_sessions_closed_total",
        "counter",
        "Закрытые сессии по причинам с запуска.",
        [(f'{{reason="{reason}"}}', manager.closed_total.get(reason, 0)) for reason in CLOSE_REASONS],
    )
    metric(
        "sendbox_session_start_failures_total",
        "counter",
        "Сессии, сервер которых не запустился.",
        [("", manager.start_failures_total)],
    )
    metric("sendbox_ports_free", "gauge", "Свободные порты личных сессий.", [("", allocator.capacity - len(allocator.in_use))])
    metric(
        "sendbox_opcua_clients",
        "gauge",
        "Подключённые OPC UA сессии клиентов.",
        [('{server="shared"}', shared.clients), ('{server="sessions"}', sum(s.clients for s in sessions))],
    )
    metric("sendbox_shared_up", "gauge", "Общий сервер работает (1) или нет (0).", [("", 1 if shared.status == "running" else 0)])
    metric("sendbox_shared_restarts_total", "counter", "Перезапуски общего сервера после падения.", [("", shared.restarts)])
    metric("sendbox_api_keys", "gauge", "Выданные и ещё не забытые API-ключи.", [("", len(manager.keys))])
    metric(
        "sendbox_ipc_telemetry_dropped_total",
        "counter",
        "Снимки телеметрии, выброшенные из-за медленного чтения (ADR-001 п. 5).",
        [
            (f'{{session="{_label(s.id)}"}}', getattr(s.handle, "telemetry_dropped", 0))
            for s in sessions
            if s.handle is not None
        ],
    )
    metric("sendbox_http_requests_total", "counter", "HTTP-ответы web-процесса с запуска.", [("", stats.total)])
    metric(
        "sendbox_http_errors_total",
        "counter",
        "HTTP-ответы со статусом 400 и выше с запуска.",
        [("", stats.errors)],
    )
    metric("sendbox_uptime_seconds", "gauge", "Время работы сервиса, с.", [("", round(manager.uptime(), 1))])
    return "\n".join(lines) + "\n"
