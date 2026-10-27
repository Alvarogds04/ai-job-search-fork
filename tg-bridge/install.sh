#!/usr/bin/env bash
# Installs the systemd user units for the Telegram job-search bridge.
# Does NOT enable/start anything: the bot token must be configured first.

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
UNITS_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNITS_DIR"

cat > "$UNITS_DIR/busqueda-tg.service" <<EOF
[Unit]
Description=Telegram job-search bridge bot
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 ${BRIDGE_DIR}/bot.py
WorkingDirectory=${BRIDGE_DIR}
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
EOF

cat > "$UNITS_DIR/busqueda-search.service" <<EOF
[Unit]
Description=Daily Turijobs search + fit evaluation (job-search bridge)
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 ${BRIDGE_DIR}/search_daily.py
WorkingDirectory=${BRIDGE_DIR}
EOF

cat > "$UNITS_DIR/busqueda-search.timer" <<EOF
[Unit]
Description=Run the daily job search at 09:00

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload

cat <<'NEXT'

Units installed (NOT enabled). Next steps:
  1. Put TG_BOT_TOKEN (and NAN_API_KEY) in tg-bridge/state/.env
     (copy state/.env.example as a starting point).
  2. Start the bot and message it once from the candidate's Telegram to /start:
       systemctl --user enable --now busqueda-tg.service
  3. Lock the chat: put the registered chat_id as CHAT_ID in state/.env
     and restart the service.
  4. Enable the daily search:
       systemctl --user enable --now busqueda-search.timer
  5. Manual test run without Telegram:
       python3 search_daily.py
NEXT
