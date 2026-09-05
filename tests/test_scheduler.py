"""Scheduler last_runs persistence tests."""

from __future__ import annotations

import time
from pathlib import Path

from config_io import load_config
from scheduler import load_last_runs, mark_run, should_run


def test_last_runs_persisted(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config: dict = {"DEVICE_TOKEN": "vzd_x"}
    interval = 3600
    now = time.time()

    assert should_run("inventory", {}, interval, now) is True

    config = mark_run(config_path, config, "inventory", now)
    last_runs = load_last_runs(config)
    assert "inventory" in last_runs

    assert should_run("inventory", last_runs, interval, now + 100) is False
    assert should_run("inventory", last_runs, interval, now + interval + 1) is True

    disk = load_config(config_path)
    assert disk["last_runs"]["inventory"] == now
