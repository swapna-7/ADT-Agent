#!/usr/bin/env bash
# Thin wrapper so you can run ./build.sh from Git Bash / MINGW / WSL.
# The real build logic lives in build.ps1 and needs PowerShell to execute.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PS1_PATH="${SCRIPT_DIR}/build.ps1"

if [ ! -f "${PS1_PATH}" ]; then
    echo "build.ps1 not found next to build.sh (looked in ${SCRIPT_DIR})" >&2
    exit 1
fi

# Prefer Windows PowerShell (powershell.exe); fall back to PowerShell Core (pwsh).
if command -v powershell >/dev/null 2>&1; then
    PS_BIN="powershell"
elif command -v powershell.exe >/dev/null 2>&1; then
    PS_BIN="powershell.exe"
elif command -v pwsh >/dev/null 2>&1; then
    PS_BIN="pwsh"
elif command -v pwsh.exe >/dev/null 2>&1; then
    PS_BIN="pwsh.exe"
else
    echo "Neither powershell.exe nor pwsh was found in PATH." >&2
    echo "Install PowerShell or run the build from a PowerShell prompt:" >&2
    echo "    powershell -NoProfile -ExecutionPolicy Bypass -File ./build.ps1" >&2
    exit 1
fi

# Git Bash passes POSIX-style paths; convert to a Windows path so PowerShell is happy.
if command -v cygpath >/dev/null 2>&1; then
    WIN_PS1_PATH="$(cygpath -w "${PS1_PATH}")"
else
    WIN_PS1_PATH="${PS1_PATH}"
fi

exec "${PS_BIN}" -NoProfile -ExecutionPolicy Bypass -File "${WIN_PS1_PATH}" "$@"
