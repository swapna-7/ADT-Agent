"""--enroll-only enrolls then exits without starting the main loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WINDOWS = Path(__file__).resolve().parents[1] / "windows"
if str(_WINDOWS) not in sys.path:
    sys.path.insert(0, str(_WINDOWS))


def test_enroll_only_exits_without_starting_pollers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows agent enroll-only")

    import agent  # noqa: E402

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ENROLLMENT_CODE": "VZ-ABCD-EFGH-IJKL",
                "DEVICE_NAME": "TEST-PC",
                "API_BASE": "https://vizhi.example.com",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["adt-agent", "--enroll-only"])

    enrolled = {
        "ENDPOINT_ID": "00000000-0000-0000-0000-000000000001",
        "DEVICE_TOKEN": "vzd_test",
    }
    with (
        patch("agent.prompt_and_enroll", return_value=enrolled) as enroll,
        patch("agent.start_command_poller") as poller,
        patch("agent.start_patch_poller") as patch_poller,
        patch("agent.setup_logging"),
    ):
        with pytest.raises(SystemExit) as exc:
            agent.main()

    assert exc.value.code == 0
    enroll.assert_called_once()
    poller.assert_not_called()
    patch_poller.assert_not_called()


def test_enroll_only_already_enrolled_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows agent enroll-only")

    import agent  # noqa: E402

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "DEVICE_TOKEN": "vzd_existing",
                "ENDPOINT_ID": "00000000-0000-0000-0000-000000000001",
                "API_BASE": "https://vizhi.example.com",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["adt-agent", "--enroll-only"])

    with (
        patch("agent.prompt_and_enroll") as enroll,
        patch("agent.start_command_poller") as poller,
        patch("agent.setup_logging"),
    ):
        with pytest.raises(SystemExit) as exc:
            agent.main()

    assert exc.value.code == 0
    enroll.assert_not_called()
    poller.assert_not_called()
