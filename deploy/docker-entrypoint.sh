#!/bin/sh
# Права на каталог сертификатов, затем работа под appuser (ADR-004).
#
# Именованный том Docker создаётся с владельцем root, а приложение в контейнере
# работает под непривилегированным appuser: самоподписанный сертификат сервера
# пришлось бы генерировать в каталог, куда запись закрыта. Поэтому контейнер
# стартует от root, точка входа выставляет права один раз и сбрасывает
# привилегии.
set -e

certs_dir="${OPCUA_CERTS_DIR:-/data/certs}"
mkdir -p "$certs_dir"
chown -R appuser:appuser "$certs_dir"

exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
