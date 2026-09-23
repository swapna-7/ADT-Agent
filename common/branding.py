"""Wallpaper / lockscreen / screensaver branding applied from portal jobs."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from report import fetch_branding, report_branding_result

try:
    from desktop_alerts import set_notif_branding_cache
except ImportError:  # pragma: no cover
    def set_notif_branding_cache(_branding: dict[str, Any] | None) -> None:
        return

if TYPE_CHECKING:
    from device_session import DeviceSession

log = logging.getLogger(__name__)

BRANDING_CACHE_DIR: Path | None = None
SESSION_WAIT_TIMEOUT_S = 4 * 60 * 60  # 4 hours

ALLOWED_URL_PREFIXES = [
    "https://vizhi.rcsaware.com/",
]


def is_trusted_url(url: str) -> bool:
    text = (url or "").strip()
    if not text.startswith("https://"):
        return False
    prefixes = list(ALLOWED_URL_PREFIXES)
    supabase = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if supabase:
        prefixes.append(supabase + "/storage/v1/object/public/")
    return any(text.startswith(p) for p in prefixes if p)


def poll_branding(
    api_base: str,
    device_token: str,
    data_dir: Path,
    *,
    session: DeviceSession | None = None,
    force: bool = False,
) -> None:
    """Poll branding jobs. If force=False and no pending jobs, returns quickly."""
    global BRANDING_CACHE_DIR
    BRANDING_CACHE_DIR = data_dir / "branding"
    BRANDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        resp = fetch_branding(api_base, device_token, session=session)
    except Exception as exc:
        log.warning("Branding poll failed: %s", exc)
        return
    if not resp:
        return

    branding = resp.get("branding") if isinstance(resp.get("branding"), dict) else {}
    jobs = resp.get("jobs") if isinstance(resp.get("jobs"), list) else []
    if branding:
        set_notif_branding_cache(branding)
    if not jobs and not force:
        return
    for job in jobs:
        if isinstance(job, dict):
            apply_branding_job(
                job,
                branding or {},
                api_base,
                device_token,
                session=session,
                data_dir=data_dir,
            )


def _surface_skipped(reason: str = "skipped") -> str:
    return reason


def apply_branding_job(
    job: dict[str, Any],
    branding: dict[str, Any],
    api_base: str,
    device_token: str,
    *,
    session: DeviceSession | None = None,
    data_dir: Path | None = None,
) -> None:
    job_id = str(job.get("job_id") or "")
    job_type = str(job.get("job_type") or "all")
    prior_status = str(job.get("status") or "")
    if not job_id:
        return

    # Timeout waiting for an interactive user session.
    if prior_status == "pending_session" and sys.platform == "win32":
        wait_started = job.get("session_wait_started_at") or job.get("created_at")
        if wait_started:
            try:
                from datetime import datetime, timezone

                started = datetime.fromisoformat(str(wait_started).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - started).total_seconds()
                if age > SESSION_WAIT_TIMEOUT_S:
                    report_branding_result(
                        api_base,
                        device_token,
                        job_id,
                        "failed",
                        error_message="no_active_user_session_within_timeout",
                        wallpaper_status="failed",
                        session=session,
                    )
                    return
            except Exception:
                pass

        try:
            from win_session import has_interactive_session
        except ImportError:
            has_interactive_session = lambda: True  # type: ignore[assignment]
        if not has_interactive_session():
            # Still waiting — leave as pending_session without re-reporting received.
            return

    if prior_status != "pending_session":
        reported_received = report_branding_result(
            api_base, device_token, job_id, "received", session=session
        )
        if not reported_received:
            log.error("Could not POST received for branding job %s", job_id)

    wallpaper_status: str | None = None
    lockscreen_status: str | None = None
    screensaver_status: str | None = None
    hard_fail = False
    pending_session = False
    error_parts: list[str] = []

    try:
        if job_type in ("wallpaper", "all"):
            if branding.get("wallpaper_enabled"):
                try:
                    apply_wallpaper(branding, data_dir=data_dir)
                    wallpaper_status = "completed"
                except RuntimeError as exc:
                    if str(exc) == "pending_session":
                        wallpaper_status = "pending_session"
                        pending_session = True
                    else:
                        wallpaper_status = "failed"
                        hard_fail = True
                        error_parts.append(f"wallpaper:{exc}")
                except Exception as exc:
                    wallpaper_status = "failed"
                    hard_fail = True
                    error_parts.append(f"wallpaper:{exc}")
            else:
                wallpaper_status = _surface_skipped("disabled")

        if job_type in ("lockscreen", "all"):
            if branding.get("lockscreen_enabled"):
                try:
                    apply_lockscreen(branding)
                    lockscreen_status = "completed"
                except Exception as exc:
                    lockscreen_status = "failed"
                    hard_fail = True
                    error_parts.append(f"lockscreen:{exc}")
            else:
                lockscreen_status = _surface_skipped("disabled")

        if job_type in ("screensaver", "all"):
            if branding.get("screensaver_enabled"):
                try:
                    apply_screensaver(branding, data_dir=data_dir)
                    screensaver_status = "completed"
                except Exception as exc:
                    screensaver_status = "failed"
                    hard_fail = True
                    error_parts.append(f"screensaver:{exc}")
            else:
                screensaver_status = _surface_skipped("disabled")

        if pending_session and not hard_fail:
            report_branding_result(
                api_base,
                device_token,
                job_id,
                "pending_session",
                error_message="waiting_for_interactive_session",
                wallpaper_status=wallpaper_status,
                lockscreen_status=lockscreen_status,
                screensaver_status=screensaver_status,
                session=session,
            )
            return

        if hard_fail:
            ok = report_branding_result(
                api_base,
                device_token,
                job_id,
                "failed",
                error_message="; ".join(error_parts)[:500],
                wallpaper_status=wallpaper_status,
                lockscreen_status=lockscreen_status,
                screensaver_status=screensaver_status,
                session=session,
            )
        else:
            ok = report_branding_result(
                api_base,
                device_token,
                job_id,
                "completed",
                wallpaper_status=wallpaper_status,
                lockscreen_status=lockscreen_status,
                screensaver_status=screensaver_status,
                session=session,
            )
        if not ok:
            log.error("Could not POST final status for branding job %s", job_id)
    except Exception as exc:
        ok = report_branding_result(
            api_base,
            device_token,
            job_id,
            "failed",
            error_message=str(exc)[:500],
            wallpaper_status=wallpaper_status,
            lockscreen_status=lockscreen_status,
            screensaver_status=screensaver_status,
            session=session,
        )
        if not ok:
            log.error("Could not POST failed for branding job %s: %s", job_id, exc)


def download_image(url: str, ext: str = ".jpg") -> Path:
    if not BRANDING_CACHE_DIR:
        raise ValueError("BRANDING_CACHE_DIR is not set")
    if not is_trusted_url(url):
        raise ValueError(f"Untrusted image URL rejected: {str(url)[:80]}")

    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    dest = BRANDING_CACHE_DIR / f"{url_hash}{ext}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    tmp = dest.with_suffix(".tmp")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise ValueError(f"download_image failed for {url}: HTTP {status}")
            data = resp.read()
        if not data:
            raise ValueError(f"download_image empty body for {url}")
        tmp.write_bytes(data)
        tmp.replace(dest)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"download_image failed for {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"download_image network error for {url}: {exc}") from exc
    finally:
        if tmp.exists() and not dest.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return dest


def _get_uid(username: str) -> str:
    import pwd

    return str(pwd.getpwnam(username).pw_uid)


def _ps_run(script: str, timeout: int = 30) -> None:
    result = subprocess.run(
        [
            "powershell",
            "-NonInteractive",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            script,
        ],
        timeout=timeout,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "powershell failed").strip()
        raise RuntimeError(err[:400])


def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir
    if BRANDING_CACHE_DIR is not None:
        return BRANDING_CACHE_DIR.parent
    raise ValueError("data_dir is required for Windows display IPC")


def _apply_wallpaper_windows(branding: dict[str, Any], *, data_dir: Path | None) -> None:
    url = branding.get("wallpaper_url")
    if not url:
        raise ValueError("wallpaper_url is empty")

    try:
        from win_session import has_interactive_session
    except ImportError:
        has_interactive_session = lambda: True  # type: ignore[assignment]

    if not has_interactive_session():
        raise RuntimeError("pending_session")

    from display_ipc import wait_for_display_result, write_display_task

    fit = str(branding.get("wallpaper_fit") or "fill")
    img_path = download_image(str(url), ".jpg")
    fit_map = {
        "fill": (10, 0),
        "fit": (6, 0),
        "stretch": (2, 0),
        "tile": (0, 1),
        "center": (0, 0),
    }
    style, tile = fit_map.get(fit, (10, 0))
    resolved_dir = _resolve_data_dir(data_dir)
    task_id = write_display_task(
        resolved_dir,
        {
            "type": "wallpaper",
            "path": str(img_path),
            "fit_code": str(style),
            "tile_code": str(tile),
        },
    )
    result = wait_for_display_result(resolved_dir, task_id, timeout=30)
    if result is None:
        raise RuntimeError("pending_session")
    if result.get("status") == "error":
        raise RuntimeError(str(result.get("error") or "wallpaper failed"))


def _apply_lockscreen_windows(branding: dict[str, Any]) -> None:
    url = branding.get("lockscreen_url")
    if not url:
        raise ValueError("lockscreen_url is empty")
    img_path = download_image(str(url), ".jpg")
    path_escaped = str(img_path).replace("'", "''")
    ps = f"""
