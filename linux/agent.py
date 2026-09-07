"""ADT Agent — Linux edition.

Installs as a root systemd unit under /opt/vizhi-agent, because apt and dnf cannot install
packages as an unprivileged user and a metrics-only agent cannot patch anything. When root is
unavailable the agent falls back to a per-user unit and reports that it cannot patch, rather than
installing in a state where rollouts would fail with no explanation.

Remote commands run under /bin/bash -lc.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import requests

_COMMON = Path(__file__).resolve().parent.parent / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))
from api_base import resolve_api_base
from command_poller import start_command_poller
from config_io import load_config, load_local_config, write_config
from device_session import DeviceSession
from enrollment import platform_tag, system_hostname, system_username
from first_run import needs_enrollment, prompt_and_enroll
from normalize import normalize_row
from patch_runner import start_patch_poller
from report import (
    fetch_pending_commands as fetch_agent_commands,
    report_command_result,
    report_telemetry,
    report_updates,
)
from scheduler import load_last_runs, mark_run, resolve_intervals, should_run
from self_update import (
    cleanup_previous_backup,
    maybe_apply_update,
    next_check_deadline,
    read_auto_update_flag,
    should_self_update,
)
from software_quality import filter_software_rows, is_plausible_software_row
from updates import linux_distro_facts, scan_updates
from version import AGENT_VERSION

if sys.platform != "linux":
    print("This binary is for Linux only.", file=sys.stderr)
    raise SystemExit(2)

SYSTEMD_UNIT_NAME = "adt-agent.service"
AGENT_STOP_COMMAND = "__ADT_AGENT_STOP__"

# Root system unit paths. Required for patching: apt/dnf cannot install as an unprivileged user.
ROOT_INSTALL_ROOT = Path("/opt/vizhi-agent")
ROOT_DATA_DIR = Path("/var/lib/vizhi-agent")
ROOT_UNIT_PATH = Path("/etc/systemd/system") / SYSTEMD_UNIT_NAME

# Per-user fallback, kept for metrics-only installs where root is unavailable.
USER_INSTALL_ROOT = Path.home() / ".config" / "vizhi-agent"
USER_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# Resolved by configure_install_mode(); root mode is the default whenever the process is root.
SYSTEM_MODE = is_root()
INSTALL_ROOT = ROOT_INSTALL_ROOT if SYSTEM_MODE else USER_INSTALL_ROOT
INSTALL_BIN = INSTALL_ROOT / "adt-agent"
DATA_DIR = ROOT_DATA_DIR if SYSTEM_MODE else USER_INSTALL_ROOT
SYSTEMD_UNIT_PATH = ROOT_UNIT_PATH if SYSTEM_MODE else USER_UNIT_PATH


def configure_install_mode(force_user_mode: bool = False) -> None:
    """Pick root-system vs per-user layout.

    Root mode is what makes patching possible, so it wins whenever the process has root. An
    existing per-user install is not left in place: it is migrated, because a user-session unit can
    never run apt or dnf and would leave the endpoint permanently unpatchable.
    """
    global SYSTEM_MODE, INSTALL_ROOT, INSTALL_BIN, DATA_DIR, SYSTEMD_UNIT_PATH

    SYSTEM_MODE = is_root() and not force_user_mode

    INSTALL_ROOT = ROOT_INSTALL_ROOT if SYSTEM_MODE else USER_INSTALL_ROOT
    INSTALL_BIN = INSTALL_ROOT / "adt-agent"
    DATA_DIR = ROOT_DATA_DIR if SYSTEM_MODE else USER_INSTALL_ROOT
    SYSTEMD_UNIT_PATH = ROOT_UNIT_PATH if SYSTEM_MODE else USER_UNIT_PATH


def _invoking_user() -> str | None:
    """The account that ran sudo, which is where a previous per-user install lives."""
    for key in ("SUDO_USER", "PKEXEC_UID"):
        value = (os.environ.get(key) or "").strip()
        if value and value != "root":
            return value
    return None


def _user_home(user: str) -> Path | None:
    try:
        import pwd

        return Path(pwd.getpwnam(user).pw_dir)
    except (ImportError, KeyError):
        return None


def _legacy_user_installs() -> list[tuple[Path, Path]]:
    """(unit path, data dir) for per-user installs this machine may still be running.

    Both the invoking user's home and root's own home are checked, since the agent may have been
    installed either before or after a switch to sudo.
    """
    homes: list[Path] = []
    invoking = _invoking_user()
    if invoking:
        home = _user_home(invoking)
        if home:
            homes.append(home)
    try:
        homes.append(Path.home())
    except RuntimeError:
        pass

    found: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for home in homes:
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        data = home / ".local" / "share" / "adt-agent"
        if unit in seen:
            continue
        seen.add(unit)
        if unit.exists() or data.exists():
            found.append((unit, data))
    return found


def migrate_user_installs_to_root() -> list[str]:
    """Retire per-user installs and carry their identity into the root install.

    The enrollment identity is what must survive: re-enrolling would be harmless but would lose
    the endpoint's job and metrics history, so config.json is moved before the old tree is removed.
    Returns a human-readable log of what was migrated.
    """
    if not SYSTEM_MODE:
        return []

    notes: list[str] = []
    invoking = _invoking_user()

    for unit, data in _legacy_user_installs():
        if unit.exists() and invoking:
            # `systemctl --user` needs the target user's session bus, so it is run as that user
            # with their runtime directory pointed at explicitly.
            uid = None
            try:
                import pwd

                uid = pwd.getpwnam(invoking).pw_uid
            except (ImportError, KeyError):
                uid = None
            if uid is not None and shutil.which("runuser"):
                env = _system_subprocess_env()
                env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
                for action in (["stop"], ["disable"]):
                    subprocess.run(
                        [
                            "runuser",
                            "-u",
                            invoking,
                            "--",
                            "systemctl",
                            "--user",
                            *action,
                            SYSTEMD_UNIT_NAME,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=45,
                        check=False,
                        env=env,
                    )

        if unit.exists():
            try:
                unit.unlink()
                notes.append(f"Removed per-user service: {unit}")
            except OSError as exc:
                notes.append(f"Could not remove {unit}: {exc}")

        legacy_config = data / "config.json"
        target_config = ROOT_DATA_DIR / "config.json"
        if legacy_config.is_file() and not target_config.is_file():
            try:
                ROOT_DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_config, target_config)
                os.chmod(target_config, 0o600)
                notes.append("Carried the existing enrollment identity into the root install.")
            except OSError as exc:
                notes.append(f"Could not migrate {legacy_config}: {exc}")

        if data.is_dir() and data != ROOT_DATA_DIR:
            try:
                shutil.rmtree(data)
                notes.append(f"Removed per-user install: {data}")
            except OSError as exc:
                notes.append(f"Could not remove {data}: {exc}")

    return notes


SoftwareRow = dict[str, str | None]


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    return values


def save_local_config(path: Path, config: dict[str, str]) -> None:
    write_config(dict(config), path)


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_running_from_install_dir() -> bool:
    if not is_frozen():
        return True
    try:
        return Path(sys.executable).resolve() == INSTALL_BIN.resolve()
    except Exception:
        return False


def find_env_file() -> Path:
    candidates: list[Path] = [
        INSTALL_ROOT / ".env",
        Path(sys.executable).resolve().parent / ".env",
        Path(".env"),
    ]
    for path in candidates:
        try:
            if path.exists():
                return path
        except Exception:
            continue
    return Path(".env")


def _system_subprocess_env() -> dict[str, str]:
    """Environment for invoking distro binaries from a PyInstaller onefile exe.

    PyInstaller prepends bundled libs (e.g. libcrypto.so.3) via LD_LIBRARY_PATH.
    That breaks systemctl and other tools that require the system OpenSSL (common on Kali).
    """
    env = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "LIBPATH", "DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"):
        env.pop(key, None)
    return env


def _systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    scope = [] if SYSTEM_MODE else ["--user"]
    return subprocess.run(
        ["systemctl", *scope, *args],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        env=_system_subprocess_env(),
    )


def stop_systemd_service() -> None:
    _systemctl(["stop", SYSTEMD_UNIT_NAME])


def disable_systemd_service() -> None:
    _systemctl(["disable", SYSTEMD_UNIT_NAME])


def write_systemd_unit() -> None:
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SYSTEM_MODE:
        unit = f"""[Unit]
