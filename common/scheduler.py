"""Task scheduling with persisted last_run timestamps in config.json."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from config_io import load_config, write_config

DEFAULT_INTERVALS: dict[str, int] = {
    "metrics": 900,
    "inventory": 21600,
    "update_scan": 21600,
    "patch_poll": 30,
    "command_poll": 20,
    "auto_update": 21600,
    "vuln_eval": 21600,
}

ENV_OVERRIDES: dict[str, str] = {
    "metrics": "INTERVAL_SECONDS",
    "inventory": "SOFTWARE_REFRESH_SECONDS",
    "update_scan": "UPDATE_SCAN_SECONDS",
    "patch_poll": "PATCH_POLL_SECONDS",
    "command_poll": "COMMAND_POLL_SECONDS",
    "auto_update": "AUTO_UPDATE_SECONDS",
}


def resolve_intervals(
    config: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, int]:
    cfg = config or {}
    env_map = env or {}
    out = dict(DEFAULT_INTERVALS)
    for task, env_key in ENV_OVERRIDES.items():
        for source in (cfg.get(env_key), env_map.get(env_key), os.getenv(env_key)):
            if source is not None and str(source).strip():
                try:
                    out[task] = max(1, int(str(source).strip()))
                    break
                except ValueError:
                    continue
    return out


def load_last_runs(config: dict[str, Any]) -> dict[str, float]:
    raw = config.get("last_runs")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def should_run(
    task: str,
    last_runs: dict[str, float],
    interval: int,
    now: float | None = None,
) -> bool:
    ts = now if now is not None else time.time()
    last = last_runs.get(task)
    if last is None:
        return True
    return ts >= last + interval


def mark_run(
    config_path: Path,
    config: dict[str, Any],
    task: str,
    now: float | None = None,
) -> dict[str, Any]:
    ts = now if now is not None else time.time()
    last_runs = load_last_runs(config)
    last_runs[task] = ts
    config = dict(config)
    config["last_runs"] = last_runs
    write_config(config, config_path)
    return config
