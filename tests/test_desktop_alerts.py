"""Desktop alert delivery — native toast path always reports a result."""

from __future__ import annotations

from unittest.mock import patch

import desktop_alerts


def test_alert_delivery_windows_reports_delivered(tmp_path):
    posts: list[tuple] = []

    def fake_report(api_base, token, delivery_id, status, **kwargs):
        posts.append((delivery_id, status, kwargs.get("error_message")))
        return True

    with (
        patch("desktop_alerts.fetch_desktop_alerts") as fetch,
        patch("desktop_alerts.report_alert_result", side_effect=fake_report),
        patch("desktop_alerts._notify_windows") as notify,
        patch("desktop_alerts.sys.platform", "win32"),
    ):
        fetch.return_value = [
            {
                "delivery_id": "del-1",
                "title": "Hello",
                "message": "World",
                "severity": "info",
            }
        ]
        notify.return_value = None
        desktop_alerts.poll_and_show_alerts("https://example.test", "dt_x")

    assert posts == [("del-1", "delivered", None)]
    notify.assert_called_once()


def test_alert_delivery_failure_reports_failed():
    posts: list[tuple] = []

    def fake_report(api_base, token, delivery_id, status, **kwargs):
        posts.append((delivery_id, status, kwargs.get("error_message")))
        return True

    with (
        patch("desktop_alerts.fetch_desktop_alerts") as fetch,
        patch("desktop_alerts.report_alert_result", side_effect=fake_report),
        patch("desktop_alerts._notify_windows", side_effect=RuntimeError("toast boom")),
        patch("desktop_alerts.sys.platform", "win32"),
    ):
        fetch.return_value = [
            {"delivery_id": "del-2", "title": "T", "message": "M", "severity": "critical"}
        ]
        desktop_alerts.poll_and_show_alerts("https://example.test", "dt_x")

    assert len(posts) == 1
    assert posts[0][0] == "del-2"
    assert posts[0][1] == "failed"
    assert "toast boom" in (posts[0][2] or "")


def test_alert_poll_empty_when_api_returns_none():
    with patch("desktop_alerts.fetch_desktop_alerts", return_value=[]):
        # Must not raise
        desktop_alerts.poll_and_show_alerts("https://example.test", "dt_x")
