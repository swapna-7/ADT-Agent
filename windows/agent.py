import argparse
import json
import logging
import os
import platform
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
from normalize import normalize_row
from command_poller import start_command_poller
from config_io import load_config, write_config
from device_session import DeviceSession
from enrollment import platform_tag, system_hostname, system_username
from first_run import needs_enrollment, prompt_and_enroll
from inventory_windows import collect_windows_inventory
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
from updates import scan_updates
from version import AGENT_VERSION


REG_PATHS = [
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
]


INSTALL_DIR = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ADT Agent"
INSTALL_EXE_PATH = INSTALL_DIR / "adt-agent.exe"
DATA_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ADT Agent"
TASK_NAME = "ADTAgent"

AGENT_STOP_COMMAND = "__ADT_AGENT_STOP__"


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


def load_local_config(path: Path) -> dict[str, str]:
    data = load_config(path)
    return {str(k): str(v) for k, v in data.items()}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def is_running_from_install_dir() -> bool:
    if not is_frozen():
        return True
    try:
        return Path(sys.executable).resolve().parent == INSTALL_DIR.resolve()
    except Exception:
        return False


def find_env_file() -> Path:
    candidates: list[Path] = [
        INSTALL_DIR / ".env",
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


def stop_scheduled_task() -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], check=False)


def disable_scheduled_task() -> None:
    subprocess.run(["schtasks", "/Change", "/TN", TASK_NAME, "/DISABLE"], check=False)


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def stop_other_agent_processes() -> None:
    stop_scheduled_task()
    self_pid = os.getpid()
    try:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if proc.info["pid"] == self_pid:
                    continue
                exe = proc.info.get("exe") or ""
                name = (proc.info.get("name") or "").lower()
                if name == "adt-agent.exe" or (
                    exe and Path(exe).resolve() == INSTALL_EXE_PATH.resolve()
                ):
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                continue
    except Exception:
        pass


def register_scheduled_task() -> None:
    exe = str(INSTALL_EXE_PATH)
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{exe}'
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -RunOnlyIfNetworkAvailable
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
"""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Failed to register scheduled task").strip()
        raise RuntimeError(detail)


def install_windows_agent() -> None:
    """Copy this frozen binary into Program Files and register the boot task."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(sys.executable).resolve()
    dest = INSTALL_EXE_PATH.resolve()
    if src != dest:
        stop_other_agent_processes()
        time.sleep(1)
        last_error: OSError | None = None
        for _ in range(8):
            try:
                shutil.copy2(src, dest)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(1)
        if last_error:
            raise RuntimeError(f"Could not copy agent to {dest}: {last_error}")
    register_scheduled_task()


def start_installed_agent() -> None:
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)


def _safe_reg_read(key: Any, name: str) -> str | None:
    try:
        value, _ = key.QueryValueEx(name)
        text = str(value).strip()
        return text if text else None
    except Exception:
        return None


