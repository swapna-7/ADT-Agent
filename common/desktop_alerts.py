"""Native desktop notifications polled from the portal (poll-based, no websockets)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from report import fetch_desktop_alerts, report_alert_result
from version import AGENT_VERSION

if TYPE_CHECKING:
    from device_session import DeviceSession

log = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"^https://[\w\-\.]+\.[a-z]{2,}(/.*)?$", re.I)

# Registered WinRT AppUserModelID for Windows PowerShell — required for Show() to work.
# A fake name like "Vizhi ADT" causes Access Denied or a silent no-op.
POWERSHELL_TOAST_AUMID = (
    r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
)

_NOTIF_BRANDING: dict[str, Any] = {}


def set_notif_branding_cache(branding: dict[str, Any] | None) -> None:
    global _NOTIF_BRANDING
    if not branding:
        return
    _NOTIF_BRANDING = {
        "notif_sender_name": branding.get("notif_sender_name"),
        "notif_logo_url": branding.get("notif_logo_url"),
    }


def get_notif_branding_cache() -> dict[str, Any]:
    return dict(_NOTIF_BRANDING)


def open_action_url(url: str) -> None:
    if not url or not URL_PATTERN.match(url):
        log.warning("Rejected action_url")
        return
    webbrowser.open(url)


def poll_and_show_alerts(
    api_base: str,
    device_token: str,
    *,
    session: DeviceSession | None = None,
    data_dir: Path | None = None,
) -> None:
    try:
        alerts = fetch_desktop_alerts(api_base, device_token, session=session)
    except Exception as exc:
        log.warning("Alert poll failed: %s", exc)
        return
    if not alerts:
        return
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        show_alert(alert, api_base, device_token, session=session, data_dir=data_dir)


def _classify_notify_error(exc: BaseException) -> str:
    text = str(exc)
    lower = text.lower()
    if "no_interactive_session" in lower:
        return "no_interactive_session"
    if "access denied" in lower or "e_accessdenied" in lower or "0x80070005" in lower:
        return f"toast_access_denied: {text[:450]}"
    if "toast_not_visible" in lower or "no_visible_toast" in lower:
        return text[:500]
    return text[:500]


def show_alert(
    alert: dict[str, Any],
    api_base: str,
    device_token: str,
    *,
    session: DeviceSession | None = None,
    data_dir: Path | None = None,
) -> None:
    delivery_id = str(alert.get("delivery_id") or "")
    if not delivery_id:
        return
    try:
        _deliver_notification(alert, data_dir=data_dir)
        action_url = str(alert.get("action_url") or "")
        if action_url:
            open_action_url(action_url)
        ok = report_alert_result(
            api_base,
            device_token,
            delivery_id,
            "delivered",
            os=sys.platform,
            agent_version=AGENT_VERSION,
            session=session,
        )
        if not ok:
            log.error("Failed to report delivered for delivery %s", delivery_id)
    except Exception as exc:
        ok = report_alert_result(
            api_base,
            device_token,
            delivery_id,
            "failed",
            error_message=_classify_notify_error(exc),
            os=sys.platform,
            agent_version=AGENT_VERSION,
            session=session,
        )
        if not ok:
            log.error("Failed to report failed for delivery %s: %s", delivery_id, exc)


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _deliver_notification(
    alert: dict[str, Any],
    *,
    data_dir: Path | None = None,
) -> None:
    severity = str(alert.get("severity") or "info")
    title = str(alert.get("title") or "")
    message = str(alert.get("message") or "")
    if sys.platform == "win32":
        _notify_windows(title, message, severity, data_dir=data_dir)
    elif sys.platform == "darwin":
        _notify_macos(title, message, severity)
    else:
        _notify_linux(title, message, severity)


def _windows_active_username() -> str | None:
    try:
        result = subprocess.run(
            ["query", "user"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if ">" in line or "Active" in line:
                parts = line.split()
                if parts:
                    return parts[0].lstrip(">").strip()
    except Exception:
        pass
    return None


def _time_in_one_minute() -> str:
    return (datetime.now() + timedelta(minutes=1)).strftime("%H:%M")


def _notify_windows_eventlog(title: str, message: str, severity: str) -> None:
    del severity
    text = f"{title}: {message}"[:800]
    subprocess.run(
        [
            "eventcreate",
            "/T",
            "INFORMATION",
            "/ID",
            "900",
            "/L",
            "APPLICATION",
            "/SO",
            "VIZHIAlert",
            "/D",
            text,
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )


def _notify_windows_via_ipc(
    title: str,
    message: str,
    data_dir: Path,
) -> bool:
    try:
        from display_ipc import (
            build_toast_xml,
            wait_for_display_result,
            write_display_task,
        )
    except ImportError:
        return False

    # Always queue — helper polls every ~10s when ADTAgentHelper is running.
    task_id = write_display_task(
        data_dir,
        {"type": "toast", "xml": build_toast_xml(title, message)},
    )
    result = wait_for_display_result(data_dir, task_id, timeout=25)
    if result is None:
        log.info("IPC toast timed out for task %s (is ADTAgentHelper running?)", task_id)
        return False
    if result.get("status") == "ok":
        return True
    log.warning("IPC toast error: %s", result.get("error"))
    return False


def _toast_result_path() -> str:
    public_dir = os.environ.get("PUBLIC", r"C:\Users\Public")
    return os.path.join(public_dir, "vizhi_toast_result.txt")


def _notify_windows_schtasks_toast(
    username: str,
    title: str,
    message: str,
) -> bool:
    """Run a verified toast in the interactive user session via schtasks."""
    safe_title = _escape_xml(title[:80])
    safe_message = _escape_xml(message[:200])
    public_dir = os.environ.get("PUBLIC", r"C:\Users\Public")
    tmp = os.path.join(public_dir, "vizhi_toast.ps1")
    result_file = _toast_result_path()
    aumid = POWERSHELL_TOAST_AUMID.replace("'", "''")

    try:
        if os.path.exists(result_file):
            os.remove(result_file)
    except OSError:
        pass

    # Write ok/fail so the agent can tell silent WinRT failures from real delivery.
    ps_content = f"""$ErrorActionPreference = 'Stop'
