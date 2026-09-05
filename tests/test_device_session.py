"""Automatic re-enrollment when Vizhi rejects the stored device token."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from device_session import DeviceSession


class _FakeResp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


def test_device_session_recovers_on_401(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"ENDPOINT_ID":"old","DEVICE_TOKEN":"vzd_old","ENROLLMENT_CODE":"VZ-ABCD-EFGH-IJKL","API_BASE":"https://vizhi.example.com"}',
        encoding="utf-8",
    )
    session = DeviceSession(
        "https://vizhi.example.com",
        config_path,
        "2.1.1",
        {
            "ENDPOINT_ID": "old",
            "DEVICE_TOKEN": "vzd_old",
            "ENROLLMENT_CODE": "VZ-ABCD-EFGH-IJKL",
        },
    )

    calls = {"n": 0}

    def fake_request(method, url, headers=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            assert headers["Authorization"] == "Bearer vzd_old"
            return _FakeResp(401, '{"error":"Unauthorized device."}')
        assert headers["Authorization"] == "Bearer vzd_new"
        return _FakeResp(200)

    with patch("device_session.requests.request", side_effect=fake_request):
        with patch("device_session.enroll") as mock_enroll:
            mock_enroll.return_value = {
                "ENDPOINT_ID": "00000000-0000-0000-0000-000000000001",
                "DEVICE_TOKEN": "vzd_new",
            }
            resp = session.request("POST", "/api/agent/telemetry", json={"metrics": {}})

    assert resp is not None
    assert resp.status_code == 200
    assert session.device_token == "vzd_new"
    saved = config_path.read_text(encoding="utf-8")
    assert "vzd_new" in saved
    assert "VZ-ABCD-EFGH-IJKL" in saved


def test_device_session_cannot_recover_without_enrollment_code(tmp_path: Path) -> None:
    session = DeviceSession(
        "https://vizhi.example.com",
        tmp_path / "config.json",
        "2.1.1",
        {"ENDPOINT_ID": "old", "DEVICE_TOKEN": "vzd_old"},
    )

    with patch("device_session.requests.request", return_value=_FakeResp(401)):
        resp = session.request("POST", "/api/agent/telemetry", json={"metrics": {}})

    assert resp is not None
    assert resp.status_code == 401
    assert session.device_token == "vzd_old"
