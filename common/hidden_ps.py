"""Run PowerShell without a visible console window.

Windows still flashes a console if powershell.exe is started without CREATE_NO_WINDOW
and -WindowStyle Hidden as the first arguments.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000
_DEBUG_LOGS = (
    Path(r"C:\ProgramData\ADT Agent\debug-c15c98.log"),
    Path(r"c:\Users\swapn\OneDrive\Desktop\Rex Projects\Vizhi\debug-c15c98.log"),
)


def _startupinfo() -> subprocess.STARTUPINFO | None:
    if sys.platform != "win32":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def hidden_powershell_argv(*tail: str) -> list[str]:
    """powershell.exe argv with hide flags first so the console never maps."""
    return [
        "powershell",
        "-WindowStyle",
        "Hidden",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        *tail,
    ]


def _debug_hidden_ps(message: str, data: dict[str, Any]) -> None:
    # #region agent log
    payload = {
        "sessionId": "c15c98",
        "runId": "pre-fix",
        "hypothesisId": "D",
        "location": "hidden_ps.py:run_hidden_powershell",
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, default=str)
    for path in _DEBUG_LOGS:
        try:
            if path.parent.is_dir():
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass
    # #endregion


def run_hidden_process(
    argv: list[str],
    *,
    timeout: int,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a console exe (schtasks, query, …) without mapping a window."""
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        startupinfo=_startupinfo(),
        **kwargs,
        creationflags=flags,
    )


def run_hidden_powershell(
    script: str,
    *,
    timeout: int,
    extra_args: list[str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    argv = hidden_powershell_argv(*(extra_args or []), "-Command", script)
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    _debug_hidden_ps(
        "hidden powershell spawn",
        {
            "creationflags": flags,
            "timeout": timeout,
            "script_prefix": (script or "").replace("\n", " ")[:80],
        },
    )
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        startupinfo=_startupinfo(),
        **kwargs,
        creationflags=flags,
    )
