#!/usr/bin/env bash
# Thin wrapper: ./build.sh from the repo root builds the Windows agent.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/windows/build.sh" "$@"
