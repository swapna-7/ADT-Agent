"""Windows interactive user detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COMMON = Path(__file__).resolve().parents[1] / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from win_session import WTSDomainName, WTSUserName, get_active_interactive_user  # noqa: E402


def test_get_active_interactive_user_none_without_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("win_session._active_session_id", lambda: None)
    assert get_active_interactive_user() is None


def test_get_active_interactive_user_prefers_wts_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("win_session.sys.platform", "win32")
    monkeypatch.setattr("win_session._active_session_id", lambda: 1)

    def fake_wts(_session_id: int, info_class: int) -> str | None:
        if info_class == WTSUserName:
            return "swapna"
        if info_class == WTSDomainName:
            return "ASUS"
        return None

    monkeypatch.setattr("win_session._wts_session_string", fake_wts)
    assert get_active_interactive_user() == "ASUS\\swapna"


def test_get_active_interactive_user_falls_back_to_cim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("win_session.sys.platform", "win32")
    monkeypatch.setattr("win_session._active_session_id", lambda: 1)
    monkeypatch.setattr("win_session._wts_session_string", lambda *_args: None)

    class Proc:
        returncode = 0
        stdout = "DESKTOP-ABC\\swapna\n"
        stderr = ""

    monkeypatch.setattr("win_session.subprocess.run", lambda *a, **k: Proc())
    assert get_active_interactive_user() == "DESKTOP-ABC\\swapna"