Description=Vizhi ADT Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={INSTALL_BIN}
Restart=on-failure
RestartSec=60
StandardInput=null
StandardOutput=journal
StandardError=journal
SyslogIdentifier=adt-agent
WorkingDirectory={DATA_DIR}
Environment=HOME=/root

[Install]
WantedBy=multi-user.target
"""
        SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSTEMD_UNIT_PATH.write_text(unit, encoding="utf-8")
        _systemctl(["daemon-reload"])
        _systemctl(["enable", SYSTEMD_UNIT_NAME])
        return

    SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    unit = f"""[Unit]
Description=Vizhi ADT Agent (user)
After=network-online.target

[Service]
Type=simple
ExecStart={INSTALL_BIN}
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""
    SYSTEMD_UNIT_PATH.write_text(unit, encoding="utf-8")
    _systemctl(["daemon-reload"])
    _systemctl(["enable", SYSTEMD_UNIT_NAME])


def install_linux_agent() -> None:
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(sys.executable).resolve()
    dest = INSTALL_BIN.resolve()
    if src != dest:
        stop_existing_agent_processes()
        shutil.copy2(src, dest)
        os.chmod(dest, 0o750)
    write_systemd_unit()


def start_installed_agent() -> None:
    _systemctl(["start", SYSTEMD_UNIT_NAME])


