"""Branding apply always POSTs a terminal result."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import branding


def test_branding_job_always_posts_result_on_failure(tmp_path: Path):
    posts: list[str] = []

    def fake_report(api_base, token, job_id, status, **kwargs):
        posts.append(status)
        return True

    branding.BRANDING_CACHE_DIR = tmp_path / "branding"
    branding.BRANDING_CACHE_DIR.mkdir(parents=True)

    with (
        patch("branding.report_branding_result", side_effect=fake_report),
        patch("branding.apply_wallpaper", side_effect=RuntimeError("apply failed")),
    ):
        branding.apply_branding_job(
            {"job_id": "job-1", "job_type": "wallpaper"},
            {"wallpaper_enabled": True, "wallpaper_url": "https://example.test/w.jpg"},
            "https://example.test",
            "dt_x",
        )

    assert "received" in posts
    assert "failed" in posts
    assert posts[-1] == "failed"


def test_branding_wallpaper_windows_uses_display_ipc(tmp_path: Path):
    branding.BRANDING_CACHE_DIR = tmp_path / "branding"
    branding.BRANDING_CACHE_DIR.mkdir(parents=True)
    img = branding.BRANDING_CACHE_DIR / "abc.jpg"
    img.write_bytes(b"fake-image")

    tasks: list[dict] = []

    def fake_write(_data_dir, task):
        tasks.append(task)
        return "task-1"

    with (
        patch("branding.sys.platform", "win32"),
        patch("branding.download_image", return_value=img),
        patch("win_session.has_interactive_session", return_value=True),
        patch("display_ipc.write_display_task", side_effect=fake_write),
        patch(
            "display_ipc.wait_for_display_result",
            return_value={"id": "task-1", "status": "ok"},
        ),
    ):
        branding.apply_wallpaper(
            {"wallpaper_url": "https://example.test/w.jpg", "wallpaper_fit": "fill"},
            data_dir=tmp_path,
        )

    assert len(tasks) == 1
    assert tasks[0]["type"] == "wallpaper"
    assert tasks[0]["path"] == str(img)
    assert tasks[0]["fit_code"] == "10"


def test_download_image_http_error(tmp_path: Path):
    branding.BRANDING_CACHE_DIR = tmp_path / "branding"
    branding.BRANDING_CACHE_DIR.mkdir(parents=True)

    class FakeHTTPError(Exception):
        code = 404

    import urllib.error

    with (
        patch("branding.is_trusted_url", return_value=True),
        patch(
            "branding.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "https://x/y.jpg", 404, "Not Found", hdrs=None, fp=None  # type: ignore[arg-type]
            ),
        ),
    ):
        try:
            branding.download_image("https://example.test/missing.jpg")
            raised = False
        except ValueError as exc:
            raised = True
            assert "404" in str(exc)
            assert "https://example.test/missing.jpg" in str(exc)
    assert raised
