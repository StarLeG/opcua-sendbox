"""DemoUserManager и ensure_server_cert: демо-учётки и самоподписанный сертификат (ADR-004)."""
from __future__ import annotations

from pathlib import Path

from asyncua.crypto.permission_rules import User, UserRole

from app.opcua.security import DemoUserManager, ServerSecurity, ensure_server_cert


def test_demo_user_manager_accepts_demo_pair() -> None:
    manager = DemoUserManager("sendbox", "sendbox")
    user = manager.get_user(None, username="sendbox", password="sendbox")
    assert user == User(role=UserRole.User, name="sendbox")


def test_demo_user_manager_accepts_anonymous() -> None:
    """Анонимов пускаем явно: иначе asyncua отклоняет их как BadUserAccessDenied (ADR-004)."""
    manager = DemoUserManager("sendbox", "sendbox")
    user = manager.get_user(None, username=None)
    assert user == User(role=UserRole.User, name="anonymous")


def test_demo_user_manager_rejects_wrong_password() -> None:
    manager = DemoUserManager("sendbox", "sendbox")
    assert manager.get_user(None, username="sendbox", password="wrong") is None
    assert manager.get_user(None, username="someone", password="sendbox") is None


def test_server_security_from_config(tmp_path: Path) -> None:
    from dataclasses import replace

    from app.config import Config

    config = replace(Config(), OPCUA_CERTS_DIR=str(tmp_path / "certs"))
    security = ServerSecurity.from_config(config)
    assert security.certs_dir == tmp_path / "certs"
    assert (security.demo_user, security.demo_password) == ("sendbox", "sendbox")


async def test_ensure_server_cert_generates_and_reuses(tmp_path: Path) -> None:
    """Файлы создаются однажды и переиспользуются: второй вызов их не трогает."""
    cert_file, key_file = await ensure_server_cert(tmp_path, host_name="example.test", app_uri="urn:test")
    assert cert_file.suffix == ".der" and cert_file.exists()
    assert key_file.suffix == ".pem" and key_file.exists()
    first = (cert_file.stat().st_mtime_ns, key_file.stat().st_mtime_ns)
    second_cert, second_key = await ensure_server_cert(tmp_path, host_name="example.test", app_uri="urn:test")
    assert (second_cert, second_key) == (cert_file, key_file)
    assert (cert_file.stat().st_mtime_ns, key_file.stat().st_mtime_ns) == first