def stop_existing_agent_processes() -> None:
    self_pid = os.getpid()
    install_bin = str(INSTALL_BIN)
    try:
        for proc in psutil.process_iter(["pid", "exe", "name"]):
            try:
                if proc.info["pid"] == self_pid:
                    continue
                exe = proc.info.get("exe") or ""
                if exe and Path(exe).resolve() == Path(install_bin).resolve():
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                continue
    except Exception:
        pass
    try:
        subprocess.run(
            ["pkill", "-f", install_bin],
            capture_output=True,
            timeout=15,
            check=False,
            env=_system_subprocess_env(),
        )
    except Exception:
        pass


def _run_query(cmd: list[str]) -> list[str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_system_subprocess_env(),
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    except Exception:
        return []


def _parse_desktop_file(path: Path) -> SoftwareRow | None:
    try:
        name: str | None = None
        hidden = False
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "Hidden=true":
                hidden = True
            elif line.startswith("Name=") and name is None:
                name = line.split("=", 1)[1].strip()
            elif line.startswith("X-Flatpak="):
                pass
        if hidden or not name:
            return None
        normalized = normalize_row(name, None, path.stem, package_type="gui")
        return {
            "name": name,
            "version": "unknown",
            "publisher": path.stem,
            "install_date": None,
            "package_type": "gui",
            "source": "desktop",
            "sources": ["desktop"],
            "evidence": {"desktop": {"desktop_file": str(path)}},
            "normalized_vendor": normalized.vendor,
            "normalized_product": normalized.product,
            "normalized_version": normalized.version,
            "match_confident": normalized.confident,
        }
    except OSError:
        return None


def _collect_gui_apps(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    roots = [
        Path("/usr/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
        Path.home() / ".local/share/applications",
        Path("/usr/local/share/applications"),
    ]
    rows: list[SoftwareRow] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for desktop in root.glob("*.desktop"):
                row = _parse_desktop_file(desktop)
                if not row:
                    continue
                key = str(row["name"]).lower()
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        except OSError as exc:
            if logger:
                logger.debug("GUI scan %s: %s", root, exc)
    return rows


def _package_row(
    *,
    name: str,
    version: str,
    publisher: str,
    package_type: str,
    evidence: dict[str, Any],
) -> SoftwareRow:
    """Build one inventory row with its canonical identity and per-source proof attached."""
    normalized = normalize_row(name, version, publisher, package_type=package_type)
    return {
        "name": name,
        "version": version,
        "publisher": publisher or "unknown",
        "install_date": None,
        "package_type": package_type,
        "source": package_type,
        "sources": [package_type],
        "evidence": {package_type: evidence},
        "normalized_vendor": normalized.vendor,
        "normalized_product": normalized.product,
        "normalized_version": normalized.version,
        "match_confident": normalized.confident,
    }


def _collect_deb_packages(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    rows: list[SoftwareRow] = []
    seen: set[str] = set()
    # ${source:Package} matters as much as the version: Debian and Ubuntu publish fixes against the
    # source package ("openssh"), while what is installed is a binary package ("openssh-server"),
    # and guessing the mapping server-side from the binary name is not reliable.
    query = (
        "-f=${Package}\t${Version}\t${Maintainer}\t${Architecture}\t"
        "${db:Status-Status}\t${source:Package}\n"
    )
    for line in _run_query(["dpkg-query", "-W", query]):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, version = parts[0], parts[1]
        publisher = parts[2] if len(parts) > 2 else "unknown"
        arch = parts[3] if len(parts) > 3 else ""
        status = parts[4] if len(parts) > 4 else ""
        source_package = parts[5].strip() if len(parts) > 5 else ""
        # Removed-but-configured packages have no files on disk and cannot be vulnerable.
        if status and status != "installed":
            continue
        if not is_plausible_software_row(
            name, version, publisher, package_type="deb", source="deb"
        ):
            continue
        key = f"{name.lower()}:{arch}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            _package_row(
                name=name,
                version=version,
                publisher=publisher,
                package_type="deb",
                evidence={
                    "package": name,
                    "arch": arch or None,
                    "status": status or None,
                    "source_package": source_package or None,
                },
            )
        )
    if logger:
        logger.debug("deb packages: %s", len(rows))
    return rows


def _srpm_name(sourcerpm: str | None) -> str | None:
    """Source package name out of an SRPM filename: "openssl-3.2.2-6.el9.src.rpm" -> "openssl".

    Red Hat advisories name the source package, so this is the key that joins an installed binary
    package to the advisory that fixes it.
    """
    value = (sourcerpm or "").strip()
    if not value or value == "(none)":
        return None
    stem = re.sub(r"\.src\.rpm$", "", value)
    # Strip the trailing -version-release, which is always the last two dash-separated fields.
    parts = stem.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else stem or None


def _collect_rpm_packages(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    rows: list[SoftwareRow] = []
    seen: set[str] = set()
    # Red Hat advisories are published against the full epoch:version-release, so %{VERSION}
    # alone cannot answer whether a fix is installed.
    query = (
        "%{NAME}\t%|EPOCH?{%{EPOCH}:}|%{VERSION}-%{RELEASE}\t%{VENDOR}\t%{ARCH}\t%{SOURCERPM}\n"
    )
    for line in _run_query(["rpm", "-qa", "--queryformat", query]):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, version = parts[0], parts[1]
        publisher = parts[2] if len(parts) > 2 else "unknown"
        arch = parts[3] if len(parts) > 3 else ""
        source_package = _srpm_name(parts[4]) if len(parts) > 4 else None
        if not is_plausible_software_row(
            name, version, publisher, package_type="rpm", source="rpm"
        ):
            continue
        key = f"{name.lower()}:{arch}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            _package_row(
                name=name,
                version=version,
                publisher=publisher,
                package_type="rpm",
                evidence={
                    "package": name,
                    "arch": arch or None,
                    "source_package": source_package,
                },
            )
        )
    if logger:
        logger.debug("rpm packages: %s", len(rows))
    return rows


def _collect_snap_packages(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    rows: list[SoftwareRow] = []
    seen: set[str] = set()
    lines = _run_query(["snap", "list"])
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        name, version = parts[0], parts[1]
        if name == "Name" or version == "Version":
            continue
        revision = parts[2] if len(parts) > 2 else ""
        if not is_plausible_software_row(
            name, version, "snap", package_type="snap", source="snap"
        ):
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        rows.append(
            _package_row(
                name=name,
                version=version,
                publisher="snap",
                package_type="snap",
                evidence={"snap": name, "revision": revision or None},
            )
        )
    if logger:
        logger.debug("snap packages: %s", len(rows))
    return rows


def _collect_flatpak_packages(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    rows: list[SoftwareRow] = []
    seen: set[str] = set()
    for line in _run_query(["flatpak", "list", "--columns=application,version", "--plain"]):
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        name, version = parts[0].strip(), parts[1].strip()
        if not is_plausible_software_row(
            name, version, "flatpak", package_type="flatpak", source="flatpak"
        ):
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        rows.append(
            _package_row(
                name=name,
                version=version,
                publisher="flatpak",
                package_type="flatpak",
                evidence={"application_id": name},
            )
        )
    if logger:
        logger.debug("flatpak packages: %s", len(rows))
    return rows


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key:
                    values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def collect_os_facts() -> dict[str, Any]:
    """Distro identity, package manager, and kernel — what advisories and can_patch key on."""
    return linux_distro_facts()


def collect_installed_software(
    logger: logging.Logger | None = None,
) -> tuple[list[SoftwareRow], dict[str, Any]]:
    """Collect packages from every available manager, plus the distro facts advisories key on."""
    all_rows: list[SoftwareRow] = []
    seen: set[tuple[str, str, str, str]] = set()

    def merge(rows: list[SoftwareRow]) -> None:
        for row in rows:
            name = str(row.get("name") or "").strip()
            version = str(row.get("version") or "unknown").strip()
            publisher = str(row.get("publisher") or "unknown").strip()
            package_type = str(row.get("package_type") or "other")
            key = (name.lower(), version, publisher.lower(), package_type)
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(row)

    merge(_collect_gui_apps(logger))
    if shutil.which("dpkg-query"):
        merge(_collect_deb_packages(logger))
    if shutil.which("rpm"):
        merge(_collect_rpm_packages(logger))
    if shutil.which("snap"):
        merge(_collect_snap_packages(logger))
    if shutil.which("flatpak"):
        merge(_collect_flatpak_packages(logger))

    filtered = filter_software_rows(all_rows)
    filtered.sort(key=lambda x: (str(x.get("package_type", "")), str(x.get("name", "")).lower()))
    os_facts = collect_os_facts()
    if logger:
        logger.info(
            "Software inventory: %s rows on %s %s (gui/deb/rpm/snap/flatpak segregated)",
            len(filtered),
            os_facts.get("distro") or "linux",
            os_facts.get("distro_release") or "",
        )
    return filtered, os_facts


def get_cpu_model_name() -> str | None:
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip() or None
    except Exception:
        pass
    return platform.processor() or None


def get_cpu_temperature() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for entries in temps.values():
            for entry in entries:
                if entry.current is not None:
                    return float(entry.current)
    except Exception:
        pass
    return None


def get_top_process_info() -> tuple[str | None, float | None]:
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.1)
        top_name: str | None = None
        top_pct = 0.0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                cpu_pct = proc.cpu_percent(interval=None)
                if cpu_pct > top_pct:
                    top_pct = cpu_pct
                    top_name = proc.info.get("name", "unknown")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return (top_name, top_pct if top_pct > 0 else None)
    except Exception:
        return (None, None)


def get_cpu_uptime_seconds() -> int | None:
    try:
        return int(time.time() - psutil.boot_time())
    except Exception:
        return None


def get_gpu_metrics() -> dict[str, object]:
    gpu: dict[str, object] = {
        "temp_celsius": None,
        "model": None,
        "utilization_percent": None,
    }
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_system_subprocess_env(),
        )
        line = (result.stdout or "").strip().splitlines()[0].strip() if result.stdout else ""
        if line:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 1:
                gpu["model"] = parts[0] or None
            if len(parts) >= 2:
                try:
                    gpu["temp_celsius"] = float(parts[1])
                except ValueError:
                    pass
            if len(parts) >= 3:
                try:
                    gpu["utilization_percent"] = float(parts[2])
                except ValueError:
                    pass
    except Exception:
        pass
    return gpu


def get_os_info() -> dict[str, str | None]:
    info: dict[str, str | None] = {
        "system": "Linux",
        "release": None,
        "version": None,
        "platform": None,
        "machine": None,
        "edition": None,
        "build": None,
    }
    try:
        info["release"] = platform.release() or None
        info["machine"] = platform.machine() or None
        info["platform"] = platform.platform() or None
    except Exception:
        pass
    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
        values: dict[str, str] = {}
        for line in os_release.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
        info["edition"] = values.get("PRETTY_NAME") or values.get("NAME")
        info["version"] = values.get("VERSION_ID")
        info["build"] = values.get("VERSION")
    except Exception:
        pass
    return info


def get_network_info() -> dict[str, object]:
    info: dict[str, object] = {
        "hostname": None,
        "local_ip": None,
        "public_ip": None,
        "interfaces": [],
    }
    try:
        info["hostname"] = socket.gethostname()
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            info["local_ip"] = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass
    try:
        af_link = getattr(psutil, "AF_LINK", None)
        interfaces: list[dict[str, object]] = []
        for name, addrs in psutil.net_if_addrs().items():
            ipv4: list[str] = []
            ipv6: list[str] = []
            mac: str | None = None
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address:
                    ipv4.append(addr.address)
                elif addr.family == socket.AF_INET6 and addr.address:
                    ipv6.append(addr.address.split("%", 1)[0])
                elif af_link is not None and addr.family == af_link and addr.address:
                    mac = addr.address
            if ipv4 or ipv6 or mac:
                interfaces.append({"name": name, "ipv4": ipv4, "ipv6": ipv6, "mac": mac})
        info["interfaces"] = interfaces
    except Exception:
        pass
    try:
        resp = requests.get("https://api.ipify.org?format=json", timeout=5)
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict) and data.get("ip"):
                info["public_ip"] = str(data["ip"])
    except Exception:
        pass
    return info


