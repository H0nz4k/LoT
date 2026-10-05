#!/bin/sh
set -eu
IOT_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$IOT_PROJECT_DIR/install.py" "$@"
