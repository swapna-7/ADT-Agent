"""ADT Agent — macOS edition.

Separate from the Windows agent. Installs to
~/Library/Application Support/ADT Agent/ and registers a per-user
LaunchAgent (login + KeepAlive). Remote commands run under /bin/zsh -lc.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import plistlib
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

if sys.platform != "darwin":
    print("This binary is for macOS only.", file=sys.stderr)
    raise SystemExit(2)

_COMMON = Path(__file__).resolve().parent.parent / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))
from api_base import resolve_api_base
from command_poller import start_command_poller
from config_io import load_config, write_config
from enrollment import platform_tag, system_hostname, system_username
from first_run import needs_enrollment, prompt_and_enroll
from normalize import normalize_row
from patch_runner import start_patch_poller
from report import (
    fetch_pending_commands as fetch_agent_commands,
    report_command_result,
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
from updates import scan_updates
from version import AGENT_VERSION

# -----------------------------------------------------------------------------
# Paths & constants
# -----------------------------------------------------------------------------

INSTALL_ROOT = Path.home() / "Library" / "Application Support" / "ADT Agent"
INSTALL_BIN = INSTALL_ROOT / "adt-agent"
DATA_DIR = INSTALL_ROOT
LAUNCH_LABEL = "com.adt.agent"
AGENT_STOP_COMMAND = "__ADT_AGENT_STOP__"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
LAUNCH_OUT_LOG = INSTALL_ROOT / "launchd.stdout.log"
LAUNCH_ERR_LOG = INSTALL_ROOT / "launchd.stderr.log"

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


def save_local_config(path: Path, config: dict[str, str]) -> None:
    write_config(dict(config), path)


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
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


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_running_from_install_dir() -> bool:
    if not is_frozen():
        return True
    try:
        return Path(sys.executable).resolve().parent == INSTALL_ROOT.resolve()
    except Exception:
        return False


def _launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        timeout=45,
    )


def unload_launch_agent() -> None:
    uid = str(os.getuid())
    _launchctl(["bootout", f"gui/{uid}", LAUNCH_LABEL])
    if LAUNCH_PLIST.exists():
        _launchctl(["unload", "-w", str(LAUNCH_PLIST)])


def normalize_install_date(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _app_bundle_row(app_path: Path) -> SoftwareRow | None:
    pl_path = app_path / "Contents" / "Info.plist"
    if not pl_path.is_file():
        return None
    try:
        with pl_path.open("rb") as f:
            pl = plistlib.load(f)
    except Exception:
        return None
    name = pl.get("CFBundleName") or pl.get("CFBundleDisplayName") or app_path.stem
    short_version = pl.get("CFBundleShortVersionString")
    build_version = pl.get("CFBundleVersion")
    version = short_version or build_version or "unknown"
    bid = pl.get("CFBundleIdentifier")
    publisher = str(bid).strip() if bid else "unknown"
    display = str(name).strip()
    normalized = normalize_row(display, str(version), publisher, package_type="app_bundle")
    return {
        "name": display,
        "version": str(version).strip() or "unknown",
        "publisher": publisher or "unknown",
        "install_date": None,
        "package_type": "app_bundle",
        "source": "app_bundle",
        "sources": ["app_bundle"],
        "evidence": {
            "app_bundle": {
                "bundle_path": str(app_path),
                "bundle_id": publisher if bid else None,
                # Both are kept: Safari reports 18.5 short and 20621.2.5.11.8 build, and an
                # advisory may be written against either.
                "short_version": str(short_version) if short_version else None,
                "build_version": str(build_version) if build_version else None,
            }
        },
        "normalized_vendor": normalized.vendor,
        "normalized_product": normalized.product,
        "normalized_version": normalized.version,
        "match_confident": normalized.confident,
    }


def _collect_software_from_applications(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    roots = [
        Path("/Applications"),
        Path("/System/Applications"),
        Path.home() / "Applications",
    ]
    rows: list[SoftwareRow] = []
    seen: set[tuple[str, str, str]] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for app in root.glob("*.app"):
                row = _app_bundle_row(app)
                if not row or not row.get("name"):
                    continue
                key = (
                    str(row["name"]).lower(),
                    str(row.get("version", "")),
                    str(row.get("publisher", "")).lower(),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        except (OSError, PermissionError) as exc:
            if logger:
                logger.debug("Scan %s: %s", root, exc)
    rows.sort(key=lambda x: str(x["name"]).lower())
    return rows


def collect_os_facts() -> dict[str, Any]:
    """macOS product version and build, which is what an Apple security update is scoped to."""
    facts: dict[str, Any] = {"kernel": platform.release() or None}
    try:
        proc = subprocess.run(
            ["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=15, check=False
        )
        facts["os_version"] = (proc.stdout or "").strip() or None
        proc = subprocess.run(
            ["sw_vers", "-buildVersion"], capture_output=True, text=True, timeout=15, check=False
        )
        facts["build"] = (proc.stdout or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return facts


def collect_installed_software(
    logger: logging.Logger | None = None,
) -> tuple[list[SoftwareRow], dict[str, Any]]:
    return _collect_software_from_applications(logger), collect_os_facts()


def get_cpu_model_name() -> str | None:
    try:
        r = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.model"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def get_cpu_advertised_mhz() -> int | None:
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.cpufrequency_max"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            hz = int(r.stdout.strip())
            mhz = hz // 1_000_000
            return mhz if mhz > 0 else None
    except Exception:
        pass
    return None


def get_cpu_temperature() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for name, entries in temps.items():
            for entry in entries:
                if entry.current is not None:
                    return float(entry.current)
        return None
    except Exception:
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
            check=True,
        )
        line = (result.stdout or "").strip().splitlines()[0].strip()
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
        return gpu
    return gpu


def get_os_info() -> dict[str, str | None]:
    info: dict[str, str | None] = {
        "system": "Darwin",
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
    for key, swflag in (
        ("edition", "-productName"),
        ("version", "-productVersion"),
        ("build", "-buildVersion"),
    ):
        try:
            r = subprocess.run(
                ["sw_vers", swflag],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                info[key] = r.stdout.strip()
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
    system_root = "/"
    try:
        sys_usage = psutil.disk_usage(system_root)
    except (OSError, PermissionError):
        sys_usage = psutil.disk_usage("/")

    return {
        "drive": system_root,
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
        top_norm: float | None = round(top_process_cpu / cpu_count_logical, 1)
    else:
        top_norm = None

    return {
        "agent_variant": "macos",
        "hostname": socket.gethostname(),
        "username": system_username(),
        "agent_version": AGENT_VERSION,
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


def supabase_headers(anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def push_to_supabase(url: str, anon_key: str, user_id: str, payload: dict[str, object]) -> None:
    endpoint = f"{url.rstrip('/')}" + "/rest/v1/device_metrics"
    body = {
        "user_id": user_id,
        "captured_at": payload["captured_at"],
        "payload": payload,
    }
    resp = requests.post(
        endpoint,
        data=json.dumps(body),
        headers=supabase_headers(anon_key),
        timeout=20,
    )
    resp.raise_for_status()


def upsert_software_inventory(
    url: str,
    anon_key: str,
    endpoint_id: str,
    user_id: str,
    software: list[SoftwareRow],
) -> None:
    if not software:
        return
    endpoint = f"{url.rstrip('/')}" + "/rest/v1/software_inventory"
    scanned_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, object]] = []
    for item in software:
        rows.append(
            {
                "endpoint": endpoint_id,
                "user_id": user_id,
                "software_name": item.get("name"),
                "version": item.get("version"),
                "publisher": item.get("publisher"),
                "install_date": item.get("install_date"),
                "scanned_at": scanned_at,
                "package_type": item.get("package_type"),
                "sources": item.get("sources") or [],
                "evidence": item.get("evidence") or {},
                "normalized_vendor": item.get("normalized_vendor"),
                "normalized_product": item.get("normalized_product"),
                "normalized_version": item.get("normalized_version"),
            }
        )
    headers = supabase_headers(anon_key)
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    params = {"on_conflict": "endpoint,software_name,version,publisher"}
    resp = requests.post(
        endpoint,
        headers=headers,
        params=params,
        data=json.dumps(rows),
        timeout=30,
    )
    resp.raise_for_status()


def fetch_pending_commands(url: str, anon_key: str, endpoint_id: str) -> list[dict[str, Any]]:
    endpoint = f"{url.rstrip('/')}" + "/rest/v1/commands"
    params = {
        "select": "id,command,created_at",
        "endpoint": f"eq.{endpoint_id}",
        "result": "is.null",
        "order": "created_at.asc",
        "limit": "20",
    }
    resp = requests.get(endpoint, headers=supabase_headers(anon_key), params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def execute_shell_command(command: str, timeout_seconds: int) -> str:
    try:
        proc = subprocess.run(
            ["/bin/zsh", "-lc", command],
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


def update_command_result(url: str, anon_key: str, command_id: str, result_text: str) -> None:
    endpoint = f"{url.rstrip('/')}" + "/rest/v1/commands"
    params = {"id": f"eq.{command_id}"}
    resp = requests.patch(
        endpoint,
        headers=supabase_headers(anon_key),
        params=params,
        data=json.dumps({"result": result_text}),
        timeout=20,
    )
    resp.raise_for_status()


def process_pending_commands(
    url: str,
    anon_key: str,
    endpoint_id: str,
    timeout_seconds: int,
    api_base: str = "",
    device_token: str = "",
) -> bool:
    use_api = bool(api_base and device_token)
    pending = (
        fetch_agent_commands(api_base, device_token)
        if use_api
        else fetch_pending_commands(url, anon_key, endpoint_id)
    )
    if not pending:
        return False

    def save_result(command_id: str, result_text: str) -> None:
        if use_api:
            report_command_result(api_base, device_token, command_id, result_text)
        else:
            update_command_result(url, anon_key, command_id, result_text)

    logging.info("Found %s pending command(s)", len(pending))
    for row in pending:
        command_id = str(row.get("id", ""))
        command_text = str(row.get("command", "")).strip()
        if not command_id:
            continue
        if command_text == AGENT_STOP_COMMAND:
            logging.info("Remote stop command received id=%s", command_id)
            result_text = (
                "exit_code=0\nstdout:\nAgent stopped remotely. LaunchAgent unloaded.\nstderr:\n"
            )
            try:
                unload_launch_agent()
            except Exception as exc:
                result_text = f"exit_code=1\nstdout:\n\nstderr:\nFailed to stop agent: {exc}\n"
            save_result(command_id, result_text)
            logging.info("Remote stop acknowledged; exiting")
            return True
        if not command_text:
            save_result(command_id, "exit_code=invalid\nstderr:\nEmpty command")
            continue
        logging.info("Executing command id=%s", command_id)
        result_text = execute_shell_command(command_text, timeout_seconds)
        save_result(command_id, result_text)
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
    candidates = [DATA_DIR, Path.home() / ".adt-agent"]
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
    parser = argparse.ArgumentParser(description="Vizhi Agent (macOS)")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()

    data_dir = resolve_data_dir()
    (data_dir / "update").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    log_file = data_dir / "agent.log"
    config_file = data_dir / "config.json"

    if args.clear:
        if config_file.exists():
            config_file.unlink()
            print(f"Cleared device identity from {config_file}")
            print("Run mac/install.sh to enroll again.")
        else:
            print(f"No device identity at {config_file}")
        return

    env = load_env(find_env_file())
    local_config = load_local_config(config_file)
    api_base = resolve_api_base(local_config, env)

    if needs_enrollment(local_config):
        enrolled = prompt_and_enroll(
            api_base, config_file, AGENT_VERSION, local_config=local_config
        )
        local_config = {str(k): str(v) for k, v in enrolled.items()}

    supabase_url = os.getenv("SUPABASE_URL", env.get("SUPABASE_URL", ""))
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", env.get("SUPABASE_ANON_KEY", ""))
    device_token = os.getenv(
        "DEVICE_TOKEN", env.get("DEVICE_TOKEN", local_config.get("DEVICE_TOKEN", ""))
    )
    legacy_user_id = os.getenv("USER_ID", env.get("USER_ID", local_config.get("USER_ID", "")))
    endpoint_id = os.getenv(
        "ENDPOINT_ID", env.get("ENDPOINT_ID", local_config.get("ENDPOINT_ID", legacy_user_id))
    )
    endpoint_role = os.getenv(
        "ROLE", env.get("ROLE", local_config.get("ROLE", ""))
    ).strip() or None
    user_id = legacy_user_id or endpoint_id

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

    if not user_id:
        user_id = endpoint_id

    last_runs = load_last_runs(local_config)
    logging.info(
        "Agent %s started (macOS) as %s. Endpoint %s... enrolled=%s",
        AGENT_VERSION,
        system_hostname(),
        endpoint_id[:8],
        bool(device_token),
    )

    cycle = 0
    software_snapshot: list[SoftwareRow] = []
    os_facts: dict[str, Any] = {}
    next_self_update = (
        next_check_deadline(intervals["auto_update"], initial=True)
        if device_token
        else float("inf")
    )

    if endpoint_id and (device_token or (supabase_url and supabase_anon_key)):
        start_command_poller(
            lambda: process_pending_commands(
                supabase_url,
                supabase_anon_key,
                endpoint_id,
                command_timeout_seconds,
                api_base,
                device_token,
            ),
            intervals["command_poll"],
        )

    if device_token and api_base:
        start_patch_poller(
            api_base, device_token, intervals["patch_poll"], endpoint_role
        )

    try:
        while True:
            now_ts = time.time()
            local_config = load_local_config(config_file)
            last_runs = load_last_runs(local_config)

            if now_ts >= next_self_update and device_token:
                next_self_update = next_check_deadline(intervals["auto_update"])
                if read_auto_update_flag(local_config, env) and should_self_update(
                    frozen=is_frozen(), install_exe=INSTALL_BIN
                ):
                    try:
                        if maybe_apply_update(
                            api_base=api_base,
                            device_token=device_token,
                            data_dir=data_dir,
                            install_exe=INSTALL_BIN,
                        ):
                            logging.info(
                                "Agent binary replaced; exiting so launchd restarts"
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
                    logging.info(
                        "Software snapshot: %s entries (macOS %s build %s)",
                        len(software_snapshot),
                        os_facts.get("os_version"),
                        os_facts.get("build"),
                    )
                    if supabase_url and supabase_anon_key and endpoint_id:
                        try:
                            upsert_software_inventory(
                                supabase_url,
                                supabase_anon_key,
                                endpoint_id,
                                user_id,
                                software_snapshot,
                            )
                        except Exception as exc:
                            logging.exception("Software upsert failed: %s", exc)
                    local_config = mark_run(
                        config_file, local_config, "inventory", now_ts
                    )
                    last_runs = load_last_runs(local_config)
                except Exception as exc:
                    logging.exception("Inventory refresh failed: %s", exc)

            if device_token and should_run(
                "update_scan", last_runs, intervals["update_scan"], now_ts
            ):
                try:
                    scan = scan_updates(
                        api_base=api_base,
                        device_token=device_token,
                        endpoint_role=endpoint_role,
                    )
                    report_updates(
                        api_base,
                        device_token,
                        scan=scan,
                        os_facts=os_facts,
                        agent_version=AGENT_VERSION,
                        platform_tag=platform_tag(),
                    )
                    local_config = mark_run(
                        config_file, local_config, "update_scan", now_ts
                    )
                    last_runs = load_last_runs(local_config)
                except Exception as exc:
                    logging.exception("Update scan failed: %s", exc)

            if should_run("metrics", last_runs, intervals["metrics"], now_ts):
                cycle += 1
                started = time.time()
                try:
                    payload = collect_metrics(software_snapshot)
                    if verbose:
                        logging.debug(
                            "Cycle %s cpu=%s%% mem=%s%% storage=%s%%",
                            cycle,
                            payload["cpu"]["percent"],
                            payload["memory"]["percent"],
                            payload["storage"]["percent"],
                        )
                    if supabase_url and supabase_anon_key:
                        try:
                            push_to_supabase(
                                supabase_url, supabase_anon_key, user_id, payload
                            )
                            logging.info(
                                "Cycle %s pushed in %.2fs",
                                cycle,
                                time.time() - started,
                            )
                        except Exception as exc:
                            logging.exception(
                                "Supabase push failed cycle %s: %s", cycle, exc
                            )
                    else:
                        logging.info("Cycle %s (no Supabase)", cycle)
                    local_config = mark_run(
                        config_file, local_config, "metrics", now_ts
                    )
                    last_runs = load_last_runs(local_config)
                except Exception as exc:
                    logging.exception("Metric cycle %s failed: %s", cycle, exc)

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
