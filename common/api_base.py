"""Resolve Vizhi API base URL at runtime."""

from __future__ import annotations

import os
from typing import Any

try:
    from api_config import API_BASE as _BAKED_API_BASE
except ImportError:
    _BAKED_API_BASE = None


def resolve_api_base(
    local_config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    override: str | None = None,
) -> str:
    cfg = local_config or {}
    env_map = env or {}
    base = "http://localhost:3000"
    for candidate in (
        override,
        cfg.get("API_BASE"),
        env_map.get("API_BASE"),
        os.getenv("API_BASE"),
        _BAKED_API_BASE,
        "http://localhost:3000",
    ):
        if candidate and str(candidate).strip():
            base = str(candidate).strip().rstrip("/")
            break
    if not base.startswith("https://") and "localhost" not in base:
        raise RuntimeError(f"API_BASE must use HTTPS in production: {base}")
    return base
