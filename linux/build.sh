#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
LINUX_DIR="$(pwd)"
REPO_ROOT="$(cd .. && pwd)"

if [[ -f "$LINUX_DIR/.env" ]]; then
  echo "ERROR: .env present in linux/ — credentials must not be bundled" >&2
  exit 1
fi

if [[ -z "${VIZHI_API_BASE:-}" ]]; then
  echo "ERROR: VIZHI_API_BASE not set" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "python3 not found. Install Python 3.10+ and retry."
  exit 1
fi

python3 "$REPO_ROOT/generate_api_config.py" "$VIZHI_API_BASE"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

pip install -U pip
pip install -r requirements.txt

PYI_ARGS=(
  --onefile
  --name adt-agent
  --clean
  --noconfirm
  --paths ../common
  --hidden-import api_config
  --hidden-import packaging
  --hidden-import packaging.version
  --hidden-import psutil
  --hidden-import config_io
  --hidden-import api_base
  --hidden-import first_run
  --hidden-import scheduler
  --hidden-import command_poller
  --hidden-import enrollment
  --hidden-import normalize
  --hidden-import patch_runner
  --hidden-import report
  --hidden-import self_update
  --hidden-import software_quality
  --hidden-import updates
  --hidden-import version
  --add-data "../api_config.py:."
)

pyinstaller "${PYI_ARGS[@]}" agent.py

echo ""
echo "==> Build complete: $LINUX_DIR/dist/adt-agent"
echo "    Install: sudo ./linux/install.sh"
