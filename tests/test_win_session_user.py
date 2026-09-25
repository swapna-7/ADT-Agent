"""Windows interactive user detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COMMON = Path(__file__).resolve().parents[1] / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from win_session import get_active_interactive_user  # noqa: E402


def test_get_active_interactive_user_none_without_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("win_session._active_session_id", lambda: None)
    assert get_active_interactive_user() is None


def test_get_active_interactive_user_returns_cim_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("win_session._active_session_id", lambda: 1)

    class Proc:
        returncode = 0
        stdout = "DESKTOP-ABC\\swapna\n"
        stderr = ""

    monkeypatch.setattr("win_session.subprocess.run", lambda *a, **k: Proc())
    assert get_active_interactive_user() == "DESKTOP-ABC\\swapna"
