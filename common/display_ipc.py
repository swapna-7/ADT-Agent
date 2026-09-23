"""IPC between SYSTEM agent and user-session display helper (Windows).

The SYSTEM agent writes pending tasks to pending_display.json; user_helper.ps1
executes them in the logged-in user's session and writes display_results.json.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

PENDING_FILE = "pending_display.json"
RESULT_FILE = "display_results.json"


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_toast_xml(title: str, message: str) -> str:
    safe_title = _escape_xml(title[:80])
    safe_message = _escape_xml(message[:200])
    return (
        "<toast duration=\"long\">"
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{safe_title}</text>"
        f"<text>{safe_message}</text>"
        "</binding></visual>"
        "</toast>"
    )


def pending_path(data_dir: Path) -> Path:
    return data_dir / PENDING_FILE


def result_path(data_dir: Path) -> Path:
    return data_dir / RESULT_FILE


def write_display_task(data_dir: Path, task: dict[str, Any]) -> str:
    """Append a display task and return its id."""
    data_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(task.get("id") or f"task-{int(time.time())}-{uuid.uuid4().hex[:8]}")
    payload = dict(task)
    payload["id"] = task_id

    pending = pending_path(data_dir)
    existing: list[dict[str, Any]] = []
    if pending.exists():
        try:
            raw = json.loads(pending.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = [item for item in raw if isinstance(item, dict)]
        except Exception:
            existing = []

    existing.append(payload)
    tmp = pending.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(pending)
    return task_id


def wait_for_display_result(
    data_dir: Path,
    task_id: str,
    *,
    timeout: int = 30,
) -> dict[str, Any] | None:
    """Poll display_results.json until task_id appears or timeout."""
    results_file = result_path(data_dir)
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        time.sleep(1)
        if not results_file.exists():
            continue
        try:
            raw = json.loads(results_file.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else [raw]
            for item in items:
                if isinstance(item, dict) and item.get("id") == task_id:
                    return item
        except Exception:
            continue
    return None


def helper_recently_active(data_dir: Path, *, within_seconds: int = 60) -> bool:
    """True if display_results.json was modified recently (helper likely running)."""
    results_file = result_path(data_dir)
    if not results_file.exists():
        return False
    try:
        age = time.time() - results_file.stat().st_mtime
        return age <= within_seconds
    except OSError:
        return False
