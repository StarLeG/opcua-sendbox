"""Спайк ADR-004: SecurityPolicy Basic256Sha256 + логин/пароль до начала реализации.

Автоматические проверки (печатают PASS/FAIL и факты для ADR):
  1. автогенерация самоподписанного сертификата сервера (cert_gen);
  2. сервер с endpoint'ами: None и Basic256Sha256 в режимах Sign и SignAndEncrypt;
  3. UserNameIdentityToken с демо-учётками через свой UserManager;
  4. клиент по Basic256Sha256 с логином/паролем читает тег;
  5. клиент анонимно по None продолжает работать (совместимость);
  6. неверный пароль отклоняется, а не пускает молча;
  7. GetEndpoints отдаёт оба endpoint'а с публичным хостом (связка с ADR-002);
  8. режим Basic256Sha256 Sign тоже работает (UaExpert часто выбирает его).

Ручная проверка (UaExpert): `--serve` поднимает тот же сервер и печатает
данные для подключения (endpoint, логин/пароль, отпечаток сертификата).

Запуск:  uv run python scripts/spikes/spike_security.py [--serve]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
from pathlib import Path

from asyncua import Client, Server, ua
from asyncua.crypto import cert_gen
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.user_managers import UserManager
from cryptography.x509.oid import ExtendedKeyUsageOID

NAMESPACE = "urn:electroman:opcua-sendbox"
PUBLIC_HOST = "opcua-sendbox.electroman.by"
PORT = 4851
#: Клиенты спайка ходят на 127.0.0.1, сервер объявляет PUBLIC_HOST (приём ADR-002).
CLIENT_URL = f"opc.tcp://127.0.0.1:{PORT}/electroman/"
DEMO_USER = "sendbox"
DEMO_PASSWORD = "sendbox"

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    """Запомнить и напечатать результат проверки."""
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class DemoUserManager(UserManager):
    """Демо-учётки: одна пара логин/пароль + анонимный вход, как решили для песочницы.

    Факт для ADR: asyncua зовёт get_user и для AnonymousIdentityToken (username=None),
    поэтому «анимус пускать» нужно явно - иначе BadUserAccessDenied на старых клиентах.
    """

    def get_user(
        self,
        iserver: object,
        username: str | None = None,
        password: str | None = None,
        certificate: object = None,
    ) -> User | None:
        if username is None:
            return User(role=UserRole.User, name="anonymous")
        if username == DEMO_USER and password == DEMO_PASSWORD:
            return User(role=UserRole.User, name=username)
        return None


async def make_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Сгенерировать самоподписанный сертификат сервера, вернуть (key, cert).

    Файл сертификата с расширением .der: setup_self_signed_certificate пишет
    DER-содержимое, а uacrypto.load_certificate выбирает парсер по расширению.
    """
    key_file = cert_dir / "server_key.pem"
    cert_file = cert_dir / "server_cert.der"
    await cert_gen.setup_self_signed_certificate(
        key_file,
        cert_file,
        app_uri="urn:electroman:opcua-sendbox:server",
        host_name=PUBLIC_HOST,
        cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
        subject_attrs={"commonName": "OPC UA SendBox"},
    )
    return key_file, cert_file


async def make_server(cert_dir: Path) -> tuple[Server, Path, Path, str]:
    """Сервер с двумя политиками и демо-учётками; вернуть (server, cert, key, node_id)."""
    key_file, cert_file = await make_cert(cert_dir)
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://{PUBLIC_HOST}:{PORT}/electroman/")
    # Как в address_space.py (ADR-002): bind на все интерфейсы, endpoint объявляет
    # публичный хост - иначе asyncua bind'ится на резолвнутый IP и падает за NAT.
    server.socket_address = (None, PORT)  # type: ignore[assignment]
    server.set_match_discovery_client_ip(False)
    server.set_server_name("OPC UA SendBox spike security")
    # Факт для ADR: Sign и SignAndEncrypt - разные члены SecurityPolicyType,
    # сервер отдаёт только явно перечисленные режимы (UaExpert часто выбирает Sign).
    server.set_security_policy([
        ua.SecurityPolicyType.NoSecurity,
        ua.SecurityPolicyType.Basic256Sha256_Sign,
        ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
    ])
    server.set_identity_tokens([ua.AnonymousIdentityToken, ua.UserNameIdentityToken])
    server.iserver.set_user_manager(DemoUserManager())
    await server.load_certificate(cert_file)
    await server.load_private_key(key_file)
    idx = await server.register_namespace(NAMESPACE)
    objects = server.nodes.objects
    node_id = f"ns={idx};s=Tags.Sine"
    tag = await objects.add_variable(node_id, "Tags.Sine", 42.0)
    await tag.set_writable()
    return server, cert_file, key_file, node_id


async def check_cert_generation(cert_dir: Path) -> Path:
    """1. cert_gen генерирует ключ и сертификат без внешнего openssl."""
    key_file, cert_file = await make_cert(cert_dir)
    ok = key_file.exists() and cert_file.exists() and cert_file.stat().st_size > 0
    report("cert_gen", ok, f"key={key_file.name} cert={cert_file.name} ({cert_file.stat().st_size} байт)")
    return cert_file