def get_storage_info() -> dict[str, object]:
    drives: list[dict[str, object]] = []
    seen: set[str] = set()
    try:
        partitions = psutil.disk_partitions(all=False) or psutil.disk_partitions(all=True)
    except Exception:
        partitions = []

    for part in partitions:
        mount = part.mountpoint
        if not mount or mount in seen:
            continue
        opts = (part.opts or "").lower()
        if "cdrom" in opts or part.fstype == "":
            continue
        try:
            usage = psutil.disk_usage(mount)
        except (OSError, PermissionError):
            continue
        seen.add(mount)
        drives.append(
            {
                "device": part.device,
                "mountpoint": mount,
                "fstype": part.fstype,
                "opts": part.opts,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
                "total_gib": round(usage.total / (1024**3), 2),
                "used_gib": round(usage.used / (1024**3), 2),
                "free_gib": round(usage.free / (1024**3), 2),
            }
        )

    total_all = sum(int(d["total"]) for d in drives)
    used_all = sum(int(d["used"]) for d in drives)
    free_all = sum(int(d["free"]) for d in drives)
    percent_all = round((used_all / total_all) * 100, 1) if total_all else 0.0
    try:
        sys_usage = psutil.disk_usage("/")
    except (OSError, PermissionError):
        sys_usage = psutil.disk_usage("/")

    return {
        "drive": "/",
        "total": sys_usage.total,
        "used": sys_usage.used,
        "free": sys_usage.free,
        "percent": sys_usage.percent,
        "drives": drives,
        "total_all": total_all,
        "used_all": used_all,
        "free_all": free_all,
        "percent_all": percent_all,
    }


