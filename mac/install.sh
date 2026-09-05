#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BINARY="${BINARY:-$SCRIPT_DIR/dist/adt-agent}"

INSTALL_ROOT="$HOME/Library/Application Support/ADT Agent"
INSTALL_BIN="$INSTALL_ROOT/adt-agent"
DATA_DIR="$INSTALL_ROOT"
LAUNCH_LABEL="com.adt.agent"
LAUNCH_PLIST="$HOME/Library/LaunchAgents/${LAUNCH_LABEL}.plist"
LAUNCH_OUT="$INSTALL_ROOT/launchd.stdout.log"
LAUNCH_ERR="$INSTALL_ROOT/launchd.stderr.log"

if [[ ! -f "$BINARY" ]]; then
  echo "Binary not found: $BINARY. Build first with VIZHI_API_BASE set." >&2
  exit 1
fi

mkdir -p "$INSTALL_ROOT" "$HOME/Library/LaunchAgents"
cp "$BINARY" "$INSTALL_BIN"
chmod 755 "$INSTALL_BIN"

cat > "$LAUNCH_PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${INSTALL_BIN}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LAUNCH_OUT}</string>
  <key>StandardErrorPath</key>
  <string>${LAUNCH_ERR}</string>
</dict>
</plist>
EOF

UID="$(id -u)"
launchctl bootout "gui/${UID}/${LAUNCH_LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/${UID}" "$LAUNCH_PLIST" 2>/dev/null || launchctl load -w "$LAUNCH_PLIST"

echo ""
echo "=== Vizhi ADT Agent enrollment ==="
"$INSTALL_BIN"

launchctl kickstart -k "gui/${UID}/${LAUNCH_LABEL}" 2>/dev/null || true

echo ""
echo "Installation complete."
echo "Agent binary: $INSTALL_BIN"
echo "Logs: $INSTALL_ROOT/agent.log"
