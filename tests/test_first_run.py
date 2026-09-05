"""Mode B enrollment from baked-in ENROLLMENT_CODE."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from first_run import enroll_with_code, get_or_prompt_enrollment_code, needs_enrollment, prompt_and_enroll


def test_needs_enrollment_empty_token():
    assert needs_enrollment({}) is True
    assert needs_enrollment({"DEVICE_TOKEN": ""}) is True
    assert needs_enrollment({"DEVICE_TOKEN": "vzd_abc"}) is False


def test_enrollment_code_from_config_no_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = get_or_prompt_enrollment_code({"ENROLLMENT_CODE": "VZ-ABCD-EFGH-IJKL"})
    assert code == "VZ-ABCD-EFGH-IJKL"


def test_enrollment_code_from_config_exits_without_tty_or_code(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        get_or_prompt_enrollment_code({})
    assert exc.value.code == 2


def test_enroll_preserves_enrollment_code_in_written_config(tmp_path: Path):
    config_path = tmp_path / "config.json"
    with patch("first_run.enroll") as mock_enroll:
        mock_enroll.return_value = {
            "ENDPOINT_ID": "00000000-0000-0000-0000-000000000001",
            "DEVICE_TOKEN": "vzd_test",
            "ROLE": "",
        }
        with patch("first_run.machine_guid", return_value="guid"):
            result = enroll_with_code(
                "https://vizhi.example.com",
                config_path,
                "2.0.0",
                "VZ-ABCD-EFGH-IJKL",
                existing_config={"ENROLLMENT_CODE": "VZ-ABCD-EFGH-IJKL", "AGENT_AUTO_UPDATE": "1"},
            )
    assert result["ENROLLMENT_CODE"] == "VZ-ABCD-EFGH-IJKL"
    assert result["DEVICE_TOKEN"] == "vzd_test"


def test_prompt_and_enroll_uses_baked_in_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with patch("first_run.enroll_with_code") as mock:
        mock.return_value = {"DEVICE_TOKEN": "vzd_x"}
        prompt_and_enroll(
            "https://vizhi.example.com",
            config_path,
            "2.0.0",
            local_config={"ENROLLMENT_CODE": "VZ-ABCD-EFGH-IJKL"},
        )
        mock.assert_called_once()
