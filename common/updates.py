"""What this endpoint reports as installable right now, and what it is able to install.

Software Updates answers "what does the OS offer?" in the vendor's vocabulary: a Windows Update
classification, an apt security pocket, a dnf advisory. CVE matching is server-side against
advisory feeds.

Every update is pinned to an exact installer address — a WUA UpdateID GUID, or a package name.
Windows uses the Windows Update Agent COM API via win32com (no PowerShell, no winget). Linux uses
apt-get / dnf directly as root. Snap and flatpak remain optional secondary sources.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

try:
    from packaging.version import InvalidVersion, Version
except ImportError:
    Version = None  # type: ignore[misc, assignment]
    InvalidVersion = Exception  # type: ignore[misc, assignment]

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 600

UPDATE_CLASSES = ("security", "critical", "feature", "driver", "optional")

ProgressFn = Callable[[str], None]
ApplyResult = dict[str, Any]


class ScanResult(dict):
    """{"updates": [...], "capabilities": {...}, "reboot_pending": bool}"""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_linux() -> bool:
    return sys.platform == "linux"


def _run(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Command failed (%s): %s", cmd[0], exc)
        return None


# ---------------------------------------------------------------------------
# Windows: Windows Update Agent via win32com
# ---------------------------------------------------------------------------


def _msrc_to_class(severity: str | None) -> tuple[str, bool]:
    """Map WUA MsrcSeverity to (update_class, is_security)."""
    sev = (severity or "").strip().lower()
    if sev == "critical":
        return "critical", True
    if sev == "important":
        return "security", True
    return "optional", bool(sev in {"moderate", "low"})


def windows_reboot_pending() -> bool:
    """True when Windows has set the Auto Update RebootRequired key."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
        )
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _wua_wsus_managed() -> bool:
    try:
        import win32com.client  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        manager = win32com.client.Dispatch("Microsoft.Update.ServiceManager")
        for i in range(manager.Services.Count):
            svc = manager.Services.Item(i)
            if not bool(getattr(svc, "IsDefaultAUService", False)):
                continue
            name = str(getattr(svc, "Name", "") or "")
            if name and not re.search(r"Microsoft Update|Windows Update", name, re.I):
                return True
    except Exception:
        return False
    return False


