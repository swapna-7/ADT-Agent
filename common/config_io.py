"""Atomic read/write for config.json on disk."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
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


def load_local_config(path: Path) -> dict[str, Any]:
    """Load config with scalar values as strings; preserve nested dicts/lists (e.g. last_runs)."""
    data = load_config(path)
    out: dict[str, Any] = {}
    for key, value in data.items():
        k = str(key)
        if isinstance(value, (dict, list)):
            out[k] = value
        elif value is None:
            out[k] = ""
        else:
            out[k] = str(value)
    return out


def write_config(config: dict[str, Any], config_path: Path) -> None:
    """Write config atomically: tmp file then os.replace()."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, config_path)
    protect_config_path(config_path)


def protect_config_path(config_path: Path) -> None:
    """Restrict config.json so unprivileged users cannot read DEVICE_TOKEN."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        if sys.platform == "win32":
            grants = [
                "icacls",
                str(config_path),
                "/inheritance:r",
                "/grant:r",
                "SYSTEM:F",
                "/grant:r",
                "Administrators:R",
            ]
            user = os.environ.get("USERNAME")
            if user:
                grants.extend(["/grant:r", f"{user}:F"])
            subprocess.run(grants, capture_output=True, check=False)
            return
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(config_path, 0, 0)
    except Exception:
        pass


def update_config(
    config_path: Path,
    patch_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    config = load_config(config_path)
    updated = patch_fn(config)
    write_config(updated, config_path)
    return updated
