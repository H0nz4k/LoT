#!/usr/bin/env bash
# Fail closed until the actual HUB configuration and rollback are verified.
set -eu
echo 'Instalace kamery zatím není připravena: ověřte produkční konfiguraci, zálohu a návrat po chybě.' >&2
echo 'Žádná služba ani data nebyly změněny. Viz deploy/PRODUCTION-C320WS.md.' >&2
exit 1
