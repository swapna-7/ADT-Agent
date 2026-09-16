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

if TYPE_CHECKING:
    from device_session import DeviceSession

log = logging.getLogger(__name__)

BRANDING_CACHE_DIR: Path | None = None

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
    if not jobs and not force:
        return
    for job in jobs:
        if isinstance(job, dict):
            apply_branding_job(job, branding or {}, api_base, device_token, session=session)


def apply_branding_job(
    job: dict[str, Any],
    branding: dict[str, Any],
    api_base: str,
    device_token: str,
    *,
    session: DeviceSession | None = None,
) -> None:
    job_id = str(job.get("job_id") or "")
    job_type = str(job.get("job_type") or "all")
    if not job_id:
        return

    reported_received = report_branding_result(
        api_base, device_token, job_id, "received", session=session
    )
    if not reported_received:
        log.error("Could not POST received for branding job %s", job_id)

    try:
        if job_type in ("wallpaper", "all") and branding.get("wallpaper_enabled"):
            apply_wallpaper(branding)
        if job_type in ("lockscreen", "all") and branding.get("lockscreen_enabled"):
            apply_lockscreen(branding)
        if job_type in ("screensaver", "all") and branding.get("screensaver_enabled"):
            apply_screensaver(branding)

        ok = report_branding_result(
            api_base, device_token, job_id, "completed", session=session
        )
        if not ok:
            log.error("Could not POST completed for branding job %s", job_id)
    except Exception as exc:
        ok = report_branding_result(
            api_base,
            device_token,
            job_id,
            "failed",
            error_message=str(exc)[:500],
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


def _apply_wallpaper_windows(branding: dict[str, Any]) -> None:
    url = branding.get("wallpaper_url")
    if not url:
        raise ValueError("wallpaper_url is empty")
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
    path_escaped = str(img_path).replace("'", "''")
    ps = f"""
Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' -Name WallpaperStyle -Value {style}
Set-ItemProperty -Path 'HKCU:\\Control Panel\\Desktop' -Name TileWallpaper -Value {tile}
Add-Type @"
using System.Runtime.InteropServices;
public class W {{
  [DllImport("user32.dll")]
  public static extern int SystemParametersInfo(int uAction, int uParam, string lpvParam, int fuWinIni);
}}
"@
[W]::SystemParametersInfo(20, 0, '{path_escaped}', 3)
"""
    _ps_run(ps)


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


def _apply_screensaver_windows(branding: dict[str, Any]) -> None:
    timeout = int(branding.get("screensaver_timeout_s") or 600)
    timeout = max(60, min(timeout, 3600))
    ps = f"""
$p = 'HKCU:\\Control Panel\\Desktop'
Set-ItemProperty -Path $p -Name ScreenSaveActive -Value 1
Set-ItemProperty -Path $p -Name ScreenSaveTimeOut -Value {timeout}
Set-ItemProperty -Path $p -Name SCRNSAVE.EXE -Value "$env:SystemRoot\\System32\\Scrnsave.scr"
Set-ItemProperty -Path $p -Name ScreenSaverIsSecure -Value 1
"""
    _ps_run(ps, timeout=15)


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


def apply_wallpaper(branding: dict[str, Any]) -> None:
    if sys.platform == "win32":
        _apply_wallpaper_windows(branding)
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


def apply_screensaver(branding: dict[str, Any]) -> None:
    if sys.platform == "win32":
        _apply_screensaver_windows(branding)
    elif sys.platform == "darwin":
        _apply_screensaver_macos(branding)
    else:
        _apply_screensaver_linux(branding)
