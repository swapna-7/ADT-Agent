"""Run PowerShell and console tools without a visible window on Windows."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


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


def run_hidden_process(
    argv: list[str],
    *,
    timeout: int,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a console exe (schtasks, query, choco, …) without mapping a window."""
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


run_hidden = run_hidden_process


def popen_hidden(argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    """Start a background process without a visible console window."""
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW | DETACHED_PROCESS)
        kwargs.setdefault("startupinfo", _startupinfo())
        kwargs.setdefault("close_fds", True)
    return subprocess.Popen(argv, **kwargs)


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
        **kwargs,
        creationflags=flags,
    )
