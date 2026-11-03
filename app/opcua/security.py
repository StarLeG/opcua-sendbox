"""SecurityPolicy Basic256Sha256 и вход по логину/паролю (ADR-004).

Сертификат сервера — самоподписанный, генерируется при первом старте в
`OPCUA_CERTS_DIR` и переживает перезапуски (один отпечаток на весь сервис).
Демо-учётки одни на общий сервер и личные сессии; регистрации нет.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asyncua.crypto import cert_gen
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.internal_server import InternalServer
from asyncua.server.user_managers import UserManager
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.config import Config

#: ApplicationUri самоподписанного сертификата = `APPLICATION_URI` address_space.
CERT_APP_URI = "urn:electroman:opcua-sendbox:server"


@dataclass(frozen=True)
class ServerSecurity:
    """Шифрование и вход сервера: каталог сертификата и демо-учётки."""

    certs_dir: Path
    demo_user: str
    demo_password: str

    @classmethod
    def from_config(cls, config: Config) -> ServerSecurity:
        """Сертификат и демо-учётки из переменных окружения (ADR-004)."""
        return cls(
            certs_dir=Path(config.OPCUA_CERTS_DIR),
            demo_user=config.OPCUA_DEMO_USER,
            demo_password=config.OPCUA_DEMO_PASSWORD,
        )


class DemoUserManager(UserManager):
    """Анонимный вход и одна пара логин/пароль на весь сервис (ADR-004 п. 2).

    asyncua зовёт `get_user` и для AnonymousIdentityToken (`username=None`):
    анонимов нужно пускать явно, иначе старые клиенты без логина получат
    BadUserAccessDenied. Неверная пара — тоже `None`, клиент отклоняется.
    """

    def __init__(self, user: str, password: str) -> None:
        self._user = user
        self._password = password

    def get_user(
        self,
        iserver: InternalServer,
        username: str | None = None,
        password: str | None = None,
        certificate: Any = None,
    ) -> User | None:
        if username is None:
            return User(role=UserRole.User, name="anonymous")
        if username == self._user and password == self._password:
            return User(role=UserRole.User, name=username)
        return None


async def ensure_server_cert(certs_dir: Path, *, host_name: str, app_uri: str) -> tuple[Path, Path]:
    """Самоподписанный сертификат сервера; генерация только при отсутствии.

    Пара ключ/сертификат собирается во временном каталоге и переименовывается —
    конкурентные воркеры не перетрут файлы друг друга внахлёст: из гонки
    переименований выигрывает один, остальные подхватывают его файлы.

    Файл сертификата с расширением `.der`: `cert_gen` пишет DER-содержимое,
    а `uacrypto.load_certificate` выбирает парсер по расширению (ADR-004 п. 1).
    """
    target = certs_dir / "server"
    key_file = target / "server_key.pem"
    cert_file = target / "server_cert.der"
    if key_file.exists() and cert_file.exists():
        return cert_file, key_file
    certs_dir.mkdir(parents=True, exist_ok=True)
    tmp = certs_dir / f".gen-{uuid.uuid4().hex[:12]}"
    tmp.mkdir()
    try:
        await cert_gen.setup_self_signed_certificate(
            tmp / key_file.name,
            tmp / cert_file.name,
            app_uri=app_uri,
            host_name=host_name,
            cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
            subject_attrs={"commonName": "OPC UA SendBox"},
        )
        try:
            tmp.rename(target)
        except OSError:
            # гонку выиграл другой воркер — его файлы уже на месте
            shutil.rmtree(tmp, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return cert_file, key_file
