"""Windows interactive session helpers (WTS → CreateProcessAsUser).

Used by branding wallpaper (SYSTEM → user HKCU) and desktop alerts
(no_interactive_session detection).
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from ctypes import wintypes
from typing import Sequence

log = logging.getLogger(__name__)

WTS_CURRENT_SERVER_HANDLE = 0
WTSActive = 0
WTSConnected = 1
WTSDisconnected = 4
WTSUserName = 5
WTSDomainName = 7

TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ALL_ACCESS = 0xF01FF

SecurityImpersonation = 2
TokenPrimary = 1

CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
NORMAL_PRIORITY_CLASS = 0x00000020


class _WTS_SESSION_INFO(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("pWinStationName", wintypes.LPWSTR),
        ("State", wintypes.DWORD),
    ]


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def has_interactive_session() -> bool:
    """Return True if a Windows console session is Active (or Connected)."""
    if sys.platform != "win32":
        return True
    try:
        return _active_session_id() is not None
    except Exception as exc:
        log.warning("has_interactive_session failed: %s", exc)
        return False


def _wts_session_string(session_id: int, info_class: int) -> str | None:
    """Read a WTSQuerySessionInformationW string for *session_id*."""
    wts = ctypes.WinDLL("wtsapi32")
    buffer = ctypes.POINTER(ctypes.c_wchar)()
    bytes_returned = wintypes.DWORD()
    if not wts.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE,
        session_id,
        info_class,
        ctypes.byref(buffer),
        ctypes.byref(bytes_returned),
    ):
        return None
    try:
        value = ctypes.wstring_at(buffer).strip()
        return value or None
    finally:
        wts.WTSFreeMemory(buffer)


def _format_domain_user(domain: str | None, username: str | None) -> str | None:
    user = (username or "").strip()
    if not user:
        return None
    if "\\" in user:
        return user
    dom = (domain or "").strip()
    if dom:
        return f"{dom}\\{user}"
    return user


def get_active_interactive_user() -> str | None:
    """Return DOMAIN\\user for the active console session, or None if none."""
    if sys.platform != "win32":
        return None
    try:
        session_id = _active_session_id()
    except Exception as exc:
        log.warning("get_active_interactive_user session probe failed: %s", exc)
        return None
    if session_id is None:
        return None

    try:
        username = _wts_session_string(session_id, WTSUserName)
        domain = _wts_session_string(session_id, WTSDomainName)
        wts_user = _format_domain_user(domain, username)
        if wts_user:
            return wts_user
    except Exception as exc:
        log.warning("get_active_interactive_user WTS lookup failed: %s", exc)

    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).UserName",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        user = (proc.stdout or "").strip()
        if proc.returncode != 0 or not user:
            return None
        return user
    except Exception as exc:
        log.warning("get_active_interactive_user failed: %s", exc)
        return None


def _active_session_id() -> int | None:
    wts = ctypes.WinDLL("wtsapi32")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    p_info = ctypes.POINTER(_WTS_SESSION_INFO)()
    count = wintypes.DWORD()
    if not wts.WTSEnumerateSessionsW(
        WTS_CURRENT_SERVER_HANDLE,
        0,
        1,
        ctypes.byref(p_info),
        ctypes.byref(count),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        preferred: int | None = None
        fallback: int | None = None
        for i in range(count.value):
            info = p_info[i]
            if info.SessionId == 0:
                continue
            if info.State == WTSActive:
                preferred = int(info.SessionId)
                break
            if info.State in (WTSConnected, WTSDisconnected) and fallback is None:
                fallback = int(info.SessionId)
        return preferred if preferred is not None else fallback
    finally:
        wts.WTSFreeMemory(p_info)


def run_as_interactive_user(
    command: Sequence[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run *command* in the interactive user's session (CreateProcessAsUser).

    Falls back to a normal subprocess when not on Windows or when no session
    token can be obtained (caller should check has_interactive_session first).
    """
    if sys.platform != "win32":
        return subprocess.run(
            list(command),
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )

    session_id = _active_session_id()
    if session_id is None:
        raise RuntimeError("no_interactive_session")

    wts = ctypes.WinDLL("wtsapi32")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user_token = wintypes.HANDLE()
    if not wts.WTSQueryUserToken(session_id, ctypes.byref(user_token)):
        raise ctypes.WinError(ctypes.get_last_error())

    primary = wintypes.HANDLE()
    try:
        if not advapi.DuplicateTokenEx(
            user_token,
            TOKEN_ALL_ACCESS,
            None,
            SecurityImpersonation,
            TokenPrimary,
            ctypes.byref(primary),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        env = wintypes.LPVOID()
        if not userenv.CreateEnvironmentBlock(ctypes.byref(env), primary, False):
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            # Build a single command line for CreateProcessAsUserW.
            cmdline = subprocess.list2cmdline(list(command))
            cmdline_buf = ctypes.create_unicode_buffer(cmdline)

            si = _STARTUPINFO()
            si.cb = ctypes.sizeof(_STARTUPINFO)
            si.lpDesktop = "winsta0\\default"
            pi = _PROCESS_INFORMATION()

            creation_flags = CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW | NORMAL_PRIORITY_CLASS
            if not advapi.CreateProcessAsUserW(
                primary,
                None,
                cmdline_buf,
                None,
                None,
                False,
                creation_flags,
                env,
                None,
                ctypes.byref(si),
                ctypes.byref(pi),
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            try:
                wait_ms = max(1, int(timeout * 1000))
                wait_result = kernel32.WaitForSingleObject(pi.hProcess, wait_ms)
                if wait_result != 0:  # WAIT_OBJECT_0
                    kernel32.TerminateProcess(pi.hProcess, 1)
                    raise TimeoutError(f"run_as_interactive_user timed out after {timeout}s")

                exit_code = wintypes.DWORD()
                kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=int(exit_code.value),
                    stdout="",
                    stderr="",
                )
            finally:
                kernel32.CloseHandle(pi.hThread)
                kernel32.CloseHandle(pi.hProcess)
        finally:
            userenv.DestroyEnvironmentBlock(env)
            kernel32.CloseHandle(primary)
    finally:
        kernel32.CloseHandle(user_token)