def collect_metrics(software: list[SoftwareRow]) -> dict[str, object]:
    cpu_percent = psutil.cpu_percent(interval=1.0)
    cpu_count_logical = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)
    cpu_freq = psutil.cpu_freq()
    cpu_model = get_cpu_model_name()
    cpu_temp = get_cpu_temperature()
    cpu_uptime = get_cpu_uptime_seconds()
    top_process_name, top_process_cpu = get_top_process_info()
    gpu = get_gpu_metrics()
    vm = psutil.virtual_memory()
    storage = get_storage_info()
    os_info = get_os_info()
    network = get_network_info()
    batt = psutil.sensors_battery()

    if top_process_cpu is not None and cpu_count_logical:
        top_norm: float | None = round(top_process_cpu / cpu_count_logical, 1)
    else:
        top_norm = None

    min_clock_mhz = cpu_freq.min if cpu_freq and cpu_freq.min else None
    max_clock_mhz = cpu_freq.max if cpu_freq and cpu_freq.max else None
    current_mhz = cpu_freq.current if cpu_freq else None

    return {
        "agent_variant": "linux",
        "hostname": socket.gethostname(),
        "username": system_username(),
        "agent_version": AGENT_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_count_logical,
            "physical_cores": cpu_count_physical,
            "model": cpu_model,
            "base_clock_mhz": current_mhz,
            "advertised_clock_mhz": max_clock_mhz,
            "min_clock_mhz": min_clock_mhz,
            "max_clock_mhz": max_clock_mhz,
            "current_clock_mhz": current_mhz,
            "temp_celsius": cpu_temp,
            "top_process_name": top_process_name,
            "top_process_cpu_percent": top_process_cpu,
            "top_process_cpu_percent_normalized": top_norm,
            "uptime_seconds": cpu_uptime,
        },
        "gpu": gpu,
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "percent": vm.percent,
            "total_gib": round(vm.total / (1024**3), 2),
            "total_gb": round(vm.total / (1000**3), 1),
            "used_gib": round(vm.used / (1024**3), 2),
        },
        "storage": storage,
        "os": os_info,
        "network": network,
        "battery": {
            "percent": batt.percent if batt else None,
            "plugged": batt.power_plugged if batt else None,
        },
        "software_count": len(software),
        "installed_software": software,
    }


