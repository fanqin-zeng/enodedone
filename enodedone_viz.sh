#!/usr/bin/env sh
set -eu
ENODEDONE_VIZ_SDK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${ENODEDONE_PYTHON:-python3}" -u "$ENODEDONE_VIZ_SDK_DIR/enodedone_viz.py" "$@"
