"""Hidden PowerShell launcher argv contract."""

from __future__ import annotations

from pathlib import Path
import sys

_COMMON = Path(__file__).resolve().parents[1] / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from hidden_ps import hidden_powershell_argv  # noqa: E402


def test_hidden_powershell_argv_hides_first() -> None:
    argv = hidden_powershell_argv("-Command", "Get-Date")
    assert argv[:5] == ["powershell", "-WindowStyle", "Hidden", "-NoProfile", "-NonInteractive"]
    assert argv[-2:] == ["-Command", "Get-Date"]
