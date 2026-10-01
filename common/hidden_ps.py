"""Run PowerShell without a visible console window.

Windows still flashes a console if powershell.exe is started without CREATE_NO_WINDOW
and -WindowStyle Hidden as the first arguments.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = 0x08000000


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


def run_hidden_powershell(
    script: str,
    *,
    timeout: int,
    extra_args: list[str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    argv = hidden_powershell_argv(*(extra_args or []), "-Command", script)
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        startupinfo=_startupinfo(),
        creationflags=flags,
        **kwargs,
    )
