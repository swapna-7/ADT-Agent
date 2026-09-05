#!/usr/bin/env bash
# Copy built agent binaries into the ADT web tool public folder.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC="$ROOT/../adt/public"

mkdir -p "$PUBLIC"

if [[ -f "$ROOT/windows/dist/adt-agent.exe" ]]; then
  cp "$ROOT/windows/dist/adt-agent.exe" "$PUBLIC/vizhi-agent.exe"
  echo "Published Windows agent -> $PUBLIC/vizhi-agent.exe"
else
  echo "Skip Windows: $ROOT/windows/dist/adt-agent.exe not found (run windows/build.ps1 on Windows)"
fi

if [[ -f "$ROOT/linux/dist/adt-agent" ]]; then
  cp "$ROOT/linux/dist/adt-agent" "$PUBLIC/vizhi-agent-linux"
  chmod +x "$PUBLIC/vizhi-agent-linux" 2>/dev/null || true
  echo "Published Linux agent -> $PUBLIC/vizhi-agent-linux"
else
  echo "Skip Linux: $ROOT/linux/dist/adt-agent not found (run linux/build.sh on Linux)"
fi
