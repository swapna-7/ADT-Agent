"""Atomic read/write for config.json on disk."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return dict(data)
    return {}


def write_config(config: dict[str, Any], config_path: Path) -> None:
    """Write config atomically: tmp file then os.replace()."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, config_path)


def update_config(
    config_path: Path,
    patch_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    config = load_config(config_path)
    updated = patch_fn(config)
    write_config(updated, config_path)
    return updated
