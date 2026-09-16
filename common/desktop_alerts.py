"""Native desktop notifications polled from the portal (poll-based, no websockets)."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import webbrowser
from typing import TYPE_CHECKING, Any

from report import fetch_desktop_alerts, report_alert_result

if TYPE_CHECKING:
    from device_session import DeviceSession

log = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"^https://[\w\-\.]+\.[a-z]{2,}(/.*)?$", re.I)


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
        show_alert(alert, api_base, device_token, session=session)


def show_alert(
    alert: dict[str, Any],
    api_base: str,
    device_token: str,
    *,
    session: DeviceSession | None = None,
) -> None:
    delivery_id = str(alert.get("delivery_id") or "")
    if not delivery_id:
        return
    try:
        _deliver_notification(alert)
        action_url = str(alert.get("action_url") or "")
        if action_url:
            open_action_url(action_url)
        ok = report_alert_result(
            api_base, device_token, delivery_id, "delivered", session=session
        )
        if not ok:
            log.error("Failed to report delivered for delivery %s", delivery_id)
    except Exception as exc:
        ok = report_alert_result(
            api_base,
            device_token,
            delivery_id,
            "failed",
            error_message=str(exc)[:500],
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


def _deliver_notification(alert: dict[str, Any]) -> None:
    severity = str(alert.get("severity") or "info")
    title = str(alert.get("title") or "")
    message = str(alert.get("message") or "")
    if sys.platform == "win32":
        _notify_windows(title, message, severity)
    elif sys.platform == "darwin":
        _notify_macos(title, message, severity)
    else:
        _notify_linux(title, message, severity)


def _notify_windows(title: str, message: str, severity: str) -> None:
    del severity  # toast template is generic; severity reserved for future icons
    ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null
$template = @"
<toast duration="long">
  <visual>
    <binding template="ToastGeneric">
      <text>{_escape_xml(title)}</text>
      <text>{_escape_xml(message)}</text>
    </binding>
  </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Vizhi ADT")
$notifier.Show($toast)
"""
    result = subprocess.run(
        [
            "powershell",
            "-NonInteractive",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            ps_script,
        ],
        timeout=15,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "toast failed").strip()
        raise RuntimeError(f"Windows toast failed: {err[:300]}")


def _notify_macos(title: str, message: str, severity: str) -> None:
    del severity
    script = (
        f'display notification "{_escape_applescript(message)}" '
        f'with title "Vizhi: {_escape_applescript(title)}" '
        f'sound name "Glass"'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
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


def _notify_linux(title: str, message: str, severity: str) -> None:
    urgency_map = {"info": "normal", "warning": "normal", "critical": "critical"}
    urgency = urgency_map.get(severity, "normal")
    target_user = None
    try:
        who = subprocess.run(["who"], capture_output=True, text=True, timeout=5)
        users = [
            line.split()[0]
            for line in who.stdout.splitlines()
            if "(: " in line or "(:" in line or "(tty" in line
        ]
        target_user = users[0] if users else None
    except Exception:
        target_user = None

    if target_user:
        try:
            uid = _get_uid(target_user)
        except Exception:
            uid = None
        env_prefix = []
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