$src = '{path_escaped}'
$dest = "$env:SystemRoot\\System32\\oobe\\info\\backgrounds\\backgroundDefault.jpg"
New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
Copy-Item $src $dest -Force
$path = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization'
New-Item -Force -Path $path | Out-Null
Set-ItemProperty -Path $path -Name LockScreenImage -Value $dest
Set-ItemProperty -Path $path -Name NoChangingLockScreen -Value 1
"""
    _ps_run(ps)


def _apply_screensaver_windows(branding: dict[str, Any], *, data_dir: Path | None) -> None:
    timeout = int(branding.get("screensaver_timeout_s") or 600)
    timeout = max(60, min(timeout, 3600))

    try:
        from win_session import has_interactive_session
    except ImportError:
        has_interactive_session = lambda: True  # type: ignore[assignment]

    if not has_interactive_session():
        raise RuntimeError("pending_session")

    from display_ipc import wait_for_display_result, write_display_task

    resolved_dir = _resolve_data_dir(data_dir)
    task_id = write_display_task(
        resolved_dir,
        {"type": "screensaver", "timeout_s": timeout},
    )
    result = wait_for_display_result(resolved_dir, task_id, timeout=30)
    if result is None:
        raise RuntimeError("pending_session")
    if result.get("status") == "error":
        raise RuntimeError(str(result.get("error") or "screensaver failed"))


def _apply_wallpaper_macos(branding: dict[str, Any]) -> None:
    url = branding.get("wallpaper_url")
    if not url:
        raise ValueError("wallpaper_url is empty")
    img_path = download_image(str(url), ".jpg")
    script = f'''
tell application "System Events"
  tell every desktop
    set picture to "{img_path}"
  end tell
end tell
'''
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        timeout=15,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "osascript failed")[:400])


def _apply_lockscreen_macos(branding: dict[str, Any]) -> None:
    url = branding.get("lockscreen_url")
    if not url:
        return
    img_path = download_image(str(url), ".jpg")
    subprocess.run(
        [
            "defaults",
            "write",
            "/Library/Preferences/com.apple.loginwindow",
            "DesktopPicture",
            str(img_path),
        ],
        timeout=10,
        capture_output=True,
        check=False,
    )


def _apply_screensaver_macos(branding: dict[str, Any]) -> None:
    timeout = int(branding.get("screensaver_timeout_s") or 600)
    subprocess.run(
        [
            "defaults",
            "-currentHost",
            "write",
            "com.apple.screensaver",
            "idleTime",
            "-int",
            str(timeout),
        ],
        timeout=10,
        capture_output=True,
        check=True,
    )
    message = branding.get("screensaver_message") or ""
    if message:
        subprocess.run(
            [
                "defaults",
                "-currentHost",
                "write",
                "com.apple.screensaver",
                "moduleDict",
                "-dict",
                "moduleName",
                "Message",
                "path",
                "/System/Library/Screen Savers/Message.saver",
                "type",
                "0",
            ],
            timeout=10,
            capture_output=True,
            check=False,
        )


def _linux_desktop_user() -> tuple[str, str] | None:
    try:
        who_out = subprocess.run(
            ["who"], capture_output=True, text=True, timeout=5
        ).stdout
        users = [line.split()[0] for line in who_out.splitlines() if "(:" in line]
        if not users:
            return None
        user = users[0]
        return user, _get_uid(user)
    except Exception:
        return None


def _apply_wallpaper_linux(branding: dict[str, Any]) -> None:
    url = branding.get("wallpaper_url")
    if not url:
        raise ValueError("wallpaper_url is empty")
    img_path = download_image(str(url), ".jpg")
    pair = _linux_desktop_user()
    if not pair:
        log.warning("Linux wallpaper: no desktop user session found")
        return
    user, uid = pair
    env = {
        **os.environ,
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
        "HOME": f"/home/{user}",
    }
    for key in ("picture-uri", "picture-uri-dark"):
        subprocess.run(
            [
                "sudo",
                "-u",
                user,
                "gsettings",
                "set",
                "org.gnome.desktop.background",
                key,
                f"file://{img_path}",
            ],
            env=env,
            timeout=10,
            capture_output=True,
            check=False,
        )


def _apply_lockscreen_linux(branding: dict[str, Any]) -> None:
    url = branding.get("lockscreen_url")
    if not url:
        return
    img_path = download_image(str(url), ".jpg")
    override_content = f"""
