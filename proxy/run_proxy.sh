#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Explicit interpreter works with a 0644 checkout. The Python supervisor preflights
# configuration/imports, stages its socket privately, then journals the takeover.
exec /usr/bin/python3 "${BASE_DIR}/proxy/takeover.py" run --base "${BASE_DIR}"
