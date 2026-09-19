"""Branding pending_session + run_as_interactive_user structural tests."""

from __future__ import annotations

from unittest.mock import patch

import branding
import win_session


def test_run_as_interactive_user_is_importable():
    assert callable(win_session.has_interactive_session)
    assert callable(win_session.run_as_interactive_user)


def test_wallpaper_no_session_raises_pending_session():
    with (
        patch("sys.platform", "win32"),
        patch("branding.has_interactive_session", create=True),
        patch("win_session.has_interactive_session", return_value=False),
        patch("branding.download_image") as dl,
    ):
        try:
            branding._apply_wallpaper_windows(
                {"wallpaper_url": "https://vizhi.rcsaware.com/x.jpg", "wallpaper_fit": "fill"}
            )
            raised = False
        except RuntimeError as exc:
            raised = str(exc) == "pending_session"
        assert raised
        dl.assert_not_called()


def test_apply_job_reports_pending_session_not_immediate_fail():
    posts: list[dict] = []

    def fake_report(*args, **kwargs):
        posts.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch("branding.report_branding_result", side_effect=fake_report),
        patch("branding.apply_wallpaper", side_effect=RuntimeError("pending_session")),
        patch("branding.apply_lockscreen"),
        patch("branding.apply_screensaver"),
        patch("sys.platform", "linux"),
    ):
        branding.apply_branding_job(
            {"job_id": "j1", "job_type": "wallpaper", "status": "sent"},
            {"wallpaper_enabled": True, "wallpaper_url": "https://vizhi.rcsaware.com/a.jpg"},
            "https://example.test",
            "tok",
        )

    statuses = [p["args"][3] for p in posts]
    assert "pending_session" in statuses
    assert "failed" not in statuses


def test_wallpaper_fail_lockscreen_ok_independent_columns():
    posts: list[dict] = []

    def fake_report(*args, **kwargs):
        posts.append({"args": args, "kwargs": kwargs})
        return True

    with (
        patch("branding.report_branding_result", side_effect=fake_report),
        patch("branding.apply_wallpaper", side_effect=RuntimeError("spi boom")),
        patch("branding.apply_lockscreen"),
        patch("branding.apply_screensaver"),
        patch("sys.platform", "linux"),
    ):
        branding.apply_branding_job(
            {"job_id": "j2", "job_type": "all", "status": "sent"},
            {
                "wallpaper_enabled": True,
                "lockscreen_enabled": True,
                "screensaver_enabled": False,
            },
            "https://example.test",
            "tok",
        )

    final = [p for p in posts if p["args"][3] == "failed"]
    assert final
    assert final[-1]["kwargs"].get("wallpaper_status") == "failed"
    assert final[-1]["kwargs"].get("lockscreen_status") == "completed"
