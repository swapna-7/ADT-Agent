"""ADTAgentHelper scheduled-task registration (SYSTEM agent → user AtLogOn)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests

_WINDOWS = Path(__file__).resolve().parent
_COMMON = _WINDOWS.parent / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from enrollment import device_headers  # noqa: E402
from hidden_ps import CREATE_NO_WINDOW, run_hidden_powershell, run_hidden_process  # noqa: E402
from version import AGENT_VERSION  # noqa: E402
from win_session import get_active_interactive_user  # noqa: E402

log = logging.getLogger(__name__)

INSTALL_DIR = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ADT Agent"
DATA_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ADT Agent"
# User-session helper must live under ProgramData — Program Files is SYSTEM/admin-only.
HELPER_INSTALL_PATH = DATA_DIR / "user_helper.ps1"
HELPER_LAUNCHER_NAME = "user_helper_launch.vbs"
HELPER_LAUNCHER_PATH = DATA_DIR / HELPER_LAUNCHER_NAME
WSCRIPT_EXE = r"C:\Windows\System32\wscript.exe"
HELPER_VERSION_MARKER = DATA_DIR / ".helper_version"
HELPER_TASK_NAME = "ADTAgentHelper"
DEBUG_LOG_PATH = DATA_DIR / "debug-c15c98.log"
WORKSPACE_DEBUG_LOG = Path(r"c:\Users\swapn\OneDrive\Desktop\Rex Projects\Vizhi\debug-c15c98.log")
HELPER_LOG_PATH = DATA_DIR / "user_helper.log"
HELPER_LOG_FALLBACK = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "adt-agent-user_helper.log"
HELPER_LOG_START_MARKER = "ADTAgentHelper started"
SESSION_ID = "c15c98"
HELPER_VERIFY_TIMEOUT_S = 15
HELPER_VERIFY_POLL_S = 2.0


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def build_helper_launcher_vbs(helper_path: Path | str) -> str:
    """VBScript that starts the helper with WshShell.Run style 0 (no window)."""
    quoted = str(helper_path).replace("'", "''")
    return (
        "Option Explicit\r\n"
        "Dim sh, cmd\r\n"
        "cmd = \"powershell.exe -WindowStyle Hidden -NoProfile -STA -NonInteractive "
        f"-ExecutionPolicy Bypass -Command \"\"& '{quoted}'\"\"\"\r\n"
        "Set sh = CreateObject(\"Wscript.Shell\")\r\n"
        "sh.Run cmd, 0, True\r\n"
    )


def write_helper_launcher(helper_path: Path | str) -> Path:
    launcher = Path(helper_path).with_name(HELPER_LAUNCHER_NAME)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(build_helper_launcher_vbs(helper_path), encoding="ascii")
    return launcher


def build_helper_task_arguments(helper_path: Path | str) -> str:
    """Task Scheduler args for wscript.exe — GUI host, no console flash."""
    launcher = Path(helper_path).with_name(HELPER_LAUNCHER_NAME)
    return f'//B //Nologo "{launcher}"'


def _resolve_user_helper_source() -> Path | None:
    if _is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        bundled = meipass / "user_helper.ps1"
        if bundled.exists():
            return bundled
    local = _WINDOWS / "user_helper.ps1"
    if local.exists():
        return local
    return None


def _debug_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict | None = None,
    *,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    payload = {
        "sessionId": SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, default=str)
    for path in (DEBUG_LOG_PATH, WORKSPACE_DEBUG_LOG):
        try:
            if path.parent.is_dir() or path == DEBUG_LOG_PATH:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass
    log.info("[debug-c15c98] %s %s", message, data or {})
    # #endregion


def ensure_helper_script_present() -> Path | None:
    """Install bundled user_helper.ps1; version marker tracks agent release."""
    src = _resolve_user_helper_source()
    if src is None:
        _debug_log(
            "E",
            "user_helper_task.py:ensure_helper_script_present",
            "helper script source missing",
            {"agent_version": AGENT_VERSION},
        )
        log.warning("user_helper.ps1 not found in agent bundle")
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    launcher = HELPER_INSTALL_PATH.with_name(HELPER_LAUNCHER_NAME)
    marker_ok = (
        HELPER_INSTALL_PATH.is_file()
        and HELPER_VERSION_MARKER.is_file()
        and HELPER_VERSION_MARKER.read_text(encoding="utf-8").strip() == AGENT_VERSION
        and launcher.is_file()
    )
    if not marker_ok:
        shutil.copy2(src, HELPER_INSTALL_PATH)
        write_helper_launcher(HELPER_INSTALL_PATH)
        HELPER_VERSION_MARKER.parent.mkdir(parents=True, exist_ok=True)
        HELPER_VERSION_MARKER.write_text(AGENT_VERSION, encoding="utf-8")
        log.info("Installed user helper %s to %s", AGENT_VERSION, HELPER_INSTALL_PATH)
        _debug_log(
            "E",
            "user_helper_task.py:ensure_helper_script_present",
            "helper script installed",
            {"version": AGENT_VERSION, "path": str(HELPER_INSTALL_PATH)},
            run_id="post-fix",
        )
        restart_helper_after_script_update()
    else:
        write_helper_launcher(HELPER_INSTALL_PATH)
    return HELPER_INSTALL_PATH


def restart_helper_after_script_update() -> None:
    """Reload user_helper.ps1 — the running process keeps the old script in memory."""
    try:
        run_hidden_process(
            ["schtasks", "/End", "/TN", HELPER_TASK_NAME],
            timeout=30,
        )
    except Exception as exc:
        log.warning("Could not end ADTAgentHelper: %s", exc)
    time.sleep(2)
    start_helper_task()


def is_helper_running() -> bool:
    """True when a powershell process is running user_helper.ps1."""
    try:
        import psutil

        target = "user_helper.ps1"
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            joined = " ".join(str(part) for part in cmdline).lower()
            if target in joined or "user_helper_launch.vbs" in joined:
                return True
    except Exception as exc:
        log.debug("is_helper_running probe failed: %s", exc)
    return False


def get_mtime_if_exists(path: Path) -> float | None:
    try:
        return path.stat().st_mtime if path.is_file() else None
    except OSError:
        return None


def _log_contains_start_marker(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return HELPER_LOG_START_MARKER in text


def verify_helper_started(
    *,
    timeout_seconds: int = HELPER_VERIFY_TIMEOUT_S,
    poll_interval: float = HELPER_VERIFY_POLL_S,
) -> bool:
    """True when user_helper.log (or TEMP fallback) gains ADTAgentHelper started."""
    deadline = time.time() + max(1, int(timeout_seconds))
    baseline_primary = get_mtime_if_exists(HELPER_LOG_PATH)
    baseline_fallback = get_mtime_if_exists(HELPER_LOG_FALLBACK)

    while time.time() < deadline:
        time.sleep(max(0.5, poll_interval))
        for path, baseline in (
            (HELPER_LOG_PATH, baseline_primary),
            (HELPER_LOG_FALLBACK, baseline_fallback),
        ):
            mtime = get_mtime_if_exists(path)
            if mtime is None:
                continue
            if baseline is None or mtime > baseline:
                if _log_contains_start_marker(path):
                    _debug_log(
                        "F",
                        "user_helper_task.py:verify_helper_started",
                        "helper log confirmed",
                        {"path": str(path)},
                        run_id="post-fix",
                    )
                    return True
                if path == HELPER_LOG_PATH:
                    baseline_primary = mtime
                else:
                    baseline_fallback = mtime

    _debug_log(
        "F",
        "user_helper_task.py:verify_helper_started",
        "helper log missing within timeout",
        {"timeout_seconds": timeout_seconds},
        run_id="post-fix",
    )
    return False


def report_helper_status(
    api_base: str,
    device_token: str,
    *,
    ok: bool,
    reason: str | None = None,
) -> None:
    base = (api_base or "").strip().rstrip("/")
    if not base or not device_token:
        return
    payload: dict[str, object] = {"ok": ok}
    if reason:
        payload["reason"] = (reason or "")[:200]
    try:
        requests.post(
            f"{base}/api/agent/helper-status",
            headers=device_headers(device_token),
            json=payload,
            timeout=15,
        )
    except Exception as exc:
        log.warning("Could not report display helper status: %s", exc)


def report_helper_registration_failure(
    api_base: str,
    device_token: str,
    reason: str,
) -> None:
    log.warning("Display helper failed verification: %s", reason)
    report_helper_status(api_base, device_token, ok=False, reason=reason)


def _verify_and_report(api_base: str | None, device_token: str | None) -> None:
    if not api_base or not device_token:
        return
    if verify_helper_started():
        report_helper_status(api_base, device_token, ok=True)
    else:
        report_helper_registration_failure(
            api_base,
            device_token,
            "helper_task_did_not_produce_log_within_timeout",
        )


def start_helper_task(*, task_name: str = HELPER_TASK_NAME) -> bool:
    """Ask Task Scheduler to run ADTAgentHelper in the interactive user session."""
    try:
        # #region agent log
        _debug_log(
            "A",
            "user_helper_task.py:start_helper_task",
            "schtasks /Run about to spawn",
            {
                "task": task_name,
                "creationflags": CREATE_NO_WINDOW,
                "has_create_no_window": True,
            },
            run_id="post-fix",
        )
        # #endregion
        proc = run_hidden_process(
            ["schtasks", "/Run", "/TN", task_name],
            timeout=30,
        )
    except Exception as exc:
        _debug_log(
            "B",
            "user_helper_task.py:start_helper_task",
            "schtasks run exception",
            {"error": str(exc)},
        )
        log.warning("Could not start ADTAgentHelper via schtasks: %s", exc)
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "schtasks /Run failed").strip()
        _debug_log(
            "B",
            "user_helper_task.py:start_helper_task",
            "schtasks run failed",
            {"returncode": proc.returncode, "detail": detail[:500]},
        )
        log.warning("schtasks /Run ADTAgentHelper failed: %s", detail)
        return False
    _debug_log(
        "C",
        "user_helper_task.py:start_helper_task",
        "schtasks run accepted",
        {"task": task_name},
    )
    log.info("Started ADTAgentHelper via schtasks")
    return True


def get_registered_task_principal(task_name: str = HELPER_TASK_NAME) -> str | None:
    script = f"(Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue).Principal.UserId"
    try:
        proc = run_hidden_powershell(script, timeout=30)
        principal = (proc.stdout or "").strip()
        if proc.returncode != 0 or not principal:
            return None
        return principal
    except Exception as exc:
        log.warning("Could not read scheduled task principal for %s: %s", task_name, exc)
        return None


def get_registered_task_arguments(task_name: str = HELPER_TASK_NAME) -> str | None:
    script = (
        f"(Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue)"
        ".Actions | ForEach-Object { $_.Arguments }"
    )
    try:
        proc = run_hidden_powershell(script, timeout=30)
        args = (proc.stdout or "").strip()
        if proc.returncode != 0 or not args:
            return None
        return args
    except Exception as exc:
        log.warning("Could not read scheduled task arguments for %s: %s", task_name, exc)
        return None


def helper_task_args_are_hidden(arguments: str | None) -> bool:
    """True when the task uses the windowless wscript launcher."""
    if not arguments:
        return False
    normalized = " ".join(arguments.split()).lower()
    return "user_helper_launch.vbs" in normalized and "//b" in normalized


def register_helper_task(user: str, *, helper_path: Path | None = None) -> bool:
    path = helper_path or ensure_helper_script_present()
    if path is None or not path.is_file():
        log.warning("Skipping ADTAgentHelper registration — helper script missing")
        return False

    write_helper_launcher(path)
    safe_user = user.replace("'", "''")
    ps_arg = build_helper_task_arguments(path).replace("'", "''")
    wscript = WSCRIPT_EXE.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
$helperAction = New-ScheduledTaskAction -Execute '{wscript}' -Argument '{ps_arg}'
$helperTrigger = New-ScheduledTaskTrigger -AtLogOn -User '{safe_user}'
$helperSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId '{safe_user}' -LogonType Interactive
Register-ScheduledTask -TaskName '{HELPER_TASK_NAME}' -Action $helperAction -Trigger $helperTrigger -Settings $helperSettings -Principal $principal -Force | Out-Null
schtasks /Run /TN '{HELPER_TASK_NAME}' | Out-Null
"""
    try:
        proc = run_hidden_powershell(script, timeout=60)
    except Exception as exc:
        _debug_log(
            "B",
            "user_helper_task.py:register_helper_task",
            "register subprocess exception",
            {"user": user, "error": str(exc)},
        )
        log.warning("ADTAgentHelper registration exception: %s", exc)
        return False

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Failed to register user helper task").strip()
        _debug_log(
            "B",
            "user_helper_task.py:register_helper_task",
            "register failed",
            {"user": user, "returncode": proc.returncode, "detail": detail[:500]},
        )
        log.warning("ADTAgentHelper registration failed: %s", detail)
        return False

    _debug_log(
        "B",
        "user_helper_task.py:register_helper_task",
        "register succeeded",
        {"user": user, "execute": "wscript.exe", "args_prefix": ps_arg[:60]},
        run_id="post-fix",
    )
    log.info("Registered ADTAgentHelper for user: %s", user)
    start_helper_task()
    return True


