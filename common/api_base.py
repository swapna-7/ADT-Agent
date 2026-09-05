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
    for candidate in (
        override,
        cfg.get("API_BASE"),
        env_map.get("API_BASE"),
        os.getenv("API_BASE"),
        _BAKED_API_BASE,
        "http://localhost:3000",
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip().rstrip("/")
    return "http://localhost:3000"
