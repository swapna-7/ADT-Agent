"""Alert result telemetry + distinct session vs access-denied errors."""

from __future__ import annotations

from unittest.mock import patch

import desktop_alerts


def test_alert_result_includes_os_and_version():
    captured: list[dict] = []

    def fake_report(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch("desktop_alerts.fetch_desktop_alerts") as fetch,
        patch("desktop_alerts.report_alert_result", side_effect=fake_report),
        patch("desktop_alerts._notify_windows"),
        patch("desktop_alerts.sys.platform", "win32"),
        patch("desktop_alerts.AGENT_VERSION", "2.1.3"),
    ):
        fetch.return_value = [
            {"delivery_id": "d1", "title": "T", "message": "M", "severity": "info"}
        ]
        desktop_alerts.poll_and_show_alerts("https://example.test", "tok")

    assert captured
    assert captured[0]["kwargs"].get("os") == "win32"
    assert captured[0]["kwargs"].get("agent_version") == "2.1.3"


def test_no_interactive_session_error_reason():
    assert desktop_alerts._classify_notify_error(RuntimeError("no_interactive_session")).startswith(
        "no_interactive_session"
    )


def test_access_denied_error_reason():
    msg = desktop_alerts._classify_notify_error(
        RuntimeError("Windows toast failed: Access is denied. (Exception from HRESULT: 0x80070005)")
    )
    assert msg.startswith("toast_access_denied")
