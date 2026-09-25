"""Tests for display IPC helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from display_ipc import (
    build_toast_xml,
    helper_recently_active,
    pending_path,
    result_path,
    wait_for_display_result,
    write_display_task,
)


def test_build_toast_xml_escapes_special_chars():
    xml = build_toast_xml('Test & "Title"', "Line <one>")
    assert "&amp;" in xml
    assert "&lt;" in xml
    assert "&quot;" in xml


def test_write_display_task_appends_and_returns_id(tmp_path: Path):
    task_id = write_display_task(
        tmp_path,
        {"type": "toast", "xml": "<toast/>"},
    )
    assert task_id
    pending = json.loads(pending_path(tmp_path).read_text(encoding="utf-8"))
    assert len(pending) == 1
    assert pending[0]["id"] == task_id
    assert pending[0]["type"] == "toast"


def test_write_display_task_atomic_append(tmp_path: Path):
    write_display_task(tmp_path, {"type": "wallpaper", "path": "C:\\a.jpg"})
    write_display_task(tmp_path, {"type": "screensaver", "timeout_s": 600})
    pending = json.loads(pending_path(tmp_path).read_text(encoding="utf-8"))
    assert len(pending) == 2


def test_wait_for_display_result_finds_task(tmp_path: Path):
    task_id = write_display_task(tmp_path, {"type": "toast", "xml": "<toast/>"})
    result_path(tmp_path).write_text(
        json.dumps([{"id": task_id, "status": "ok", "error": None}]),
        encoding="utf-8",
    )
    result = wait_for_display_result(tmp_path, task_id, timeout=2)
    assert result is not None
    assert result["status"] == "ok"


def test_wait_for_display_result_timeout(tmp_path: Path):
    result = wait_for_display_result(tmp_path, "missing-id", timeout=1)
    assert result is None


def test_helper_recently_active(tmp_path: Path):
    assert helper_recently_active(tmp_path) is False
    result_path(tmp_path).write_text("[]", encoding="utf-8")
    assert helper_recently_active(tmp_path) is True
    old = time.time() - 300
    path = result_path(tmp_path)
    path.touch()
    import os

    os.utime(path, (old, old))
    assert helper_recently_active(tmp_path, within_seconds=60) is False

    log_path = tmp_path / "user_helper.log"
    log_path.write_text("started\n", encoding="utf-8")
    assert helper_recently_active(tmp_path, within_seconds=60) is True