[org.gnome.login-screen]
logo='{img_path}'
"""
    try:
        override_path = Path(
            "/usr/share/glib-2.0/schemas/90_vizhi-lockscreen.gschema.override"
        )
        override_path.write_text(override_content)
        subprocess.run(
            ["glib-compile-schemas", "/usr/share/glib-2.0/schemas/"],
            timeout=15,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        log.warning("Linux lockscreen (GDM) failed: %s", exc)


def _apply_screensaver_linux(branding: dict[str, Any]) -> None:
    timeout = int(branding.get("screensaver_timeout_s") or 600)
    pair = _linux_desktop_user()
    if not pair:
        return
    user, uid = pair
    env = {
        **os.environ,
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }
    subprocess.run(
        [
            "sudo",
            "-u",
            user,
            "gsettings",
            "set",
            "org.gnome.desktop.session",
            "idle-delay",
            f"uint32 {timeout}",
        ],
        env=env,
        timeout=10,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "sudo",
            "-u",
            user,
            "gsettings",
            "set",
            "org.gnome.desktop.screensaver",
            "lock-enabled",
            "true",
        ],
        env=env,
        timeout=10,
        capture_output=True,
        check=False,
    )


def apply_wallpaper(
    branding: dict[str, Any],
    *,
    data_dir: Path | None = None,
) -> None:
    if sys.platform == "win32":
        _apply_wallpaper_windows(branding, data_dir=data_dir)
    elif sys.platform == "darwin":
        _apply_wallpaper_macos(branding)
    else:
        _apply_wallpaper_linux(branding)


def apply_lockscreen(branding: dict[str, Any]) -> None:
    if sys.platform == "win32":
        _apply_lockscreen_windows(branding)
    elif sys.platform == "darwin":
        _apply_lockscreen_macos(branding)
    else:
        _apply_lockscreen_linux(branding)


def apply_screensaver(
    branding: dict[str, Any],
    *,
    data_dir: Path | None = None,
) -> None:
    if sys.platform == "win32":
        _apply_screensaver_windows(branding, data_dir=data_dir)
    elif sys.platform == "darwin":
        _apply_screensaver_macos(branding)
    else:
        _apply_screensaver_linux(branding)
