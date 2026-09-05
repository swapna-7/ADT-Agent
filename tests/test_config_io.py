"""Atomic config.json write tests."""

from __future__ import annotations

import json
from pathlib import Path

from config_io import load_config, write_config


def test_write_config_atomic(tmp_path: Path):
    config_path = tmp_path / "config.json"
    write_config({"DEVICE_TOKEN": "vzd_one"}, config_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["DEVICE_TOKEN"] == "vzd_one"
    assert not config_path.with_suffix(".tmp").exists()

    write_config({"DEVICE_TOKEN": "vzd_two", "last_runs": {"metrics": 100}}, config_path)
    data = load_config(config_path)
    assert data["DEVICE_TOKEN"] == "vzd_two"
    assert data["last_runs"]["metrics"] == 100

    # Simulate interrupted write: stale tmp must not corrupt the live file.
    tmp_path_file = config_path.with_suffix(".tmp")
    tmp_path_file.write_text("{broken", encoding="utf-8")
    assert load_config(config_path)["DEVICE_TOKEN"] == "vzd_two"
