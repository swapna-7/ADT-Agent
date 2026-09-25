"""Stale duplicate agent process cleanup after self-update."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WINDOWS = Path(__file__).resolve().parents[1] / "windows"
if str(_WINDOWS) not in sys.path:
    sys.path.insert(0, str(_WINDOWS))

import agent  # noqa: E402


def test_cleanup_stale_agent_process_terminates_other_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    self_pid = 1000
    other = MagicMock()
    other.info = {"pid": 2000, "name": "adt-agent.exe", "exe": str(agent.INSTALL_EXE_PATH)}
    self_proc = MagicMock()
    self_proc.info = {"pid": self_pid, "name": "adt-agent.exe", "exe": str(agent.INSTALL_EXE_PATH)}

    monkeypatch.setattr(agent.os, "getpid", lambda: self_pid)
    monkeypatch.setattr(
        agent.psutil,
        "process_iter",
        lambda *args, **kwargs: [self_proc, other],
    )

    agent.cleanup_stale_agent_processes()
    other.kill.assert_called_once()
    self_proc.kill.assert_not_called()
