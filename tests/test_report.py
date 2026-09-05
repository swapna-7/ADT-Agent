"""Telemetry helper posts to Vizhi, not to Supabase."""

from __future__ import annotations

from report import report_telemetry


class _FakeResp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status
        self.text = ""


def test_report_telemetry_posts_vizhi_path(monkeypatch) -> None:
    seen = {}

    def fake_request(method, url, json=None, headers=None, timeout=None, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return _FakeResp(200)

    monkeypatch.setattr("report.requests.request", fake_request)
    ok = report_telemetry(
        "https://vizhi.example.com",
        "vzd_token",
        metrics={"captured_at": "2026-01-01T00:00:00Z"},
    )
    assert ok is True
    assert seen["method"] == "POST"
    assert seen["url"] == "https://vizhi.example.com/api/agent/telemetry"
    assert seen["json"]["metrics"]["captured_at"] == "2026-01-01T00:00:00Z"
    assert seen["headers"]["Authorization"] == "Bearer vzd_token"


def test_report_telemetry_skips_empty() -> None:
    assert report_telemetry("https://vizhi.example.com", "vzd_token") is False