def execute_shell_command(command: str, timeout_seconds: int) -> str:
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=_system_subprocess_env(),
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        result = f"exit_code={proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    except subprocess.TimeoutExpired:
        result = f"exit_code=timeout\nstdout:\n\nstderr:\nCommand timed out after {timeout_seconds}s"
    if len(result) > 12000:
        return result[:12000] + "\n...[truncated]"
    return result


def process_pending_commands(
    timeout_seconds: int,
    session: DeviceSession,
) -> bool:
    api_base = session.api_base
    device_token = session.device_token
    pending = fetch_agent_commands(api_base, device_token, session=session)
    if not pending:
        return False

    logging.info("Found %s pending command(s)", len(pending))
    for row in pending:
        command_id = str(row.get("id", ""))
        command_text = str(row.get("command", "")).strip()
        if not command_id:
            continue
        if command_text == AGENT_STOP_COMMAND:
            logging.info("Remote stop command received id=%s", command_id)
            result_text = (
                "exit_code=0\nstdout:\nAgent stopped remotely. systemd service disabled.\nstderr:\n"
            )
            try:
                stop_systemd_service()
                disable_systemd_service()
            except Exception as exc:
                result_text = f"exit_code=1\nstdout:\n\nstderr:\nFailed to stop agent: {exc}\n"
            report_command_result(api_base, device_token, command_id, result_text, session=session)
            logging.info("Remote stop acknowledged; exiting")
            return True
        if not command_text:
            report_command_result(
                api_base, device_token, command_id, "exit_code=invalid\nstderr:\nEmpty command", session=session
            )
            continue
        logging.info("Executing command id=%s", command_id)
        result_text = execute_shell_command(command_text, timeout_seconds)
        report_command_result(api_base, device_token, command_id, result_text, session=session)
        logging.info("Saved result for command id=%s", command_id)
    return False