def scan_windows_updates(timeout: int = DEFAULT_TIMEOUT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Applicable Windows updates via WUA COM. Each row is pinned to Identity.UpdateID."""
    del timeout  # COM has no timeout; callers still pass one for API symmetry.
    state: dict[str, Any] = {
        "wua": True,
        "winget": False,
        "reboot_pending": windows_reboot_pending(),
        "wsus_managed": False,
    }
    try:
        import win32com.client  # type: ignore[import-untyped]
    except ImportError as exc:
        state["wua"] = False
        state["wua_error"] = f"pywin32 unavailable: {exc}"
        return [], state

    updates: list[dict[str, Any]] = []
    try:
        state["wsus_managed"] = _wua_wsus_managed()
        try:
            sys_info = win32com.client.Dispatch("Microsoft.Update.SystemInfo")
            if bool(getattr(sys_info, "RebootRequired", False)):
                state["reboot_pending"] = True
        except Exception:
            pass

        session = win32com.client.Dispatch("Microsoft.Update.Session")
        session.ClientApplicationID = "Vizhi-ADT-Agent"
        searcher = session.CreateUpdateSearcher()
        result = searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'")

        for i in range(result.Updates.Count):
            u = result.Updates.Item(i)
            kb_ids: list[str] = []
            try:
                for k in range(u.KBArticleIDs.Count):
                    kb_ids.append(f"KB{u.KBArticleIDs.Item(k)}")
            except Exception:
                pass
            categories: list[str] = []
            try:
                for c in range(u.Categories.Count):
                    categories.append(str(u.Categories.Item(c).Name))
            except Exception:
                pass

            severity = str(getattr(u, "MsrcSeverity", "") or "").strip() or None
            update_class, is_security = _msrc_to_class(severity)
            try:
                reboot = int(u.InstallationBehavior.RebootBehavior) != 0
            except Exception:
                reboot = bool(getattr(u, "RebootRequired", False))
            try:
                size_bytes = int(u.MaxDownloadSize)
            except Exception:
                size_bytes = None

            uid = str(u.Identity.UpdateID)
            updates.append(
                {
                    "source": "windows_update",
                    "update_uid": uid,
                    "title": str(u.Title or "").strip(),
                    "package_name": None,
                    "current_version": None,
                    "available_version": None,
                    "kb_ids": kb_ids,
                    "cve_ids": [],
                    "severity": severity,
                    "update_class": update_class,
                    "is_security": is_security,
                    "requires_reboot": reboot,
                    "size_bytes": size_bytes,
                    "evidence": {
                        "categories": categories,
                        "revision": int(getattr(u.Identity, "RevisionNumber", 0) or 0),
                    },
                }
            )
    except Exception as exc:
        state["wua"] = False
        state["wua_error"] = str(exc)[:300]
        log.warning("WUA scan failed: %s", exc)
        return [], state

    return updates, state


# Alias used by older call sites / tests.
scan_windows_update = scan_windows_updates


def apply_windows_update(
    update_uid: str,
    timeout: int = 3600,
    on_progress: ProgressFn | None = None,
) -> ApplyResult:
    """Download and install one WUA update by UpdateID GUID."""
    del timeout
    try:
        import win32com.client  # type: ignore[import-untyped]
    except ImportError as exc:
        return _apply_ok(
            exit_code=1,
            error_code="pywin32_missing",
            error_message=f"pywin32 unavailable: {exc}",
        )

    uid = (update_uid or "").strip()
    if not uid:
        return _apply_ok(exit_code=1, error_code="missing_uid", error_message="No UpdateID.")

    try:
        session = win32com.client.Dispatch("Microsoft.Update.Session")
        session.ClientApplicationID = "Vizhi-ADT-Agent"
        searcher = session.CreateUpdateSearcher()
        # Prefer the exact UpdateID; fall back to a full pending search if the filter fails.
        try:
            result = searcher.Search(f"UpdateID='{uid}'")
        except Exception:
            result = searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'")

        update = None
        for i in range(result.Updates.Count):
            candidate = result.Updates.Item(i)
            if str(candidate.Identity.UpdateID) == uid:
                update = candidate
                break

        if update is None:
            return _apply_ok(
                exit_code=0,
                error_code="not_applicable",
                error_message="Update is no longer offered (superseded or already installed).",
            )

        if bool(getattr(update, "IsInstalled", False)):
            return _apply_ok(
                exit_code=0,
                stdout="already_installed",
                version_after=uid,
            )

        collection = win32com.client.Dispatch("Microsoft.Update.UpdateColl")
        collection.Add(update)

        if on_progress:
            on_progress("downloading")
        downloader = session.CreateUpdateDownloader()
        downloader.Updates = collection
        dl_result = downloader.Download()
        dl_code = int(getattr(dl_result, "ResultCode", 4) or 4)
        if dl_code not in (2, 3):
            return _apply_ok(
                exit_code=dl_code,
                error_code=str(dl_code),
                error_message=f"Windows Update download failed (ResultCode={dl_code}).",
            )

        if on_progress:
            on_progress("installing")
        installer = session.CreateUpdateInstaller()
        installer.Updates = collection
        install_result = installer.Install()
        install_code = int(getattr(install_result, "ResultCode", 4) or 4)
        reboot_required = bool(getattr(install_result, "RebootRequired", False))

        if install_code in (2, 3):
            return _apply_ok(
                exit_code=0,
                stdout=f"result_code={install_code}",
                reboot_required=reboot_required,
            )
        return _apply_ok(
            exit_code=install_code,
            error_code=str(install_code),
            error_message=f"Windows Update install failed (ResultCode={install_code}).",
            reboot_required=reboot_required,
        )
    except Exception as exc:
        log.exception("WUA install failed for %s", uid)
        return _apply_ok(exit_code=1, error_code="wua_exception", error_message=str(exc)[:500])


# ---------------------------------------------------------------------------
# Linux: distro detection + apt / dnf / snap / flatpak
# ---------------------------------------------------------------------------


def detect_linux_distro() -> tuple[str, str | None]:
    """Return (distro_id, pkg_manager) where pkg_manager is apt|dnf|yum|None."""
    facts: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                facts[key] = value.strip().strip('"')
    except FileNotFoundError:
        pass

    distro_id = (facts.get("ID") or "").lower()
    id_like = (facts.get("ID_LIKE") or "").lower().split()

    if distro_id in {"ubuntu", "debian", "linuxmint", "pop", "kali"} or "debian" in id_like:
        return distro_id or "debian", "apt"
    if distro_id in {"rhel", "centos", "almalinux", "rocky", "amzn", "fedora"} or any(
        x in id_like for x in ("rhel", "fedora", "centos")
    ):
        manager = "dnf" if shutil.which("dnf") else ("yum" if shutil.which("yum") else None)
        return distro_id or "rhel", manager
    return distro_id or "unknown", None


def linux_distro_facts() -> dict[str, Any]:
    """Facts merged into endpoint_agent_state.os_facts on each update report."""
    distro_id, pkg_manager = detect_linux_distro()
    release: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                release[key] = value.strip().strip('"')
    except FileNotFoundError:
        pass
    return {
        "distro": distro_id or release.get("ID"),
        "distro_release": release.get("VERSION_ID") or release.get("VERSION_CODENAME"),
        "distro_codename": release.get("VERSION_CODENAME"),
        "pretty_name": release.get("PRETTY_NAME"),
        "kernel": platform.release() or None,
        "pkg_manager": pkg_manager,
        "id_like": [part for part in (release.get("ID_LIKE") or "").split() if part],
        "reboot_required": linux_reboot_pending(),
    }


def _linux_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}}
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    return env


def scan_apt(timeout: int = DEFAULT_TIMEOUT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Upgradable packages via apt-get --simulate upgrade."""
    state: dict[str, Any] = {"apt": False}
    if not shutil.which("apt-get"):
        return [], state
    state["apt"] = True
    env = _linux_env()

    refresh = _run(["apt-get", "update", "-qq"], timeout=min(timeout, 120), env=env)
    if refresh is not None and refresh.returncode != 0:
        state["apt_error"] = (refresh.stderr or "").strip()[:300]

    proc = _run(["apt-get", "--simulate", "upgrade"], timeout=timeout, env=env)
    if proc is None:
        state["apt_error"] = "apt-get --simulate upgrade failed"
        return [], state

    updates: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("Inst "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        package_name = parts[1]
        installed_ver = None
        available_ver = None
        if len(parts) > 2 and parts[2].startswith("["):
            installed_ver = parts[2].strip("[]")
        if len(parts) > 3 and parts[3].startswith("("):
            available_ver = parts[3].strip("(").split()[0] if parts[3].strip("(") else None
        elif len(parts) > 2 and parts[2].startswith("("):
            available_ver = parts[2].strip("(").split()[0]
        is_security = "-security" in line
        updates.append(
            {
                "source": "apt",
                "update_uid": package_name,
                "title": package_name if not available_ver else f"{package_name} {available_ver}",
                "package_name": package_name,
                "current_version": installed_ver,
                "available_version": available_ver,
                "kb_ids": [],
                "cve_ids": [],
                "severity": None,
                "update_class": "security" if is_security else "optional",
                "is_security": is_security,
                "requires_reboot": False,
                "size_bytes": 0,
                "evidence": {},
            }
        )
    return updates, state


def scan_dnf(timeout: int = DEFAULT_TIMEOUT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Upgradable RPM packages via dnf/yum check-update."""
    _, pkg_manager = detect_linux_distro()
    manager = pkg_manager if pkg_manager in {"dnf", "yum"} else None
    if not manager:
        manager = "dnf" if shutil.which("dnf") else ("yum" if shutil.which("yum") else None)
    state: dict[str, Any] = {"dnf": bool(manager)}
    if not manager:
        return [], state

    proc = _run([manager, "check-update", "--quiet"], timeout=timeout, env=_linux_env())
    if proc is None:
        state["dnf_error"] = f"{manager} check-update failed"
        return [], state
    if proc.returncode not in (0, 100):
        state["dnf_error"] = (proc.stderr or "").strip()[:300]
        log.error("%s check-update failed: %s", manager, state["dnf_error"])
        return [], state

    updates: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or line.startswith("Last") or line.startswith("Obs"):
            continue
        pkg_arch = parts[0]
        if "." not in pkg_arch:
            continue
        package_name = pkg_arch.rsplit(".", 1)[0]
        available_ver = parts[1]
        repo = parts[2]
        if repo.lower() in {"repo", "repository"}:
            continue
        is_security = "security" in repo.lower()
        updates.append(
            {
                "source": "dnf",
                "update_uid": package_name,
                "title": f"{package_name} {available_ver}",
                "package_name": package_name,
                "current_version": None,
                "available_version": available_ver,
                "kb_ids": [],
                "cve_ids": [],
                "severity": None,
                "update_class": "security" if is_security else "optional",
                "is_security": is_security,
                "requires_reboot": False,
                "size_bytes": 0,
                "evidence": {"repo": repo, "nevra": pkg_arch},
            }
        )
    return updates, state


def scan_snap(timeout: int = DEFAULT_TIMEOUT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {"snap": bool(shutil.which("snap"))}
    if not state["snap"]:
        return [], state
    proc = _run(["snap", "refresh", "--list"], timeout=timeout, env=_linux_env())
    if proc is None:
        return [], state

    updates: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0] == "Name":
            continue
        name, version = parts[0], parts[1]
        updates.append(
            {
                "source": "snap",
                "update_uid": name,
                "title": f"{name} {version}",
                "package_name": name,
                "current_version": None,
                "available_version": version,
                "kb_ids": [],
                "cve_ids": [],
                "severity": None,
                "update_class": "optional",
                "is_security": False,
                "requires_reboot": False,
                "size_bytes": None,
                "evidence": {"publisher": parts[2] if len(parts) > 2 else None},
            }
        )
    return updates, state


def scan_flatpak(timeout: int = DEFAULT_TIMEOUT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state: dict[str, Any] = {"flatpak": bool(shutil.which("flatpak"))}
    if not state["flatpak"]:
        return [], state
    proc = _run(
        ["flatpak", "remote-ls", "--updates", "--columns=application,version", "--plain"],
        timeout=timeout,
        env=_linux_env(),
    )
    if proc is None:
        return [], state

    updates: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t") if "\t" in line else line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        app_id, version = parts[0].strip(), parts[1].strip()
        if not app_id:
            continue
        updates.append(
            {
                "source": "flatpak",
                "update_uid": app_id,
                "title": f"{app_id} {version}",
                "package_name": app_id,
                "current_version": None,
                "available_version": version or None,
                "kb_ids": [],
                "cve_ids": [],
                "severity": None,
                "update_class": "optional",
                "is_security": False,
                "requires_reboot": False,
                "size_bytes": None,
                "evidence": {},
            }
        )
    return updates, state


def linux_reboot_pending() -> bool:
    if os.path.exists("/var/run/reboot-required") or os.path.exists("/run/reboot-required"):
        return True
    if shutil.which("needs-restarting"):
        proc = _run(["needs-restarting", "-r"], timeout=60, env=_linux_env())
        if proc is not None and proc.returncode == 1:
            return True
    return False


def has_root() -> bool:
    """Whether this process can actually install packages."""
    if _is_windows():
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


_ROOT_REQUIRED_SOURCES = frozenset(
    {"windows_update", "apt", "dnf", "snap", "flatpak", "registry_app", "chocolatey"}
)


def _version_gt(a: str, b: str) -> bool:
    from version_compare import version_gt

    return version_gt(a, b)


# ---------------------------------------------------------------------------
# Windows: registry_app (vendor installers via manifest)
# ---------------------------------------------------------------------------

_MANIFEST_CACHE: dict[str, Any] = {"data": [], "fetched_at": 0.0}
_MANIFEST_LOCK = threading.Lock()
_MANIFEST_TTL = 6 * 3600


def read_installed_apps() -> dict[str, dict[str, Any]]:
    """Installed apps from uninstall registry keys, keyed by DisplayName."""
    if not _is_windows():
        return {}
    try:
        import winreg
    except ImportError:
        return {}

    uninstall_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    apps: dict[str, dict[str, Any]] = {}
    for hive, path in uninstall_keys:
        try:
            key = winreg.OpenKey(hive, path)
        except FileNotFoundError:
            continue
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(key, index)
                index += 1
            except OSError:
                break
            try:
                sub = winreg.OpenKey(key, sub_name)

                def qv(name: str) -> str | None:
                    try:
                        return str(winreg.QueryValueEx(sub, name)[0])
                    except OSError:
                        return None

                name = qv("DisplayName")
                version = qv("DisplayVersion")
                pub = qv("Publisher")
                uid = qv("UninstallString") or name
                winreg.CloseKey(sub)
                if not name or not version:
                    continue
                if name in apps:
                    if Version is not None:
                        try:
                            if Version(version) <= Version(apps[name]["version_current"]):
                                continue
                        except InvalidVersion:
                            pass
                apps[name] = {
                    "display_name": name,
                    "version_current": version,
                    "publisher": pub,
                    "registry_uid": uid,
                }
            except Exception:
                pass
        winreg.CloseKey(key)
    return apps


def fetch_app_manifest(api_base: str, device_token: str) -> list[dict[str, Any]]:
    """Manifest entries from /api/agent/app-manifest; cached 6h, stale-while-error."""
    with _MANIFEST_LOCK:
        now = time.time()
        if now - float(_MANIFEST_CACHE["fetched_at"]) < _MANIFEST_TTL:
            return list(_MANIFEST_CACHE["data"])
        try:
            import requests

            from enrollment import device_headers

            base = (api_base or "").strip().rstrip("/")
            if not base or not device_token:
                return list(_MANIFEST_CACHE["data"])
            resp = requests.get(
                f"{base}/api/agent/app-manifest",
                headers=device_headers(device_token),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                _MANIFEST_CACHE["data"] = data
                _MANIFEST_CACHE["fetched_at"] = now
        except Exception as exc:
            log.warning("app-manifest fetch failed: %s — using cached manifest", exc)
        return list(_MANIFEST_CACHE["data"])


def resolve_latest_version(entry: dict[str, Any]) -> str | None:
    """Fetch version_feed_url and extract version per manifest entry rules."""
    try:
        import requests as req

        url = str(entry.get("version_feed_url") or "")
        if not url:
            return None
        resp = req.get(url, timeout=10, headers={"User-Agent": "Vizhi-ADT-Agent/1.0"})
        resp.raise_for_status()
        version: str | None = None
        if entry.get("version_path"):
            data = resp.json()
            obj: Any = data
            path = str(entry["version_path"])
            for part in path.replace("[", ".").replace("]", "").split("."):
                if part == "":
                    continue
                if isinstance(obj, list):
                    obj = obj[int(part)]
                else:
                    obj = obj[part]
            version = str(obj)
        elif entry.get("version_parse") == "first_line_version":
            first_line = resp.text.splitlines()[0]
            match = re.search(r"(\d+[\.\d]+)", first_line)
            version = match.group(1) if match else None
        elif entry.get("version_parse") == "first_href_version":
            match = re.search(r'href="([^"]*)"', resp.text)
            if match:
                href_match = re.search(r"(\d+\.\d+[\.\d]*)", match.group(1))
                version = href_match.group(1) if href_match else None
        else:
            return None
        transform = str(entry.get("version_transform") or "")
        if version and transform == "strip_v_prefix":
            version = version.lstrip("v")
        return version
    except Exception as exc:
        log.warning(
            "version feed failed for %s: %s",
            entry.get("canonical_id"),
            exc,
        )
        return None


def _match_installed_app(
    entry: dict[str, Any],
    installed: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Match manifest match_names against registry DisplayNames (prefix/contains)."""
    publisher_hint = str(entry.get("publisher_hint") or "").strip().lower()
    match_names = entry.get("match_names") or []

    for display_name, app in installed.items():
        name_lower = display_name.lower()
        for match_name in match_names:
            needle = str(match_name).strip().lower()
            if not needle:
                continue
            if name_lower == needle or needle in name_lower or name_lower.startswith(needle):
                if publisher_hint:
                    pub = str(app.get("publisher") or "").lower()
                    if publisher_hint not in pub and pub not in publisher_hint:
                        continue
                return app
    return None


def scan_registry_app_channel(
    api_base: str | None,
    device_token: str | None,
    endpoint_role: str | None,
) -> list[dict[str, Any]]:
    if not api_base or not device_token:
        return []
    manifest = fetch_app_manifest(api_base, device_token)
    if not manifest:
        return []
    installed = read_installed_apps()
    rows: list[dict[str, Any]] = []
    for entry in manifest:
        if not entry.get("enabled", True):
            continue
        if entry.get("dev_only", False):
            if "dev" not in (endpoint_role or "").lower():
                continue
        matched_app = _match_installed_app(entry, installed)
        if not matched_app:
            continue

        version_current = matched_app["version_current"]
        skip_check = bool(entry.get("skip_version_check"))

        if skip_check:
            if entry.get("install_cooldown_active"):
                continue
            version_available = "latest"
        else:
            version_available = entry.get("resolved_version")
            if not version_available:
                continue
            if not _version_gt(str(version_available), str(version_current)):
                continue

        update_class = str(entry.get("update_class") or "optional")
        if update_class not in UPDATE_CLASSES:
            update_class = "optional"
        category = str(entry.get("category") or "APPLICATION")
        rows.append(
            {
                "source": "registry_app",
                "update_uid": str(entry["canonical_id"]),
                "title": f"{matched_app['display_name']} {version_available}",
                "package_name": str(entry["canonical_id"]),
                "current_version": version_current,
                "available_version": version_available,
                "kb_ids": [],
                "cve_ids": [],
                "update_class": update_class,
                "category": category,
                "is_security": update_class in {"security", "critical"},
                "requires_reboot": False,
                "size_bytes": 0,
            }
        )
    return rows


def _format_installer_url(template: str, version: str) -> str:
    parts = version.split(".")
    return template.format(
        version=version,
        version_nodot=version.replace(".", ""),
        tag=f"v{version}" if not version.startswith("v") else version,
        version_major=parts[0] if parts else version,
        version_minor=parts[1] if len(parts) > 1 else "0",
    )


def _interpret_install_exit(
    exit_code: int,
    requires_reboot_policy: str,
    windows_reboot: bool,
) -> tuple[bool, bool]:
    """Return (success, reboot_required)."""
    policy = (requires_reboot_policy or "auto").lower()
    if exit_code == 3010:
        return True, True
    if exit_code == 0:
        if policy == "always":
            return True, True
        return True, windows_reboot
    return False, False


def install_registry_app(
    update_uid: str,
    target_version: str | None,
    manifest_entry: dict[str, Any],
    *,
    version_before: str | None = None,
) -> ApplyResult:
    from install_verify import assert_apt_command_safe, verify_download

    skip_check = bool(manifest_entry.get("skip_version_check"))
    version = target_version or manifest_entry.get("available_version")
    if not version and not skip_check:
        return _apply_ok(
            exit_code=1,
            error_code="no_target_version",
            error_message="No target_version in job or manifest",
        )
    if skip_check:
        version = "latest"

    raw_url = str(manifest_entry.get("installer_url") or "")
    if not raw_url:
        return _apply_ok(
            exit_code=0,
            error_code="not_applicable",
            error_message="No installer_url in manifest entry",
        )

    ver = str(version) if version != "latest" else "latest"
    url = _format_installer_url(raw_url, ver) if ver != "latest" else raw_url

    installer_type = str(manifest_entry.get("installer_type") or "exe").lower()
    install_method = str(manifest_entry.get("install_method") or "vendor_installer").lower()

    if _is_linux() and installer_type == "deb_url":
        tmp_path = f"/tmp/{update_uid}.deb"
        try:
            urllib.request.urlretrieve(url, tmp_path)
        except Exception as exc:
            return _apply_ok(exit_code=1, error_code="download_failed", error_message=str(exc))
        ok_verify, err_code, err_msg = verify_download(tmp_path, manifest_entry)
        if not ok_verify:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return _apply_ok(exit_code=1, error_code=err_code, error_message=err_msg)
        cmd = ["apt-get", "install", "-y", tmp_path]
        try:
            assert_apt_command_safe(cmd)
        except ValueError as exc:
            return _apply_ok(exit_code=1, error_code="unsafe_apt_command", error_message=str(exc))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        success, reboot = _interpret_install_exit(
            proc.returncode,
            str(manifest_entry.get("requires_reboot") or "auto"),
            linux_reboot_pending(),
        )
        return _apply_ok(
            exit_code=0 if success else proc.returncode,
            stdout=(proc.stdout or "")[-2000:],
            stderr=(proc.stderr or "")[-1000:],
            reboot_required=reboot,
            error_code=None if success else "installer_failed",
            error_message=(proc.stderr or "")[:500] if not success else None,
        )

    tmp_dir = r"C:\ProgramData\ADT Agent\tmp" if _is_windows() else "/var/lib/vizhi-agent/tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    suffix = ".msi" if url.lower().endswith(".msi") else ".exe"
    if installer_type == "zip":
        suffix = ".zip"
    tmp_path = os.path.join(tmp_dir, f"{update_uid}{suffix}")

    try:
        urllib.request.urlretrieve(url, tmp_path)
    except Exception as exc:
        return _apply_ok(
            exit_code=1,
            error_code="download_failed",
            error_message=f"Download failed: {exc}",
        )

    ok_verify, err_code, err_msg = verify_download(tmp_path, manifest_entry)
    if not ok_verify:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return _apply_ok(exit_code=1, error_code=err_code, error_message=err_msg)

    if installer_type == "zip" and install_method == "extract_and_replace":
        import zipfile

        extract_dir = os.path.join(tmp_dir, f"{update_uid}-extract")
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(extract_dir)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return _apply_ok(
            exit_code=0,
            stdout=f"Extracted zip to {extract_dir}",
            reboot_required=False,
            error_code="zip_manual_replace",
            error_message="zip extract_and_replace requires service stop/replace — partial",
        )

    args = list(manifest_entry.get("installer_args") or [])
    if suffix == ".msi":
        cmd = ["msiexec", "/i", tmp_path, "/quiet", "/norestart"] + args
    else:
        cmd = [tmp_path] + args

    proc: subprocess.CompletedProcess[str] | None = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return _apply_ok(exit_code=1, error_code="installer_timeout", error_message="Installer timed out after 600s")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if proc is None:
        return _apply_ok(exit_code=1, error_message="Installer did not run")

    apps_after = read_installed_apps()
    version_after: str | None = None
    matched = _match_installed_app(manifest_entry, apps_after)
    if matched:
        version_after = matched["version_current"]

    reboot_flag = windows_reboot_pending() if _is_windows() else linux_reboot_pending()
    success, reboot_required = _interpret_install_exit(
        proc.returncode,
        str(manifest_entry.get("requires_reboot") or "auto"),
        reboot_flag,
    )

    before = version_before
    if before and version_after and not skip_check:
        if not _version_gt(version_after, before):
            return _apply_ok(
                exit_code=proc.returncode or 1,
                stdout=(proc.stdout or "")[-2000:],
                stderr=(proc.stderr or "")[-1000:],
                reboot_required=reboot_required,
                version_after=version_after,
                error_code="version_unchanged_after_install",
                error_message="version_unchanged_after_install",
            )

    return _apply_ok(
        exit_code=0 if success else proc.returncode,
        stdout=(proc.stdout or "")[-2000:],
        stderr=(proc.stderr or "")[-1000:],
        reboot_required=reboot_required,
        version_after=version_after,
        error_code=None if success else "installer_failed",
        error_message=(proc.stderr or "")[:500] if not success else None,
    )


def _manifest_entry_for_uid(
    api_base: str | None,
    device_token: str | None,
    update_uid: str,
) -> dict[str, Any] | None:
    if not api_base or not device_token:
        return None
    for entry in fetch_app_manifest(api_base, device_token):
        if str(entry.get("canonical_id") or "") == update_uid:
            return entry
    return None


# ---------------------------------------------------------------------------
# npm_global / pip_global (dev role only)
# ---------------------------------------------------------------------------


def scan_npm_global(endpoint_role: str | None) -> list[dict[str, Any]]:
    if "dev" not in (endpoint_role or "").lower():
        return []
    npm = shutil.which("npm")
    if not npm:
        return []
    result = subprocess.run(
        [npm, "outdated", "--global", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    rows: list[dict[str, Any]] = []
    for pkg_name, info in data.items():
        if not isinstance(info, dict):
            continue
        rows.append(
            {
                "source": "npm_global",
                "update_uid": pkg_name,
                "title": f"{pkg_name} {info.get('wanted', '?')}",
                "package_name": pkg_name,
                "update_class": "optional",
                "is_security": False,
                "requires_reboot": False,
                "current_version": info.get("current"),
                "available_version": info.get("wanted"),
                "kb_ids": [],
                "cve_ids": [],
                "size_bytes": None,
            }
        )
    return rows


def install_npm_package(package_name: str) -> ApplyResult:
    npm = shutil.which("npm")
    if not npm:
        return _apply_ok(exit_code=1, error_message="npm not found")
    proc = subprocess.run(
        [npm, "install", "--global", package_name],
        capture_output=True,
        text=True,
        timeout=300,
    )
    ver = subprocess.run(
        [npm, "list", "--global", "--depth=0", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    version_after: str | None = None
    try:
        tree = json.loads(ver.stdout or "{}")
        version_after = (
            (tree.get("dependencies") or {}).get(package_name, {}).get("version")
        )
    except Exception:
        pass
    return _apply_ok(
        exit_code=proc.returncode,
        stdout=(proc.stdout or "")[-2000:],
        stderr=(proc.stderr or "")[-1000:],
        reboot_required=False,
        version_after=version_after,
        error_code=None if proc.returncode == 0 else "npm_failed",
        error_message=(proc.stderr or "")[:500] if proc.returncode != 0 else None,
    )


def scan_pip_global(endpoint_role: str | None) -> list[dict[str, Any]]:
    if "dev" not in (endpoint_role or "").lower():
        return []
    pip = shutil.which("pip3") or shutil.which("pip")
    if not pip:
        return []
    result = subprocess.run(
        [pip, "list", "--outdated", "--format=json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        {
            "source": "pip_global",
            "update_uid": pkg["name"],
            "title": f"{pkg['name']} {pkg['latest_version']}",
            "package_name": pkg["name"],
            "update_class": "optional",
            "is_security": False,
            "requires_reboot": False,
            "current_version": pkg.get("version"),
            "available_version": pkg.get("latest_version"),
            "kb_ids": [],
            "cve_ids": [],
            "size_bytes": None,
        }
        for pkg in data
        if isinstance(pkg, dict) and pkg.get("name")
    ]


def install_pip_package(package_name: str) -> ApplyResult:
    pip = shutil.which("pip3") or shutil.which("pip")
    if not pip:
        return _apply_ok(exit_code=1, error_message="pip not found")
    proc = subprocess.run(
        [pip, "install", "--upgrade", package_name],
        capture_output=True,
        text=True,
        timeout=300,
    )
    ver = subprocess.run([pip, "show", package_name], capture_output=True, text=True, timeout=30)
    version_after: str | None = None
    for line in (ver.stdout or "").splitlines():
        if line.startswith("Version:"):
            version_after = line.split(":", 1)[1].strip()
    return _apply_ok(
        exit_code=proc.returncode,
        stdout=(proc.stdout or "")[-2000:],
        stderr=(proc.stderr or "")[-1000:],
        reboot_required=False,
        version_after=version_after,
        error_code=None if proc.returncode == 0 else "pip_failed",
        error_message=(proc.stderr or "")[:500] if proc.returncode != 0 else None,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def scan_updates(
    timeout: int = DEFAULT_TIMEOUT,
    *,
    api_base: str | None = None,
    device_token: str | None = None,
    endpoint_role: str | None = None,
) -> ScanResult:
    """Everything installable on this endpoint, plus what it is able to install."""
    updates: list[dict[str, Any]] = []
    capabilities: dict[str, Any] = {"root": has_root(), "platform": sys.platform}
    reboot_pending = False
    scanned: list[str] = []

    if _is_windows():
        wua_updates, wua_state = scan_windows_updates(timeout)
        updates.extend(wua_updates)
        reboot_pending = bool(wua_state.pop("reboot_pending", False)) or windows_reboot_pending()
        capabilities.update(wua_state)
        capabilities["winget"] = False
        capabilities["registry_app"] = True
        if wua_state.get("wua"):
            scanned.append("windows_update")
        scanned.append("winget")
        registry_rows = scan_registry_app_channel(api_base, device_token, endpoint_role)
        updates.extend(registry_rows)
        scanned.append("registry_app")

        from chocolatey import (
            dedupe_chocolatey_updates,
            detect_chocolatey,
            scan_chocolatey_outdated,
        )

        choco_cap = detect_chocolatey()
        capabilities.update(choco_cap)
        if choco_cap.get("chocolatey") and choco_cap.get("choco_path"):
            manifest = fetch_app_manifest(api_base, device_token) if api_base and device_token else []
            choco_rows = scan_chocolatey_outdated(str(choco_cap["choco_path"]))
            choco_rows = dedupe_chocolatey_updates(choco_rows, registry_rows, manifest)
            updates.extend(choco_rows)
            if choco_rows:
                scanned.append("chocolatey")
            elif choco_cap.get("chocolatey"):
                scanned.append("chocolatey")

        npm_rows = scan_npm_global(endpoint_role)
        updates.extend(npm_rows)
        if "dev" in (endpoint_role or "").lower() and shutil.which("npm"):
            scanned.append("npm_global")
        pip_rows = scan_pip_global(endpoint_role)
        updates.extend(pip_rows)
        if "dev" in (endpoint_role or "").lower() and (shutil.which("pip3") or shutil.which("pip")):
            scanned.append("pip_global")
        capabilities["npm_global"] = bool(shutil.which("npm"))
        capabilities["pip_global"] = bool(shutil.which("pip3") or shutil.which("pip"))

    elif _is_linux():
        distro_id, pkg_manager = detect_linux_distro()
        capabilities["distro_id"] = distro_id
        capabilities["pkg_manager"] = pkg_manager

        if pkg_manager is None:
            capabilities["apt"] = False
            capabilities["dnf"] = False
            capabilities["can_patch"] = False
        elif pkg_manager == "apt":
            apt_updates, apt_state = scan_apt(timeout)
            updates.extend(apt_updates)
            capabilities.update(apt_state)
            capabilities["dnf"] = False
            if apt_state.get("apt") and not apt_state.get("apt_error"):
                scanned.append("apt")
        else:
            dnf_updates, dnf_state = scan_dnf(timeout)
            updates.extend(dnf_updates)
            capabilities.update(dnf_state)
            capabilities["apt"] = False
            if dnf_state.get("dnf") and not dnf_state.get("dnf_error"):
                scanned.append("dnf")

        if pkg_manager is not None:
            snap_updates, snap_state = scan_snap(timeout)
            updates.extend(snap_updates)
            capabilities.update(snap_state)
            if snap_state.get("snap"):
                scanned.append("snap")

            flatpak_updates, flatpak_state = scan_flatpak(timeout)
            updates.extend(flatpak_updates)
            capabilities.update(flatpak_state)
            if flatpak_state.get("flatpak"):
                scanned.append("flatpak")

        reboot_pending = linux_reboot_pending()
        npm_rows = scan_npm_global(endpoint_role)
        updates.extend(npm_rows)
        if "dev" in (endpoint_role or "").lower() and shutil.which("npm"):
            scanned.append("npm_global")
        pip_rows = scan_pip_global(endpoint_role)
        updates.extend(pip_rows)
        if "dev" in (endpoint_role or "").lower() and (shutil.which("pip3") or shutil.which("pip")):
            scanned.append("pip_global")
        capabilities["npm_global"] = bool(shutil.which("npm"))
        capabilities["pip_global"] = bool(shutil.which("pip3") or shutil.which("pip"))

    else:
        capabilities["unsupported_platform"] = platform.system()
        capabilities["winget"] = False

    capabilities["scanned_sources"] = scanned
    capabilities["can_patch"] = bool(
        capabilities.get("root")
        and (capabilities.get("wua") or capabilities.get("apt") or capabilities.get("dnf"))
    )

    def class_rank(value: str) -> int:
        try:
            return UPDATE_CLASSES.index(value)
        except ValueError:
            return len(UPDATE_CLASSES)

    updates.sort(key=lambda u: (class_rank(str(u.get("update_class") or "optional")), u.get("title") or ""))
    return ScanResult(
        updates=updates,
        capabilities=capabilities,
        reboot_pending=reboot_pending,
    )


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


def _apply_ok(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    reboot_required: bool = False,
    version_after: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ApplyResult:
    not_applicable = error_code == "not_applicable"
    return {
        "ok": exit_code == 0 and not not_applicable,
        "exit_code": exit_code,
        "stdout": (stdout or "")[:8000],
        "stderr": (stderr or "")[:8000],
        "reboot_required": reboot_required,
        "version_after": version_after,
        "error_code": error_code,
        "error_message": error_message,
        "not_applicable": not_applicable,
    }


def apply_apt(package_name: str, target_version: str | None, timeout: int = 7200) -> ApplyResult:
    pkg_spec = f"{package_name}={target_version}" if target_version else package_name
    proc = _run(
        ["apt-get", "install", "--only-upgrade", "-y", pkg_spec],
        timeout=timeout,
        env=_linux_env(),
    )
    if proc is None:
        return _apply_ok(exit_code=1, error_code="apt_timeout", error_message="apt-get timed out.")
    stderr = proc.stderr or ""
    code_name = None
    if "Could not get lock" in stderr:
        code_name = "apt_lock"
    elif "dpkg was interrupted" in stderr:
        code_name = "dpkg_interrupted"
    version_after = None
    ver_proc = _run(
        ["dpkg-query", "-W", "-f=${Version}", package_name],
        timeout=30,
        env=_linux_env(),
    )
    if ver_proc is not None and ver_proc.returncode == 0:
        version_after = (ver_proc.stdout or "").strip() or None
    return _apply_ok(
        exit_code=proc.returncode,
        stdout=(proc.stdout or "")[-4000:],
        stderr=stderr[-2000:],
        reboot_required=os.path.exists("/var/run/reboot-required")
        or os.path.exists("/run/reboot-required"),
        error_code=code_name if proc.returncode != 0 else None,
        error_message=stderr[:500] if proc.returncode != 0 else None,
        version_after=version_after,
    )


def apply_dnf(package_name: str, target_version: str | None, timeout: int = 7200) -> ApplyResult:
    del target_version
    _, pkg_manager = detect_linux_distro()
    manager = pkg_manager if pkg_manager in {"dnf", "yum"} else ("dnf" if shutil.which("dnf") else "yum")
    proc = _run([manager, "update", "-y", package_name], timeout=timeout, env=_linux_env())
    if proc is None:
        return _apply_ok(exit_code=1, error_code="dnf_timeout", error_message=f"{manager} timed out.")
    version_after = None
    ver_proc = _run(
        ["rpm", "-q", "--queryformat", "%{VERSION}-%{RELEASE}", package_name],
        timeout=30,
        env=_linux_env(),
    )
    if ver_proc is not None and ver_proc.returncode == 0:
        version_after = (ver_proc.stdout or "").strip() or None
    reboot = False
    if shutil.which("needs-restarting"):
        reboot_proc = _run(["needs-restarting", "-r"], timeout=30, env=_linux_env())
        reboot = reboot_proc is not None and reboot_proc.returncode == 1
    return _apply_ok(
        exit_code=proc.returncode,
        stdout=(proc.stdout or "")[-4000:],
        stderr=(proc.stderr or "")[-2000:],
        reboot_required=reboot,
        error_code=None if proc.returncode == 0 else "dnf_failed",
        error_message=(proc.stderr or "")[:500] if proc.returncode != 0 else None,
        version_after=version_after,
    )


def apply_snap(package_name: str, timeout: int = 1800) -> ApplyResult:
    proc = _run(["snap", "refresh", package_name], timeout=timeout, env=_linux_env())
    if proc is None:
        return _apply_ok(exit_code=1, error_code="snap_timeout", error_message="snap refresh timed out.")
    return _apply_ok(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error_code=None if proc.returncode == 0 else "snap_failed",
        error_message=(proc.stderr or "")[:500] if proc.returncode != 0 else None,
    )


def apply_flatpak(package_name: str, timeout: int = 1800) -> ApplyResult:
    proc = _run(["flatpak", "update", "-y", package_name], timeout=timeout, env=_linux_env())
    if proc is None:
        return _apply_ok(exit_code=1, error_code="flatpak_timeout", error_message="flatpak update timed out.")
    return _apply_ok(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error_code=None if proc.returncode == 0 else "flatpak_failed",
        error_message=(proc.stderr or "")[:500] if proc.returncode != 0 else None,
    )


def apply_update(
    source: str,
    update_uid: str,
    *,
    package_name: str | None = None,
    target_version: str | None = None,
    timeout: int = 3600,
    on_progress: ProgressFn | None = None,
    api_base: str | None = None,
    device_token: str | None = None,
    endpoint_role: str | None = None,
    version_before: str | None = None,
) -> ApplyResult:
    """Install exactly one approved update. Never takes a shell string from the server."""
    if source == "winget":
        return _apply_ok(
            exit_code=1,
            error_code="winget_retired",
            error_message="winget is no longer used; install via Windows Update or a Linux package manager.",
        )
    if source in _ROOT_REQUIRED_SOURCES and not has_root():
        return _apply_ok(
            exit_code=1,
            error_code="blocked_no_root",
            error_message="This agent is not running with install privileges.",
        )
    if source == "windows_update":
        return apply_windows_update(update_uid, timeout, on_progress=on_progress)
    if source == "registry_app":
        entry = _manifest_entry_for_uid(api_base, device_token, update_uid)
        if not entry:
            return _apply_ok(
                exit_code=1,
                error_code="manifest_missing",
                error_message=f"No manifest entry for {update_uid}",
            )
        if on_progress:
            on_progress("downloading")
            on_progress("installing")
        return install_registry_app(
            update_uid,
            target_version,
            entry,
            version_before=version_before,
        )
    if source == "npm_global":
        if on_progress:
            on_progress("installing")
        return install_npm_package(package_name or update_uid)
    if source == "pip_global":
        if on_progress:
            on_progress("installing")
        return install_pip_package(package_name or update_uid)
    if source == "apt":
        if on_progress:
            on_progress("installing")
        return apply_apt(package_name or update_uid, target_version, max(timeout, 7200))
    if source == "dnf":
        if on_progress:
            on_progress("installing")
        return apply_dnf(package_name or update_uid, target_version, max(timeout, 7200))
    if source == "snap":
        if on_progress:
            on_progress("installing")
        return apply_snap(package_name or update_uid, timeout)
    if source == "flatpak":
        if on_progress:
            on_progress("installing")
        return apply_flatpak(package_name or update_uid, timeout)
    if source == "chocolatey":
        from chocolatey import detect_chocolatey, install_choco_package

        choco_cap = detect_chocolatey()
        choco_path = choco_cap.get("choco_path")
        if not choco_path:
            return _apply_ok(
                exit_code=1,
                error_code="chocolatey_missing",
                error_message="Chocolatey is not installed on this endpoint.",
            )
        choco_id = (package_name or update_uid.replace("choco:", "", 1) if update_uid else "") or ""
        if on_progress:
            on_progress("installing")
        raw = install_choco_package(str(choco_path), choco_id, target_version)
        return _apply_ok(
            exit_code=int(raw.get("exit_code") or 1),
            stdout=str(raw.get("stdout") or ""),
            stderr=str(raw.get("stderr") or ""),
            reboot_required=bool(raw.get("reboot_required")),
            version_after=raw.get("version_after"),
            error_code=raw.get("error_code"),
            error_message=raw.get("error_message"),
        )
    return _apply_ok(exit_code=1, error_code="unknown_source", error_message=f"Unsupported source {source}.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print(json.dumps(scan_updates(), indent=2, ensure_ascii=False))
