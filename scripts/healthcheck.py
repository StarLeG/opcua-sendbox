"""Проверка живости сервиса (systemd, Docker HEALTHCHECK, ручная диагностика).

Проверяет `GET /healthz` (web жив и общий сервер работает) и по желанию TCP-порт
общего OPC UA сервера. Код выхода 0 — всё в порядке, 1 — проверка не прошла.
Только stdlib: скрипт работает и в минимальном образе.

    uv run python scripts/healthcheck.py
    uv run python scripts/healthcheck.py --url http://127.0.0.1:8766/healthz --opcua-port 4840
"""
from __future__ import annotations

import argparse
import socket
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 5.0


def check_healthz(url: str) -> str | None:
    """Текст ошибки или None, если /healthz отвечает 2xx."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
            if 200 <= response.status < 300:
                return None
            return f"{url}: HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"{url}: HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return f"{url}: {exc}"


def check_tcp(host: str, port: int) -> str | None:
    """Текст ошибки или None, если порт принимает TCP."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S):
            return None
    except OSError as exc:
        return f"opc.tcp {host}:{port}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8766/healthz")
    parser.add_argument("--opcua-host", default="127.0.0.1")
    parser.add_argument("--opcua-port", type=int, default=None, help="проверить TCP-порт общего сервера")
    args = parser.parse_args(argv)
    errors = [check_healthz(args.url)]
    if args.opcua_port is not None:
        errors.append(check_tcp(args.opcua_host, args.opcua_port))
    failed = [e for e in errors if e]
    for error in failed:
        print(f"FAIL {error}", file=sys.stderr)
    if not failed:
        print("OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
