#!/bin/sh
set -eu
IOT_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$IOT_PROJECT_DIR"
git pull --ff-only
exec sh ./install.sh "$@"
