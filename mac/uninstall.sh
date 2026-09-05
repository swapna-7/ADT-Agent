#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="$HOME/Library/Application Support/ADT Agent"
INSTALL_BIN="$INSTALL_ROOT/adt-agent"
LAUNCH_LABEL="com.adt.agent"
LAUNCH_PLIST="$HOME/Library/LaunchAgents/${LAUNCH_LABEL}.plist"
UID="$(id -u)"

launchctl bootout "gui/${UID}/${LAUNCH_LABEL}" 2>/dev/null || true
launchctl unload -w "$LAUNCH_PLIST" 2>/dev/null || true
rm -f "$LAUNCH_PLIST"
rm -f "$INSTALL_BIN"

echo "ADT Agent uninstalled. Data preserved at:"
echo "  $INSTALL_ROOT"