$out = '{result_file.replace(chr(39), chr(39)+chr(39))}'
try {{
  [void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime]
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml('<toast duration="long"><visual><binding template="ToastGeneric"><text>{safe_title}</text><text>{safe_message}</text></binding></visual></toast>')
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}')
  $notifier.Show($toast)
  Set-Content -Path $out -Value 'ok' -Encoding UTF8
}} catch {{
  Set-Content -Path $out -Value ("fail:" + $_.Exception.Message) -Encoding UTF8
  exit 1
}}
"""
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(ps_content)

    task_name = "VIZHIToastOnce"
    create = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            task_name,
            "/TR",
            f'powershell -WindowStyle Hidden -STA -NonInteractive -ExecutionPolicy Bypass -File "{tmp}"',
            "/SC",
            "ONCE",
            "/ST",
            _time_in_one_minute(),
            "/RU",
            username,
            "/RL",
            "LIMITED",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if create.returncode != 0:
        log.warning(
            "schtasks create failed: %s",
            (create.stderr or create.stdout or "")[:300],
        )
        return False

    run = subprocess.run(
        ["schtasks", "/Run", "/TN", task_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if run.returncode != 0:
        log.warning("schtasks run failed: %s", (run.stderr or run.stdout or "")[:300])
        return False

    # Wait for the user-session script to confirm Show() succeeded.
    deadline = time.time() + 20
    status = ""
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            if os.path.exists(result_file):
                with open(result_file, encoding="utf-8") as handle:
                    status = handle.read().strip()
                break
        except OSError:
            continue

    subprocess.Popen(
        [
            "cmd",
            "/C",
            f'timeout /T 5 && schtasks /Delete /F /TN {task_name} '
            f'&& del /F "{tmp}" & del /F "{result_file}"',
        ],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
        close_fds=True,
    )

    if status == "ok":
        return True
    if status.startswith("fail:"):
        log.warning("User-session toast failed: %s", status[5:][:300])
        return False
    log.warning("Toast result missing after schtasks run (status=%r)", status)
    return False


def _notify_windows(
    title: str,
    message: str,
    severity: str,
    *,
    data_dir: Path | None = None,
) -> None:
    """Deliver a visible toast in the logged-in user's session.

    Must not report success for Event Log-only fallbacks — those never show a toast.
    """
    try:
        from win_session import has_interactive_session
    except ImportError:
        has_interactive_session = None  # type: ignore[assignment]

    if has_interactive_session is not None and not has_interactive_session():
        raise RuntimeError("no_interactive_session")

    brand = get_notif_branding_cache()
    sender = str(brand.get("notif_sender_name") or "").strip()
    display_title = (
        f"{sender}: {title}" if sender and sender.lower() not in title.lower() else title
    )
    display_message = message
    title_safe = display_title.replace('"', "'").replace("\n", " ")[:80]
    message_safe = display_message.replace('"', "'").replace("\n", " ")[:200]

    if data_dir is not None:
        try:
            if _notify_windows_via_ipc(display_title, display_message, data_dir):
                return
        except Exception as exc:
            log.debug("IPC toast failed, falling back: %s", exc)

    username = _windows_active_username()
    if username and _notify_windows_schtasks_toast(username, display_title, display_message):
        return

    # msg.exe is a last-resort visible dialog (often disabled on modern Windows).
    if username:
        try:
            msg = subprocess.run(
                ["msg", username, f"{title_safe}: {message_safe}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if msg.returncode == 0:
                return
        except Exception:
            pass

    # Audit only — never count as delivered.
    try:
        _notify_windows_eventlog(display_title, display_message, severity)
    except Exception:
        pass

    raise RuntimeError(
        "no_visible_toast: install/start ADTAgentHelper at user logon, "
        "or allow PowerShell toast notifications (Focus Assist / notification settings)"
    )


def _notify_macos(title: str, message: str, severity: str) -> None:
    del severity
    script = (
        f'display notification "{_escape_applescript(message)}" '
        f'with title "Vizhi: {_escape_applescript(title)}" '
        f'sound name "Glass"'
    )
    try:
        who = subprocess.run(
            ["stat", "-f", "%Su", "/dev/console"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        console_user = (who.stdout or "").strip()
    except Exception:
        console_user = ""

    if not console_user or console_user.lower() == "root":
        raise RuntimeError("no_console_user")

    result = subprocess.run(
        ["sudo", "-u", console_user, "/usr/bin/osascript", "-e", script],
        timeout=10,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "osascript failed").strip()
        raise RuntimeError(f"macOS notification failed: {err[:300]}")


def _get_uid(username: str) -> str:
    import pwd

    return str(pwd.getpwnam(username).pw_uid)


def _linux_active_user() -> str | None:
    try:
        who = subprocess.run(["who"], capture_output=True, text=True, timeout=5)
        users = [
            line.split()[0]
            for line in who.stdout.splitlines()
            if "(:0)" in line or "(: " in line or "(:" in line or "(tty" in line
        ]
        if users:
            return users[0]
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] not in {"", "-"}:
                return parts[2]
    except Exception:
        pass
    return None


def _notify_linux(title: str, message: str, severity: str) -> None:
    urgency_map = {"info": "normal", "warning": "normal", "critical": "critical"}
    urgency = urgency_map.get(severity, "normal")
    target_user = _linux_active_user()

    if target_user:
        try:
            uid = _get_uid(target_user)
        except Exception:
            uid = None
        cmd = [
            "sudo",
            "-u",
            target_user,
            "env",
            "DISPLAY=:0",
        ]
        if uid:
            cmd.append(f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus")
        cmd.extend(
            [
                "notify-send",
                "--urgency",
                urgency,
                "--app-name",
                "Vizhi ADT",
                "--expire-time",
                "10000",
                title,
                message,
            ]
        )
        result = subprocess.run(cmd, timeout=10, capture_output=True, text=True)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "notify-send failed").strip()
            raise RuntimeError(f"Linux notify-send failed: {err[:300]}")
        return

    subprocess.run(
        ["logger", "-t", "vizhi-alert", f"{title}: {message}"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    raise RuntimeError("no_active_linux_session")
