#!/usr/bin/env bash
# SpaceX Calendar Sync — HostGator setup
# Run this once via SSH to install dependencies and configure the cron job.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
LOG_FILE="$SCRIPT_DIR/sync.log"

# ── 1. Create a Python virtual environment ────────────────────────────────────
echo "==> Creating virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
echo "    Done — $(${VENV_DIR}/bin/python --version)"

# ── 2. Check for .env ─────────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo ""
    echo "ACTION REQUIRED: Edit .env and fill in your values:"
    echo "  $SCRIPT_DIR/.env"
    echo ""
    echo "  GOOGLE_CALENDAR_ID           — your SpaceX calendar ID"
    echo "  GOOGLE_APPLICATION_CREDENTIALS — full path to your service account key JSON"
    echo ""
fi

# ── 3. Test the script manually before setting up cron ───────────────────────
echo "==> Running a test sync (check output for errors)..."
set -o allexport; source "$SCRIPT_DIR/.env"; set +o allexport
"$VENV_DIR/bin/python" "$SCRIPT_DIR/main.py"

# ── 4. Install the cron job ───────────────────────────────────────────────────
# Runs daily at 06:00 server time. Edit the schedule as needed.
CRON_SCHEDULE="0 6 * * *"
CRON_CMD="$CRON_SCHEDULE cd $SCRIPT_DIR && set -o allexport && source .env && set +o allexport && $VENV_DIR/bin/python main.py >> $LOG_FILE 2>&1"

echo ""
echo "==> Installing cron job..."
# Append only if the line isn't already there
(crontab -l 2>/dev/null | grep -qF "$SCRIPT_DIR/main.py") \
    && echo "    Cron job already exists, skipping." \
    || (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -

echo "    Installed: $CRON_SCHEDULE"
echo ""
echo "Verify with:  crontab -l"
echo "Watch logs:   tail -f $LOG_FILE"
