"""Tests for Windows desktop alert delivery helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import desktop_alerts


def test_windows_active_username_parses_query_user():
    with patch("desktop_alerts.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout=(
                " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
                ">swapn                 console             1  Active      none   3/21/2026 8:00 AM\n"
            ),
            returncode=0,
        )
        assert desktop_alerts._windows_active_username() == "swapn"


def test_notify_windows_uses_msg_when_available():
    with patch("win_session.has_interactive_session", return_value=True):
        with patch("desktop_alerts._windows_active_username", return_value="swapn"):
            with patch("desktop_alerts.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                desktop_alerts._notify_windows("Title", "Body", "info")
                assert mock_run.call_args_list[0].args[0][:2] == ["msg", "swapn"]


def test_notify_windows_eventlog_when_no_user():
    with patch("win_session.has_interactive_session", return_value=True):
        with patch("desktop_alerts._windows_active_username", return_value=None):
            with patch("desktop_alerts._notify_windows_eventlog") as mock_log:
                desktop_alerts._notify_windows("Title", "Body", "info", data_dir=None)
                mock_log.assert_called_once()


def test_notify_windows_ipc_fast_path(tmp_path: Path):
    with patch("win_session.has_interactive_session", return_value=True):
        with patch("desktop_alerts._notify_windows_via_ipc", return_value=True) as mock_ipc:
            desktop_alerts._notify_windows(
                "Title",
                "Body",
                "info",
                data_dir=tmp_path,
            )
            mock_ipc.assert_called_once()