def normalize_install_date(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    formats = ["%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _collect_software_from_powershell(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    try:
        ps_script = """
$apps = @()
$paths = @(
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
    "HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
)
foreach ($path in $paths) {
    if (Test-Path $path) {
        Get-ChildItem "$path" -ErrorAction SilentlyContinue | ForEach-Object {
            $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if (-not $props.DisplayName) { return }
            $displayName = $props.DisplayName.Trim()
            if ($displayName.Length -lt 2) { return }
            if ($props.SystemComponent -eq 1) { return }
            if ($props.ParentKeyName) { return }
            if ($displayName -match '^\\{?[0-9a-fA-F]{8}-') { return }
            if ($displayName -match '^(KB\\d+|Update for|Security Update|Hotfix for|Service Pack|Definition Update)') { return }
            $version = $props.DisplayVersion
            if (-not $version) { $version = "unknown" }
            $publisher = $props.Publisher
            if (-not $publisher) { $publisher = "unknown" }
            $installDate = $props.InstallDate
            $apps += @{
                Name = $displayName
                Version = $version
                Publisher = $publisher
                InstallDate = $installDate
            } | ConvertTo-Json -Compress
        }
    }
}
if ($apps.Count -gt 0) {
    "[" + ($apps -join ",") + "]"
} else {
    "[]"
}
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            if logger:
                logger.debug(f"PowerShell query failed: {result.stderr}")
            return []
        rows: list[SoftwareRow] = []
        seen: set[tuple[str, str, str]] = set()
        try:
            output = result.stdout.strip()
            if output:
                data = json.loads(output)
                if not isinstance(data, list):
                    data = [data] if data else []
                for item in data:
                    if isinstance(item, dict) and item.get("Name"):
                        name = str(item["Name"]).strip()
                        version = str(item.get("Version", "unknown")).strip()
                        publisher = str(item.get("Publisher", "unknown")).strip() or "unknown"
                        install_date = normalize_install_date(str(item.get("InstallDate", "")))
                        if not is_plausible_software_row(
                            name, version, publisher, package_type="registry", source="registry"
                        ):
                            continue
                        key = (name.lower(), version, publisher.lower())
                        if key not in seen:
                            seen.add(key)
                            rows.append(
                                {
                                    "name": name,
                                    "version": version,
                                    "publisher": publisher,
                                    "install_date": install_date,
                                    "package_type": "registry",
                                    "source": "registry",
                                }
                            )
        except json.JSONDecodeError:
            if logger:
                logger.debug(f"Failed to parse PowerShell JSON output: {result.stdout}")
        if logger and rows:
            logger.debug(f"Found {len(rows)} apps via PowerShell registry")
        return rows
    except Exception as e:
        if logger:
            logger.debug(f"PowerShell collection failed: {e}")
        return []



def collect_installed_software(
    logger: logging.Logger | None = None,
    *,
    api_base: str | None = None,
    device_token: str | None = None,
) -> tuple[list[SoftwareRow], dict[str, Any]]:
    """Collect installed software plus the OS facts needed for advisory matching.

    Returns (rows, os_facts). os_facts carries build, UBR and installed KBs, which is what lets
    the server decide whether a Windows advisory applies to this exact build rather than guessing
    from a marketing version string.
    """
    os_facts: dict[str, Any] = {}
    rows: list[SoftwareRow] = []

    try:
        rows, os_facts = collect_windows_inventory()
    except Exception as exc:
        if logger:
            logger.warning("Multi-source inventory failed, falling back to registry: %s", exc)

    if not rows:
        # Losing inventory entirely is worse than losing the extra sources, so the original
        # registry-only path stays as a fallback.
        try:
            rows = _collect_software_from_powershell(logger)
        except Exception as exc:
            if logger:
                logger.debug("Registry fallback failed: %s", exc)

    plausible: list[SoftwareRow] = []
    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        version = str(row.get("version", "unknown")).strip() or "unknown"
        publisher = str(row.get("publisher", "unknown")).strip() or "unknown"
        package_type = str(row.get("package_type") or "registry")
        source = str(row.get("source") or "registry")
        if not is_plausible_software_row(
            name, version, publisher, package_type=package_type, source=source
        ):
            continue
        derived = normalize_row(name, version, publisher, package_type=package_type)
        plausible.append(
            {
                "name": name,
                "version": version,
                "publisher": publisher,
                "install_date": normalize_install_date(str(row.get("install_date") or "")),
                "package_type": package_type,
                "source": source,
                "sources": row.get("sources") or [source],
                "evidence": row.get("evidence") or {},
                "normalized_vendor": row.get("normalized_vendor") or derived.vendor,
                "normalized_product": row.get("normalized_product") or derived.product,
                "normalized_version": row.get("normalized_version") or derived.version,
                "match_confident": bool(row.get("match_confident") or derived.confident),
            }
        )

    if not plausible and logger:
        logger.warning(
            "No software collected from any source. "
            "Check admin rights or uninstall-key permissions."
        )

    plausible = filter_software_rows(plausible)

    if api_base and device_token:
        try:
            from chocolatey import enrich_inventory_snapshot

            plausible = enrich_inventory_snapshot(plausible, api_base, device_token)
        except Exception as exc:
            if logger:
                logger.warning("Chocolatey inventory enrichment failed: %s", exc)

    plausible.sort(key=lambda x: str(x.get("name", "")).lower())
    if logger:
        logger.debug("Total software found: %s entries", len(plausible))
    return plausible, os_facts


def get_cpu_model_name() -> str | None:
    """Get CPU model name from Windows registry or fallback."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        model = str(value).strip()
        return model if model else None
    except Exception:
        return None


def get_cpu_advertised_mhz() -> int | None:
    """Read the advertised base clock (MHz) from the CPU registry entry.

    psutil.cpu_freq().min/max return 0 on Windows for most CPUs, so this
    pulls the value the firmware reports to Windows under
    HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0\\~MHz.
    """
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        value, _ = winreg.QueryValueEx(key, "~MHz")
        winreg.CloseKey(key)
        mhz = int(value)
        return mhz if mhz > 0 else None
    except Exception:
        return None


def get_cpu_temperature() -> float | None:
    """Get CPU temperature in Celsius if available.

    psutil.sensors_temperatures() returns {} on Windows (no built-in support);
    this still works on Linux and on Windows hosts that expose temps via a
    shim. Iterates every entry per group rather than just entries[0].
    """
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for name, entries in temps.items():
            for entry in entries:
                label = entry.label or ""
                if "Core" in label or "Package" in label or "CPU" in name:
                    if entry.current is not None:
                        return float(entry.current)
        return None
    except Exception:
        return None


def get_top_process_info() -> tuple[str | None, float | None]:
    """Get the process name and CPU percentage of the process consuming most CPU."""
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        time.sleep(0.1)

        top_process = None
        top_percent = 0.0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                cpu_pct = proc.cpu_percent(interval=None)
                if cpu_pct > top_percent:
                    top_percent = cpu_pct
                    top_process = proc.info.get("name", "unknown")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return (top_process, top_percent if top_percent > 0 else None)
    except Exception:
        return (None, None)


def get_cpu_uptime_seconds() -> int | None:
    """Get total CPU uptime in seconds since last reboot."""
    try:
        boot_time = psutil.boot_time()
        uptime_seconds = int(time.time() - boot_time)
        return uptime_seconds
    except Exception:
        return None


def get_gpu_metrics() -> dict[str, object]:
    """Get GPU details using NVIDIA tools when available."""
    gpu = {
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
            check=True,
        )
        line = (result.stdout or "").strip().splitlines()[0].strip()
        if line:
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 1:
                gpu["model"] = parts[0] or None
            if len(parts) >= 2:
                try:
                    gpu["temp_celsius"] = float(parts[1])
                except ValueError:
                    gpu["temp_celsius"] = None
            if len(parts) >= 3:
                try:
                    gpu["utilization_percent"] = float(parts[2])
                except ValueError:
                    gpu["utilization_percent"] = None
    except Exception:
        return gpu

    return gpu


def get_os_info() -> dict[str, str | None]:
    """Get OS version, build, architecture details."""
    info: dict[str, str | None] = {
        "system": None,
        "release": None,
        "version": None,
        "platform": None,
        "machine": None,
        "edition": None,
        "build": None,
    }
    try:
        info["system"] = platform.system() or None
        info["release"] = platform.release() or None
        info["version"] = platform.version() or None
        info["platform"] = platform.platform() or None
        info["machine"] = platform.machine() or None
    except Exception:
        pass
    try:
        win_ver = sys.getwindowsversion()
        info["build"] = str(win_ver.build)
    except Exception:
        pass
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        )
        try:
            product_name, _ = winreg.QueryValueEx(key, "ProductName")
            info["edition"] = str(product_name).strip() or None
        except OSError:
            pass
        try:
            display_version, _ = winreg.QueryValueEx(key, "DisplayVersion")
            if display_version:
                info["version"] = str(display_version).strip()
        except OSError:
            pass
        try:
            ubr, _ = winreg.QueryValueEx(key, "UBR")
            if info["build"]:
                info["build"] = f"{info['build']}.{ubr}"
        except OSError:
            pass
        winreg.CloseKey(key)
    except Exception:
        pass
    return info


def get_network_info() -> dict[str, object]:
    """Get hostname, local IP, public IP and per-interface addresses."""
    info: dict[str, object] = {
        "hostname": None,
        "local_ip": None,
        "public_ip": None,
        "interfaces": [],
    }
    try:
        info["hostname"] = socket.gethostname()
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            info["local_ip"] = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
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
                interfaces.append(
                    {
                        "name": name,
                        "ipv4": ipv4,
                        "ipv6": ipv6,
                        "mac": mac,
                    }
                )
        info["interfaces"] = interfaces
    except Exception:
        pass

    try:
        resp = requests.get("https://api.ipify.org?format=json", timeout=5)
        if resp.ok:
            data = resp.json()
            ip_value = data.get("ip") if isinstance(data, dict) else None
            if ip_value:
                info["public_ip"] = str(ip_value)
    except Exception:
        pass

    return info


def get_storage_info() -> dict[str, object]:
    """Detect all mounted drives and return per-drive plus aggregate usage.

    Backward compatible: keeps top-level total/used/percent/drive (system drive)
    while also exposing a `drives` list and aggregate `*_all` totals.
    """
    drives: list[dict[str, object]] = []
    seen_mounts: set[str] = set()

    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        partitions = []

    if not partitions:
        try:
            partitions = psutil.disk_partitions(all=True)
        except Exception:
            partitions = []

    for part in partitions:
        mount = part.mountpoint
        if not mount or mount in seen_mounts:
            continue
        opts = (part.opts or "").lower()
        if "cdrom" in opts or part.fstype == "":
            continue
        try:
            usage = psutil.disk_usage(mount)
        except (PermissionError, OSError):
            continue
        seen_mounts.add(mount)
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
                "total_gib": round(usage.total / (1024 ** 3), 2),
                "used_gib": round(usage.used / (1024 ** 3), 2),
                "free_gib": round(usage.free / (1024 ** 3), 2),
            }
        )

    total_all = sum(int(d["total"]) for d in drives)
    used_all = sum(int(d["used"]) for d in drives)
    free_all = sum(int(d["free"]) for d in drives)
    percent_all = round((used_all / total_all) * 100, 1) if total_all else 0.0

    system_drive = os.environ.get("SystemDrive", "C:") + "\\"
    try:
        sys_usage = psutil.disk_usage(system_drive)
    except Exception:
        sys_usage = psutil.disk_usage("C:\\")

    return {
        "drive": system_drive,
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
    advertised_clock_mhz = get_cpu_advertised_mhz()
    min_clock_mhz = cpu_freq.min if cpu_freq and cpu_freq.min else None
    max_clock_mhz = cpu_freq.max if cpu_freq and cpu_freq.max else None
    gpu = get_gpu_metrics()
    vm = psutil.virtual_memory()
    storage = get_storage_info()
    os_info = get_os_info()
    network = get_network_info()
    batt = psutil.sensors_battery()

    if top_process_cpu is not None and cpu_count_logical:
        top_process_cpu_normalized: float | None = round(
            top_process_cpu / cpu_count_logical, 1
        )
    else:
        top_process_cpu_normalized = None

    return {
        "hostname": socket.gethostname(),
        "username": system_username(),
        "agent_version": AGENT_VERSION,
        "agent_variant": "windows",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_count_logical,
            "physical_cores": cpu_count_physical,
            "model": cpu_model,
            "base_clock_mhz": advertised_clock_mhz,
            "advertised_clock_mhz": advertised_clock_mhz,
            "min_clock_mhz": min_clock_mhz,
            "max_clock_mhz": max_clock_mhz,
            "current_clock_mhz": cpu_freq.current if cpu_freq else None,
            "temp_celsius": cpu_temp,
            "top_process_name": top_process_name,
            "top_process_cpu_percent": top_process_cpu,
            "top_process_cpu_percent_normalized": top_process_cpu_normalized,
            "uptime_seconds": cpu_uptime,
        },
        "gpu": gpu,
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "percent": vm.percent,
            "total_gib": round(vm.total / (1024 ** 3), 2),
            "total_gb": round(vm.total / (1000 ** 3), 1),
            "used_gib": round(vm.used / (1024 ** 3), 2),
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


def execute_powershell_command(command: str, timeout_seconds: int) -> str:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
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
                "exit_code=0\nstdout:\nAgent stopped remotely. Scheduled task disabled.\nstderr:\n"
            )
            try:
                disable_scheduled_task()
                stop_scheduled_task()
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
        result_text = execute_powershell_command(command_text, timeout_seconds)
        report_command_result(api_base, device_token, command_id, result_text, session=session)
        logging.info("Saved result for command id=%s", command_id)
    return False


