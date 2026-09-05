#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BINARY="${BINARY:-$SCRIPT_DIR/dist/adt-agent}"

INSTALL_DIR=/opt/vizhi-agent
DATA_DIR=/var/lib/vizhi-agent
SERVICE_NAME=adt-agent

if [[ ! -f "$BINARY" ]]; then
  echo "Binary not found: $BINARY. Build first with VIZHI_API_BASE set." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp "$BINARY" "$INSTALL_DIR/adt-agent"
chmod 750 "$INSTALL_DIR/adt-agent"
chown root:root "$INSTALL_DIR/adt-agent"

cat > /etc/systemd/system/adt-agent.service << 'EOF'
[Unit]
Description=Vizhi ADT Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/vizhi-agent/adt-agent
Restart=on-failure
RestartSec=60
StandardInput=null
StandardOutput=journal
StandardError=journal
SyslogIdentifier=adt-agent
WorkingDirectory=/var/lib/vizhi-agent
Environment=HOME=/root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable adt-agent

echo ""
echo "=== Vizhi ADT Agent enrollment ==="
"$INSTALL_DIR/adt-agent"

systemctl start adt-agent

echo ""
echo "Installation complete."
echo "Agent status: $(systemctl is-active adt-agent)"
echo "Logs: journalctl -u adt-agent -f"
