"""Native desktop notifications polled from the portal (poll-based, no websockets)."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import webbrowser
from typing import TYPE_CHECKING, Any

from report import fetch_desktop_alerts, report_alert_result
from version import AGENT_VERSION

if TYPE_CHECKING:
    from device_session import DeviceSession

log = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"^https://[\w\-\.]+\.[a-z]{2,}(/.*)?$", re.I)

# Lightweight cache filled by branding poll (notif sender / logo for toasts).
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


def _classify_notify_error(exc: BaseException) -> str:
    text = str(exc)
    lower = text.lower()
    if "no_interactive_session" in lower:
        return "no_interactive_session"
    if "access denied" in lower or "e_accessdenied" in lower or "0x80070005" in lower:
        return f"toast_access_denied: {text[:450]}"
    return text[:500]


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
    """Show a user-visible notification on Windows.

    Task Scheduler / non-interactive hosts often get E_ACCESSDENIED from
    CreateToastNotifier with an unregistered AppId. Prefer PowerShell's own
    AppUserModelID, then fall back to a NotifyIcon balloon tip.
    """
    del severity  # reserved for future icon / urgency mapping

    try:
        from win_session import has_interactive_session
    except ImportError:
        has_interactive_session = None  # type: ignore[assignment]

    if has_interactive_session is not None and not has_interactive_session():
        raise RuntimeError("no_interactive_session")

    brand = get_notif_branding_cache()
    sender = str(brand.get("notif_sender_name") or "").strip()
    # Keep AUMID workaround; prefix sender into title/body instead of custom AppId.
    display_title = f"{sender}: {title}" if sender and sender.lower() not in title.lower() else title
    display_message = message

    safe_title = _escape_xml(display_title)
    safe_message = _escape_xml(display_message)
    # Escape for single-quoted PowerShell strings in the balloon fallback.
    ps_title = display_title.replace("'", "''")
    ps_message = display_message.replace("'", "''")

    # Known AUMID for powershell.exe — registered with Windows, avoids Access Denied
    # when the agent runs under a logged-on user via Scheduled Task.
    ps_script = f"""
$ErrorActionPreference = 'Stop'
function Show-VizhiToast {{
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null
  $template = @"
<toast duration="long">
  <visual>
    <binding template="ToastGeneric">
      <text>{safe_title}</text>
      <text>{safe_message}</text>
    </binding>
  </visual>
</toast>
"@
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml($template)
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  $appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
  $notifier.Show($toast)
}}
function Show-VizhiBalloon {{
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $notify = New-Object System.Windows.Forms.NotifyIcon
  $notify.Icon = [System.Drawing.SystemIcons]::Information
  $notify.Visible = $true
  $notify.BalloonTipTitle = '{ps_title}'
  $notify.BalloonTipText = '{ps_message}'
  $notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
  $notify.ShowBalloonTip(12000)
  Start-Sleep -Milliseconds 1500
  $notify.Dispose()
}}
$errs = @()
try {{ Show-VizhiToast; exit 0 }} catch {{ $errs += $_.Exception.Message }}
try {{ Show-VizhiBalloon; exit 0 }} catch {{ $errs += $_.Exception.Message }}
Write-Error ($errs -join ' | ')
exit 1
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
        timeout=20,
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