def setup_logging(log_file: Path, verbose: bool, console_log: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if console_log:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def resolve_data_dir() -> Path:
    """Pick a data directory we can actually create + write the log file in.

    Preferred: C:\\ProgramData\\ADT Agent (used by SYSTEM/admin runs).
    Fallbacks for non-admin/dev runs where ProgramData files were created
    by SYSTEM and the current user can't reopen them: %LOCALAPPDATA%\\ADT
    Agent, then ~/.adt-agent.
    """
    candidates: list[Path] = [DATA_DIR]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "ADT Agent")
    candidates.append(Path.home() / ".adt-agent")

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            log_path = candidate / "agent.log"
            with open(log_path, "a", encoding="utf-8"):
                pass
            return candidate
        except (PermissionError, OSError) as exc:
            last_error = exc
            continue
    raise PermissionError(
        f"No writable data directory found (last error: {last_error})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Vizhi agent (Windows)")
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

    bootstrap = is_frozen() and not is_running_from_install_dir()
    if bootstrap:
        if not is_admin():
            print(
                "Run this file as administrator (right-click -> Run as administrator), "
                "then enter your enrollment code from the Vizhi portal.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        try:
            install_windows_agent()
        except Exception as exc:
            print(f"Install failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print("Agent installed to C:\\Program Files\\ADT Agent")

    data_dir = resolve_data_dir()
    (data_dir / "update").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    log_file = data_dir / "agent.log"
    config_file = data_dir / "config.json"

    if args.clear:
        if config_file.exists():
            config_file.unlink()
            print(f"Cleared device identity from {config_file}")
            print("Run the agent again as administrator to enroll.")
        else:
            print(f"No device identity found at {config_file}")
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
        print("Starting the ADTAgent scheduled task...")
        start_installed_agent()
        print("Done. This computer is now connected to Vizhi.")
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
        cleanup_previous_backup(INSTALL_EXE_PATH)

    if not endpoint_id or not is_valid_uuid(endpoint_id):
        raise ValueError(f"Invalid or missing ENDPOINT_ID in {config_file}")
    if not device_token:
        raise ValueError(f"Missing DEVICE_TOKEN in {config_file}")

    session = DeviceSession(api_base, config_file, AGENT_VERSION, local_config)

    last_runs = load_last_runs(local_config)
    logging.info(
        "Agent %s started as %s. Endpoint: %s...",
        AGENT_VERSION,
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
                    frozen=is_frozen(), install_exe=INSTALL_EXE_PATH
                ):
                    try:
                        if maybe_apply_update(
                            api_base=api_base,
                            device_token=session.device_token,
                            data_dir=data_dir,
                            install_exe=INSTALL_EXE_PATH,
                        ):
                            logging.info(
                                "Agent binary replaced; exiting so the scheduled task restarts"
                            )
                            raise SystemExit(0)
                    except SystemExit:
                        raise
                    except Exception:
                        logging.exception("Self-update check failed")

            if should_run("inventory", last_runs, intervals["inventory"], now_ts):
                try:
                    software_snapshot, os_facts = collect_installed_software(
                        logger=logging.getLogger(),
                        api_base=api_base,
                        device_token=session.device_token,
                    )
                    logging.info(
                        "Software snapshot refreshed: %s entries",
                        len(software_snapshot),
                    )
                    payload = collect_metrics(software_snapshot)
                    report_telemetry(
                        api_base,
                        session.device_token,
                        metrics=payload,
                        inventory=software_snapshot,
                        os_facts=os_facts if os_facts else None,
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
        logging.info("Agent stopped by user")
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Fatal loop error, exiting so the scheduler can restart us: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