def ensure_helper_task_registered(
    *,
    api_base: str | None = None,
    device_token: str | None = None,
    get_user: Callable[[], str | None] | None = None,
    get_principal: Callable[[], str | None] | None = None,
    get_args: Callable[[], str | None] | None = None,
    register: Callable[[str], bool] | None = None,
) -> None:
    """Re-register ADTAgentHelper when an interactive user is present (metrics cadence)."""
    user_fn = get_user or get_active_interactive_user
    principal_fn = get_principal or get_registered_task_principal
    args_fn = get_args or get_registered_task_arguments
    register_fn = register or (lambda u: register_helper_task(u))

    active_user = user_fn()
    if not active_user:
        _debug_log(
            "A",
            "user_helper_task.py:ensure_helper_task_registered",
            "no active interactive session — skip",
            {},
        )
        log.debug("No active interactive session — skipping helper registration this cycle")
        return

    current_principal = principal_fn()
    current_args = args_fn()
    hidden_ok = helper_task_args_are_hidden(current_args)
    _debug_log(
        "D",
        "user_helper_task.py:ensure_helper_task_registered",
        "principal check",
        {
            "active_user": active_user,
            "current_principal": current_principal,
            "hidden_ok": hidden_ok,
            "args_prefix": (current_args or "")[:40],
        },
    )

    if current_principal and _principal_matches(current_principal, active_user):
        if not hidden_ok:
            _debug_log(
                "A",
                "user_helper_task.py:ensure_helper_task_registered",
                "visible helper args — re-registering hidden",
                {"user": active_user, "args_prefix": (current_args or "")[:80]},
                run_id="post-fix",
            )
            try:
                if register_fn(active_user):
                    _verify_and_report(api_base, device_token)
            except Exception as exc:
                log.warning("ADTAgentHelper hidden re-register error (will retry): %s", exc)
            return
        if is_helper_running():
            _debug_log(
                "D",
                "user_helper_task.py:ensure_helper_task_registered",
                "already registered — helper running",
                {"user": active_user},
            )
            return
        _debug_log(
            "D",
            "user_helper_task.py:ensure_helper_task_registered",
            "registered but helper not running — starting",
            {"user": active_user},
        )
        if start_helper_task():
            _verify_and_report(api_base, device_token)
        return

    log.info("Registering ADTAgentHelper for user: %s", active_user)
    try:
        if register_fn(active_user):
            _verify_and_report(api_base, device_token)
    except Exception as exc:
        _debug_log(
            "B",
            "user_helper_task.py:ensure_helper_task_registered",
            "register raised — will retry next cycle",
            {"user": active_user, "error": str(exc)},
        )
        log.warning("ADTAgentHelper registration error (will retry): %s", exc)


def _principal_matches(registered: str, active_user: str) -> bool:
    reg = registered.strip().lower()
    act = active_user.strip().lower()
    if reg == act:
        return True
    if "\\" in act and reg.endswith(act.split("\\", 1)[1]):
        return True
    if "\\" in reg and act.endswith(reg.split("\\", 1)[1]):
        return True
    return False