async def check_endpoints(server: Server) -> None:
    """7. GetEndpoints отдаёт оба endpoint'а."""
    endpoints = await Client(CLIENT_URL).connect_and_get_server_endpoints()
    urls = {str(ep.EndpointUrl) for ep in endpoints}
    policies = {str(ep.SecurityPolicyUri) for ep in endpoints}
    report(
        "GetEndpoints",
        any("Basic256Sha256" in p for p in policies) and any("None" in p for p in policies),
        f"policy={sorted(policies)} url={sorted(urls)}",
    )


async def check_none_anonymous(node_id: str) -> None:
    """5. Анонимный вход по None работает как раньше."""
    client = Client(CLIENT_URL)
    try:
        await client.connect()
        value = await client.get_node(node_id).read_value()
        report("None+Anonymous", value == 42.0, f"Sine={value}")
    finally:
        await client.disconnect()


async def check_basic256_login(node_id: str, cert_dir: Path) -> None:
    """4. Basic256Sha256 + логин/пароль: клиент читает тег."""
    client_cert = cert_dir / "client_cert.der"
    client_key = cert_dir / "client_key.pem"
    await cert_gen.setup_self_signed_certificate(
        client_key,
        client_cert,
        app_uri="urn:electroman:opcua-sendbox:spike-client",
        host_name="localhost",
        cert_use=[ExtendedKeyUsageOID.CLIENT_AUTH],
        subject_attrs={"commonName": "spike client"},
    )
    client = Client(CLIENT_URL)
    await client.set_security_string(f"Basic256Sha256,SignAndEncrypt,{client_cert},{client_key}")
    client.set_user(DEMO_USER)
    client.set_password(DEMO_PASSWORD)
    await client.connect()
    try:
        value = await client.get_node(node_id).read_value()
        report("Basic256Sha256+login", value == 42.0, f"Sine={value}")
    finally:
        await client.disconnect()


async def check_basic256_sign(node_id: str, cert_dir: Path) -> None:
    """4b. Режим Sign (без шифрования тела) тоже работает - UaExpert часто выбирает его."""
    client_cert = cert_dir / "client_cert.der"
    client_key = cert_dir / "client_key.pem"
    client = Client(CLIENT_URL)
    await client.set_security_string(f"Basic256Sha256,Sign,{client_cert},{client_key}")
    client.set_user(DEMO_USER)
    client.set_password(DEMO_PASSWORD)
    await client.connect()
    try:
        value = await client.get_node(node_id).read_value()
        report("Basic256Sha256+Sign", value == 42.0, f"Sine={value}")
    finally:
        await client.disconnect()


async def check_bad_password(cert_dir: Path) -> None:
    """6. Неверный пароль должен быть отклонён."""
    client_cert = cert_dir / "client_cert.der"
    client_key = cert_dir / "client_key.pem"
    client = Client(CLIENT_URL)
    await client.set_security_string(f"Basic256Sha256,SignAndEncrypt,{client_cert},{client_key}")
    client.set_user(DEMO_USER)
    client.set_password("wrong-password")
    try:
        await client.connect()
        report("bad password", False, "подключение прошло - плохо")
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001 — спайк отчитывается о любой ошибке
        name = type(exc).__name__
        code = getattr(getattr(exc, "code", None), "name", "")
        report("bad password", True, f"отклонён: {name} {code}")


async def step(name: str, coro: object) -> None:
    """Выполнить проверку, падение не должно срезать остальные."""
    try:
        await coro  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — падение одной проверки не срезает остальные
        report(name, False, f"исключение: {type(exc).__name__}: {exc}")


async def main(serve: bool) -> None:
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="sendbox-spike-sec-") as tmp:
        cert_dir = Path(tmp)
        await step("cert_gen", check_cert_generation(cert_dir))
        server, cert_file, _key_file, node_id = await make_server(cert_dir)
        async with server:
            await step("GetEndpoints", check_endpoints(server))
            await step("None+Anonymous", check_none_anonymous(node_id))
            await step("Basic256Sha256+login", check_basic256_login(node_id, cert_dir))
            await step("Basic256Sha256+Sign", check_basic256_sign(node_id, cert_dir))
            await step("bad password", check_bad_password(cert_dir))
            if serve:
                print()
                print("Ручная проверка. Данные для UaExpert:")
                print(f"  endpoint : opc.tcp://{PUBLIC_HOST}:{PORT}/electroman/")
                print("  Security : Basic256Sha256 / SignAndEncrypt (или None / None)")
                print(f"  login    : {DEMO_USER} / {DEMO_PASSWORD}")
                print(f"  сертификат сервера: {cert_file}")
                print("  доверие к сертификату - в UaExpert при подключении (Trust Server Certificate)")
                print("Ctrl+C - остановка.")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
        failed = [name for name, ok, _ in results if not ok]
        print()
        print(f"итого: {len(results) - len(failed)} PASS, {len(failed)} FAIL")
        if failed:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Спайк ADR-004: шифрование и логин/пароль")
    parser.add_argument("--serve", action="store_true", help="поднять сервер для ручной проверки")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.serve))
    except KeyboardInterrupt:
        pass
