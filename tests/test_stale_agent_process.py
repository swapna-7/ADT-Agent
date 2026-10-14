"""Stale duplicate agent process cleanup after self-update."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WINDOWS = Path(__file__).resolve().parents[1] / "windows"
if str(_WINDOWS) not in sys.path:
    sys.path.insert(0, str(_WINDOWS))


def _agent_proc(pid: int, exe: str, age_seconds: float) -> MagicMock:
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "name": "adt-agent.exe",
        "exe": exe,
        "create_time": time.time() - age_seconds,
    }
    return proc


def test_cleanup_stale_agent_process_terminates_other_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows agent cleanup")
    import agent  # noqa: E402

    self_pid = 1000
    other = _agent_proc(2000, str(agent.INSTALL_EXE_PATH), age_seconds=60)
    self_proc = _agent_proc(self_pid, str(agent.INSTALL_EXE_PATH), age_seconds=60)

    monkeypatch.setattr(agent.os, "getpid", lambda: self_pid)
    monkeypatch.setattr(
        agent.psutil,
        "process_iter",
        lambda *args, **kwargs: [self_proc, other],
    )

    agent.cleanup_stale_agent_processes()
    other.kill.assert_called_once()
    self_proc.kill.assert_not_called()


def test_cleanup_skips_young_agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows agent cleanup")
    import agent  # noqa: E402

    self_pid = 1000
    young = _agent_proc(2000, str(agent.INSTALL_EXE_PATH), age_seconds=5)
    old = _agent_proc(3000, str(agent.INSTALL_EXE_PATH), age_seconds=90)

    monkeypatch.setattr(agent.os, "getpid", lambda: self_pid)
    monkeypatch.setattr(
        agent.psutil,
        "process_iter",
        lambda *args, **kwargs: [young, old],
    )

    agent.cleanup_stale_agent_processes()
    young.kill.assert_not_called()
    old.kill.assert_called_once()
