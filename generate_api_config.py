#!/usr/bin/env python3
"""Run before PyInstaller. Writes api_config.py with the baked-in API_BASE.

Usage: python generate_api_config.py https://your-domain.vercel.app
"""

from __future__ import annotations

import pathlib
import sys

if len(sys.argv) != 2:
    print("Usage: generate_api_config.py <api_base_url>")
    sys.exit(1)

url = sys.argv[1].rstrip("/")
out = pathlib.Path(__file__).parent / "api_config.py"
out.write_text(f'API_BASE = "{url}"\n', encoding="utf-8")
print(f"Written: {out}")
