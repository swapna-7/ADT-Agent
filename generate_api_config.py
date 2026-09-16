#!/usr/bin/env python3
"""Run before PyInstaller. Writes api_config.py and ed25519_verify_key.py.

Usage: python generate_api_config.py https://your-domain.vercel.app
"""

from __future__ import annotations

import os
import pathlib
import sys

if len(sys.argv) != 2:
    print("Usage: generate_api_config.py <api_base_url>")
    sys.exit(1)

url = sys.argv[1].rstrip("/")
root = pathlib.Path(__file__).parent
(root / "api_config.py").write_text(f'API_BASE = "{url}"\n', encoding="utf-8")
print(f"Written: {root / 'api_config.py'}")

pub = os.environ.get("AGENT_VERIFY_PUBLIC_KEY", "").strip()
verify_path = root / "ed25519_verify_key.py"
contents = f'AGENT_VERIFY_PUBLIC_KEY = "{pub}"\n'
verify_path.write_text(contents, encoding="utf-8")
(root / "common" / "ed25519_verify_key.py").write_text(contents, encoding="utf-8")
print(f"Written: {verify_path}")
