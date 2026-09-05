#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

systemctl stop adt-agent 2>/dev/null || true
systemctl disable adt-agent 2>/dev/null || true
rm -f /etc/systemd/system/adt-agent.service
systemctl daemon-reload
rm -rf /opt/vizhi-agent

echo "ADT Agent uninstalled. Data preserved at /var/lib/vizhi-agent"
