"""ADTAgentHelper runtime registration (Windows)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WINDOWS = Path(__file__).resolve().parents[1] / "windows"
import sys

if str(_WINDOWS) not in sys.path:
    sys.path.insert(0, str(_WINDOWS))

import user_helper_task as uht  # noqa: E402


def test_ensure_helper_task_registered_skips_when_no_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = MagicMock()
    monkeypatch.setattr(uht, "get_active_interactive_user", lambda: None)
    uht.ensure_helper_task_registered(get_user=lambda: None, register=register)
    register.assert_not_called()


def test_ensure_helper_task_registered_registers_for_detected_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = MagicMock(return_value=True)
    monkeypatch.setattr(uht, "get_active_interactive_user", lambda: "DESKTOP\\swapna")
    uht.ensure_helper_task_registered(
        get_user=lambda: "DESKTOP\\swapna",
        get_principal=lambda: None,
        register=register,
    )
    register.assert_called_once_with("DESKTOP\\swapna")


def test_ensure_helper_task_registered_reregisters_on_user_change() -> None:
    register = MagicMock(return_value=True)
    uht.ensure_helper_task_registered(
        get_user=lambda: "DESKTOP\\newuser",
        get_principal=lambda: "DESKTOP\\olduser",
        register=register,
    )
    register.assert_called_once_with("DESKTOP\\newuser")


def test_ensure_helper_task_registered_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = MagicMock(return_value=True)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(uht, "is_helper_running", lambda: True)
    monkeypatch.setattr(uht, "start_helper_task", start)
    uht.ensure_helper_task_registered(
        get_user=lambda: "DESKTOP\\swapna",
        get_principal=lambda: "DESKTOP\\swapna",
        register=register,
    )
    register.assert_not_called()
    start.assert_not_called()


def test_ensure_helper_task_registered_starts_when_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = MagicMock(return_value=True)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(uht, "is_helper_running", lambda: False)
    monkeypatch.setattr(uht, "start_helper_task", start)
    uht.ensure_helper_task_registered(
        get_user=lambda: "DESKTOP\\swapna",
        get_principal=lambda: "DESKTOP\\swapna",
        register=register,
    )
    register.assert_not_called()
    start.assert_called_once()


def test_register_helper_task_failure_does_not_crash_main_loop() -> None:
    register = MagicMock(side_effect=RuntimeError("0x80070534"))
    try:
        uht.ensure_helper_task_registered(
            get_user=lambda: "DESKTOP\\swapna",
            get_principal=lambda: None,
            register=register,
        )
    except Exception as exc:
        pytest.fail(f"ensure_helper_task_registered must not raise: {exc}")
    register.assert_called_once()


def test_helper_script_version_matches_agent_version_after_self_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "bundle" / "user_helper.ps1"
    src.parent.mkdir()
    src.write_text("# helper v2", encoding="utf-8")
    install_dir = tmp_path / "install"
    data_dir = tmp_path / "data"
    install_dir.mkdir()
    data_dir.mkdir()

    monkeypatch.setattr(uht, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(uht, "HELPER_INSTALL_PATH", install_dir / "user_helper.ps1")
    monkeypatch.setattr(uht, "DATA_DIR", data_dir)
    monkeypatch.setattr(uht, "HELPER_VERSION_MARKER", data_dir / ".helper_version")
    monkeypatch.setattr(uht, "_resolve_user_helper_source", lambda: src)
    monkeypatch.setattr(uht, "AGENT_VERSION", "2.1.7")

    path = uht.ensure_helper_script_present()
    assert path is not None
    assert path.read_text(encoding="utf-8") == "# helper v2"
    assert uht.HELPER_VERSION_MARKER.read_text(encoding="utf-8") == "2.1.7"

    src.write_text("# helper v2.1.8", encoding="utf-8")
    monkeypatch.setattr(uht, "AGENT_VERSION", "2.1.8")
    uht.ensure_helper_script_present()
    assert path.read_text(encoding="utf-8") == "# helper v2.1.8"
    assert uht.HELPER_VERSION_MARKER.read_text(encoding="utf-8") == "2.1.8"


def test_register_helper_task_uses_schtasks_and_user_logon_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "user_helper.ps1"
    helper.write_text("# helper", encoding="utf-8")
    captured: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        captured["script"] = cmd[-1]
        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setattr(uht.subprocess, "run", fake_run)
    monkeypatch.setattr(uht, "start_helper_task", lambda **k: True)

    assert uht.register_helper_task("DESKTOP\\swapna", helper_path=helper)
    script = captured["script"]
    assert "-AtLogOn -User 'DESKTOP\\swapna'" in script
    assert "schtasks /Run /TN 'ADTAgentHelper'" in script
    assert "MultipleInstances IgnoreNew" in script
    assert "Start-ScheduledTask" not in script


def test_debug_log_writes_ndjson(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "debug-5a7da5.log"
    monkeypatch.setattr(uht, "DEBUG_LOG_PATH", log_path)
    uht._debug_log("A", "test", "hello", {"k": 1}, run_id="test-run")
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["hypothesisId"] == "A"
    assert payload["sessionId"] == "5a7da5"