def setup_logging(log_file: Path, verbose: bool, console_log: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if console_log:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def resolve_data_dir() -> Path:
    candidates = [DATA_DIR, USER_INSTALL_ROOT, Path.home() / ".adt-agent"]
    last: Exception | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / "agent.log"
            with open(probe, "a", encoding="utf-8"):
                pass
            return candidate
        except (OSError, PermissionError) as exc:
            last = exc
            continue
    raise PermissionError(f"No writable data directory (last error: {last})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vizhi agent (Linux)")
    parser.add_argument("--clear", action="store_true", help="Clear stored credentials and exit")
    parser.add_argument(
        "--code",
        default="",
        help="Organisation enrollment code (VZ-XXXX-XXXX-XXXX). Optional if you type it when prompted.",
    )
    parser.add_argument(
        "--api",
        default="",
        help="Vizhi portal URL for enrollment (defaults to the URL baked into this build).",
    )
    args = parser.parse_args()

    configure_install_mode()

    bootstrap = is_frozen() and not is_running_from_install_dir()
    if bootstrap:
        if not is_root():
            print(
                "Run with sudo: sudo ./vizhi-agent-linux --code VZ-XXXX-XXXX-XXXX --api <portal-url>",
                file=sys.stderr,
            )
            raise SystemExit(1)
        configure_install_mode()
        try:
            install_linux_agent()
        except Exception as exc:
            print(f"Install failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print("Agent installed to /opt/vizhi-agent")

    data_dir = resolve_data_dir()
    (data_dir / "update").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    log_file = data_dir / "agent.log"
    config_file = data_dir / "config.json"

    if args.clear:
        if config_file.exists():
            config_file.unlink()
            print(f"Cleared device identity from {config_file}")
            print("Run the agent again with sudo to enroll.")
        else:
            print(f"No device identity at {config_file}")
        return

    env = load_env(find_env_file())
    local_config = load_local_config(config_file)
    api_base = resolve_api_base(local_config, env, override=args.api)

    if needs_enrollment(local_config):
        enrolled = prompt_and_enroll(
            api_base, config_file, AGENT_VERSION, code=args.code or None, local_config=local_config
        )
        local_config = {str(k): str(v) for k, v in enrolled.items()}

    if bootstrap:
        print("Starting the adt-agent systemd service...")
        start_installed_agent()
        print("Done. This machine is now connected to Vizhi.")
        raise SystemExit(0)

    device_token = local_config.get("DEVICE_TOKEN", "")
    endpoint_id = local_config.get("ENDPOINT_ID", "")
    endpoint_role = (local_config.get("ROLE") or "").strip() or None

    intervals = resolve_intervals(local_config, env)
    command_timeout_seconds = int(
        os.getenv(
            "COMMAND_TIMEOUT_SECONDS",
            env.get("COMMAND_TIMEOUT_SECONDS", "60"),
        )
    )
    verbose = is_truthy(os.getenv("VERBOSE", env.get("VERBOSE", "0")))
    console_log = is_truthy(os.getenv("CONSOLE_LOG", env.get("CONSOLE_LOG", "1")))

    setup_logging(log_file, verbose=verbose, console_log=console_log)
    if is_frozen() and is_running_from_install_dir():
        cleanup_previous_backup(INSTALL_BIN)

    if not endpoint_id or not is_valid_uuid(endpoint_id):
        raise ValueError(f"Invalid or missing ENDPOINT_ID in {config_file}")
    if not device_token:
        raise ValueError(f"Missing DEVICE_TOKEN in {config_file}")

    session = DeviceSession(api_base, config_file, AGENT_VERSION, local_config)

    last_runs = load_last_runs(local_config)
    logging.info(
        "Agent %s started (Linux, %s) as %s. Endpoint %s...",
        AGENT_VERSION,
        "root system unit" if SYSTEM_MODE else "user session",
        system_hostname(),
        session.endpoint_id[:8],
    )

    cycle = 0
    software_snapshot: list[SoftwareRow] = []
    os_facts: dict[str, Any] = {}
    next_self_update = next_check_deadline(intervals["auto_update"], initial=True)

    start_command_poller(
        lambda: process_pending_commands(
            command_timeout_seconds,
            session,
        ),
        intervals["command_poll"],
    )
    start_patch_poller(api_base, session.device_token, intervals["patch_poll"], endpoint_role, session=session)

    try:
        while True:
            now_ts = time.time()
            local_config = load_local_config(config_file)
            last_runs = load_last_runs(local_config)
            session.reload()

            if now_ts >= next_self_update:
                next_self_update = next_check_deadline(intervals["auto_update"])
                if read_auto_update_flag(local_config, env) and should_self_update(
                    frozen=is_frozen(), install_exe=INSTALL_BIN
                ):
                    try:
                        if maybe_apply_update(
                            api_base=api_base,
                            device_token=session.device_token,
                            data_dir=data_dir,
                            install_exe=INSTALL_BIN,
                        ):
                            logging.info(
                                "Agent binary replaced; exiting so systemd restarts"
                            )
                            raise SystemExit(0)
                    except SystemExit:
                        raise
                    except Exception:
                        logging.exception("Self-update check failed")

            if should_run("inventory", last_runs, intervals["inventory"], now_ts):
                try:
                    software_snapshot, os_facts = collect_installed_software(
                        logger=logging.getLogger()
                    )
                    logging.info("Software snapshot: %s entries", len(software_snapshot))
                    payload = collect_metrics(software_snapshot)
                    report_telemetry(
                        api_base,
                        session.device_token,
                        metrics=payload,
                        inventory=software_snapshot,
                        session=session,
                    )
                    local_config = mark_run(
                        config_file, local_config, "inventory", now_ts
                    )
                    local_config = mark_run(
                        config_file, local_config, "metrics", now_ts
                    )
                    last_runs = load_last_runs(local_config)
                except Exception as exc:
                    logging.exception("Inventory refresh failed: %s", exc)

            if should_run("metrics", last_runs, intervals["metrics"], now_ts):
                cycle += 1
                started = time.time()
                try:
                    payload = collect_metrics(software_snapshot)
                    if report_telemetry(api_base, session.device_token, metrics=payload, session=session):
                        logging.info(
                            "Cycle %s reported in %.2fs",
                            cycle,
                            time.time() - started,
                        )
                    else:
                        logging.warning("Cycle %s telemetry was not accepted", cycle)
                    local_config = mark_run(
                        config_file, local_config, "metrics", now_ts
                    )
                    last_runs = load_last_runs(local_config)
                except Exception as exc:
                    logging.exception("Metric cycle %s failed: %s", cycle, exc)

            if should_run("update_scan", last_runs, intervals["update_scan"], now_ts):
                try:
                    logging.info("Starting update scan (may take several minutes)")
                    scan = scan_updates(
                        api_base=api_base,
                        device_token=session.device_token,
                        endpoint_role=endpoint_role,
                    )
                    report_updates(
                        api_base,
                        session.device_token,
                        scan=scan,
                        os_facts=os_facts,
                        agent_version=AGENT_VERSION,
                        platform_tag=platform_tag(),
                        session=session,
                    )
                    local_config = mark_run(
                        config_file, local_config, "update_scan", now_ts
                    )
                    last_runs = load_last_runs(local_config)
                except Exception as exc:
                    logging.exception("Update scan failed: %s", exc)

            time.sleep(intervals["command_poll"])
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Fatal: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
