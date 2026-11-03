# ADR-004 · SecurityPolicy Basic256Sha256 и вход по логину/паролю

- **Статус:** принято; спайк 6 PASS / 0 FAIL
- **Дата:** 2026-11-03
- **Задача:** TASK-107
- **Версия:** asyncua 2.1.0 (`uv.lock`), Python 3.14, Windows 11
- **Скрипт:** `scripts/spikes/spike_security.py` (автоматика), `--serve` — ручной стенд

## Решения (выбраны владельцем 2026-11-03)

1. **Обе политики сосуществуют**: `None` (прежние клиенты продолжают работать) и
   `Basic256Sha256` в режимах `Sign` и `SignAndEncrypt`.
2. **Демо-учётки общие** `sendbox` / `sendbox`, напечатаны на странице и в доках.
   Регистрации нет; пара настраивается env `OPCUA_DEMO_USER` / `OPCUA_DEMO_PASSWORD`.
3. **Самоподписанный сертификат генерируется автоматически** при первом старте,
   один на весь сервис (общий сервер и все сессии доверяют один отпечаток),
   хранится в `OPCUA_CERTS_DIR` (в Docker — том, чтобы отпечаток переживал выкладки).
4. **Шифрование и логин везде одинаково**: общий сервер и личные сессии.

## 1. Сертификат: cert_gen без openssl

**Факт.** `asyncua.crypto.cert_gen.setup_self_signed_certificate(key_file, cert_file,
app_uri, host_name, cert_use, subject_attrs)` — async, генерирует ключ и сертификат без
внешнего openssl. **Пишет сертификат в DER**, хотя низкоуровневые функции модуля
работают с объектами `cryptography`. `uacrypto.load_certificate` выбирает парсер по
расширению файла, поэтому сертификат обязан называться `*.der` (ключ — `*.pem`):
файл `server_cert.pem` с DER-содержимым повторная загрузка роняет
`ValueError: MalformedFraming`.

**Решение.** Генерация в одном месте (родительский процесс при старте, воркеры только
читают файлы) — иначе конкурентные воркеры перезапишут пару ключ/сертификат внахлёст.

**Связка с ADR-002.** `app_uri` сертификата должен совпадать с
`set_application_uri(...)`, иначе клиенты пишут предупреждение «certificate does not
contain the application uri». `host_name` — `OPCUA_PUBLIC_HOST`; клиенты, подключающиеся
по другому имени (localhost в разработке), получат предупреждение о hostname и диалог
доверия — для самоподписанного сертификата это нормально.

## 2. Вход: свой UserManager, анонимов пускать явно

**Факт.** Менеджер ставится через `server.iserver.set_user_manager(UserManager())`
(приватный API — в ADR-000). `get_user(iserver, username, password, certificate)`
asyncua вызывает **и для AnonymousIdentityToken** (`username=None`): если вернуть
`None`, анонимный клиент получит `BadUserAccessDenied`, то есть старые клиенты без
логина перестанут подключаться.

**Решение.** `DemoUserManager` в `app/opcua/security.py`:

- `username is None` → `User(role=UserRole.User, name="anonymous")` — анонимный вход
  остаётся наравне с логином (по решению 1 обе политики доступны, никто не обязан
  логиниться);
- `sendbox` / `sendbox` (из конфига) → `User(...)`;
- всё остальное → `None`, клиент получает `BadUserAccessDenied` — проверено спайком.

## 3. Политики: Sign и SignAndEncrypt — разные режимы

**Факт.** `ua.SecurityPolicyType.Basic256Sha256_Sign` и
`Basic256Sha256_SignAndEncrypt` — разные члены перечисления; сервер отдаёт endpoint
только для явно перечисленных режимов. В спайке сервер с одним `SignAndEncrypt`
отклонял клиента в режиме `Sign` ошибкой `No matching endpoints` — а UaExpert часто
выбирает именно Sign.

**Решение.** Перечисляем оба режима:

```python
server.set_security_policy([
    ua.SecurityPolicyType.NoSecurity,
    ua.SecurityPolicyType.Basic256Sha256_Sign,
    ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
])
server.set_identity_tokens([ua.AnonymousIdentityToken, ua.UserNameIdentityToken])
```

`set_security_IDs` — deprecated, не использовать.

## 4. Проверки спайка

| Проверка | Результат |
|---|---|
| cert_gen: ключ + сертификат без openssl | PASS: `server_key.pem` + `server_cert.der` |
| GetEndpoints: обе политики | PASS: `Basic256Sha256` и `None` в ответе |
| None + аноним | PASS: тег читается, прежние клиенты работают |
| Basic256Sha256 SignAndEncrypt + логин/пароль | PASS: тег читается |
| Basic256Sha256 Sign + логин/пароль | PASS: тег читается |
| неверный пароль | PASS: отклонён `BadUserAccessDenied` на ActivateSession |

Связка с ADR-002 сохранилась: `socket_address = (None, port)` +
`set_match_discovery_client_ip(False)` работают и с шифрованием; GetEndpoints эхоит
EndpointUrl клиента, при пустом — отдаёт публичный хост.

## 5. Что меняется в коде

- `app/opcua/security.py` (новый): `DemoUserManager`, `ensure_server_cert`;
- `config.py`: `OPCUA_CERTS_DIR`, `OPCUA_DEMO_USER`, `OPCUA_DEMO_PASSWORD`;
- `address_space.py::create_server`: политики, identity tokens, `load_certificate` /
  `load_private_key`, `set_user_manager`;
- `worker.py` / `session_manager.py`: пути сертификата и демо-учётки в `WorkerParams`;
- `routes.py` FEATURES: плюс `security_basic256sha256` и `username_identity`;
- UI/README: плашка и «Безопасность» вместо «появятся в следующей версии»;
- тест `test_get_endpoints_announces_public_host` переписывается: теперь сервер отдаёт
  обе политики, а не только `None`.
