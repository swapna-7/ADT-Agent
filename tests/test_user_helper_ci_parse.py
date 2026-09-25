"""CI parse validation for user_helper.ps1 (Windows PowerShell 5.1 only)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "windows" / "scripts" / "validate-user-helper.ps1"
GOOD = ROOT / "windows" / "user_helper.ps1"
BAD = ROOT / "windows" / "scripts" / "fixtures" / "user_helper_bad_winrt.ps1"


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell 5.1")
def test_ci_parse_step_accepts_current_user_helper() -> None:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VALIDATE),
            "-ScriptPath",
            str(GOOD),
            "-ParseOnly",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell 5.1")
def test_ci_parse_step_rejects_known_bad_syntax() -> None:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VALIDATE),
            "-ScriptPath",
            str(BAD),
            "-ParseOnly",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0, "bad WinRT fixture should fail parse"
