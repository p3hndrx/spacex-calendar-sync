#!/usr/bin/env bash
# Cron wrapper — sources .env and runs main.py via the virtualenv.
# Called by crontab directly; avoids the fragility of sourcing inside cron.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/main.py"
