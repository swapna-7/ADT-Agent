import argparse
import json
import logging
import os
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import tkinter as tk
from tkinter import ttk

import psutil
import requests


REG_PATHS = [
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
]


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
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        out: dict[str, str] = {}
        for key, value in data.items():
            out[str(key)] = str(value)
        return out
    return {}


def save_local_config(path: Path, config: dict[str, str]) -> None:
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def prompt_user_id_popup(initial_value: str = "") -> str:
    result: dict[str, str | None] = {"value": None}

    root = tk.Tk()
    root.title("ADT Agent Setup")
    root.resizable(False, False)

    root.geometry("380x170")
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="Enter your user ID", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text="Must be a valid UUID format (e.g. xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).",
        font=("Segoe UI", 9),
    ).pack(anchor="w", pady=(2, 10))

    user_var = tk.StringVar(value=initial_value)
    entry = ttk.Entry(frame, textvariable=user_var, width=42)
    entry.pack(fill="x")
    entry.focus_set()

    error_label = ttk.Label(frame, text="", foreground="#b00020")
    error_label.pack(anchor="w", pady=(6, 0))

    button_row = ttk.Frame(frame)
    button_row.pack(fill="x", pady=(10, 0))

    def on_save() -> None:
        value = user_var.get().strip()
        if not value:
            error_label.config(text="User ID is required.")
            return
        if not is_valid_uuid(value):
            error_label.config(text="Invalid UUID format (e.g. xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).")
            return
        result["value"] = value
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    ttk.Button(button_row, text="Cancel", command=on_cancel).pack(side="right")
    ttk.Button(button_row, text="Save", command=on_save).pack(side="right", padx=(0, 8))

    root.bind("<Return>", lambda _e: on_save())
    root.protocol("WM_DELETE_WINDOW", on_cancel)
    root.mainloop()

    if not result["value"]:
        raise RuntimeError("User ID entry cancelled")
    return result["value"]


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
            if ($props.DisplayName) {
                $version = $props.DisplayVersion
                if (-not $version) { $version = "unknown" }
                $publisher = $props.Publisher
                if (-not $publisher) { $publisher = "unknown" }
                $installDate = $props.InstallDate
                $apps += @{
                    Name = $props.DisplayName
                    Version = $version
                    Publisher = $publisher
                    InstallDate = $installDate
                } | ConvertTo-Json -Compress
            }
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
                        key = (name.lower(), version, publisher.lower())
                        if key not in seen:
                            seen.add(key)
                            rows.append(
                                {
                                    "name": name,
                                    "version": version,
                                    "publisher": publisher,
                                    "install_date": install_date,
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


def _collect_software_from_wmi(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    try:
        result = subprocess.run(
            ["wmic", "product", "get", "name,version,vendor,installdate", "/format:csv"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            if logger:
                logger.debug(f"wmic query failed: {result.stderr}")
            return []
        rows: list[SoftwareRow] = []
        seen: set[tuple[str, str, str]] = set()
        for line in result.stdout.strip().split("\n")[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4 and parts[1]:
                name = parts[1]
                version = parts[2] or "unknown"
                publisher = parts[3] or "unknown"
                install_date = normalize_install_date(parts[0] if parts[0] else None)
                key = (name.lower(), version, publisher.lower())
                if key not in seen:
                    seen.add(key)
                    rows.append(
                        {
                            "name": name,
                            "version": version,
                            "publisher": publisher,
                            "install_date": install_date,
                        }
                    )
        if logger and rows:
            logger.debug(f"Found {len(rows)} apps via WMI")
        return rows
    except Exception as e:
        if logger:
            logger.debug(f"WMI collection failed: {e}")
        return []


def _collect_software_from_filesystem(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    rows: list[SoftwareRow] = []
    seen: set[str] = set()
    prog_files = [
        Path("C:\\Program Files"),
        Path("C:\\Program Files (x86)"),
    ]
    for prog_dir in prog_files:
        if not prog_dir.exists():
            continue
        try:
            for item in prog_dir.iterdir():
                if item.is_dir() and item.name not in seen:
                    seen.add(item.name)
                    rows.append(
                        {
                            "name": item.name,
                            "version": "installed",
                            "publisher": "unknown",
                            "install_date": None,
                        }
                    )
        except (PermissionError, OSError) as e:
            if logger:
                logger.debug(f"Error scanning {prog_dir}: {e}")
    if logger and rows:
        logger.debug(f"Found {len(rows)} apps via filesystem")
    return rows


def collect_installed_software(logger: logging.Logger | None = None) -> list[SoftwareRow]:
    all_rows: list[SoftwareRow] = []
    seen: set[tuple[str, str, str]] = set()

    methods = [
        ("PowerShell Registry", _collect_software_from_powershell),
        ("WMI", _collect_software_from_wmi),
        ("Filesystem", _collect_software_from_filesystem),
    ]

    for method_name, method_func in methods:
        try:
            rows = method_func(logger)
            if logger:
                logger.debug(f"{method_name}: {len(rows)} entries")
            for row in rows:
                name = str(row.get("name", "")).strip()
                version = str(row.get("version", "unknown")).strip() or "unknown"
                publisher = str(row.get("publisher", "unknown")).strip() or "unknown"
                install_date = normalize_install_date(str(row.get("install_date", "")))
                if not name:
                    continue
                key = (name.lower(), version, publisher.lower())
                if key not in seen:
                    seen.add(key)
                    all_rows.append(
                        {
                            "name": name,
                            "version": version,
                            "publisher": publisher,
                            "install_date": install_date,
                        }
                    )
        except Exception as e:
            if logger:
                logger.debug(f"{method_name} method failed: {e}")

    all_rows.sort(key=lambda x: x["name"].lower())
    if logger:
        logger.debug(f"Total unique software found: {len(all_rows)} entries")
    return all_rows


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


def get_cpu_temperature() -> float | None:
    """Get CPU temperature in Celsius if available."""
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                if "Core" in entries[0].label or "Package" in entries[0].label or "CPU" in name:
                    return float(entries[0].current)
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


def collect_metrics(software: list[SoftwareRow]) -> dict[str, object]:
    cpu_percent = psutil.cpu_percent(interval=0.1)
    cpu_count_logical = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)
    cpu_freq = psutil.cpu_freq()
    cpu_model = get_cpu_model_name()
    cpu_temp = get_cpu_temperature()
    cpu_uptime = get_cpu_uptime_seconds()
    top_process_name, top_process_cpu = get_top_process_info()
    gpu = get_gpu_metrics()
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    batt = psutil.sensors_battery()

    return {
        "hostname": socket.gethostname(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_count_logical,
            "physical_cores": cpu_count_physical,
            "model": cpu_model,
            "base_clock_mhz": cpu_freq.min if cpu_freq else None,
            "current_clock_mhz": cpu_freq.current if cpu_freq else None,
            "temp_celsius": cpu_temp,
            "top_process_name": top_process_name,
            "top_process_cpu_percent": top_process_cpu,
            "uptime_seconds": cpu_uptime,
        },
        "gpu": gpu,
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "percent": vm.percent,
        },
        "storage": {
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent,
        },
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
    resp = requests.post(endpoint, data=json.dumps(body), headers=supabase_headers(anon_key), timeout=20)
    resp.raise_for_status()


def upsert_software_inventory(url: str, anon_key: str, endpoint_id: str, user_id: str, software: list[SoftwareRow]) -> None:
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
            }
        )
    headers = supabase_headers(anon_key)
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    params = {"on_conflict": "endpoint,software_name,version,publisher"}
    resp = requests.post(endpoint, headers=headers, params=params, data=json.dumps(rows), timeout=30)
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


def update_command_result(url: str, anon_key: str, command_id: str, result_text: str) -> None:
    endpoint = f"{url.rstrip('/')}" + "/rest/v1/commands"
    params = {"id": f"eq.{command_id}"}
    body = {"result": result_text}
    resp = requests.patch(endpoint, headers=supabase_headers(anon_key), params=params, data=json.dumps(body), timeout=20)
    resp.raise_for_status()


def process_pending_commands(url: str, anon_key: str, endpoint_id: str, timeout_seconds: int) -> None:
    pending = fetch_pending_commands(url, anon_key, endpoint_id)
    if not pending:
        return
    logging.info("Found %s pending command(s)", len(pending))
    for row in pending:
        command_id = str(row.get("id", ""))
        command_text = str(row.get("command", "")).strip()
        if not command_id:
            continue
        if not command_text:
            update_command_result(url, anon_key, command_id, "exit_code=invalid\nstderr:\nEmpty command")
            continue
        logging.info("Executing command id=%s", command_id)
        result_text = execute_powershell_command(command_text, timeout_seconds)
        update_command_result(url, anon_key, command_id, result_text)
        logging.info("Saved result for command id=%s", command_id)


def setup_logging(log_file: Path, verbose: bool, console_log: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if console_log:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main() -> None:
    parser = argparse.ArgumentParser(description="ADT Agent")
    parser.add_argument("--clear", action="store_true", help="Clear stored credentials and exit")
    args = parser.parse_args()

    base_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "adt-agent"
    base_dir.mkdir(parents=True, exist_ok=True)

    log_file = base_dir / "agent.log"
    config_file = base_dir / "config.json"

    if args.clear:
        if config_file.exists():
            config_file.unlink()
            print(f"Cleared stored credentials from {config_file}")
        else:
            print(f"No stored credentials found at {config_file}")
        return

    env = load_env(Path(".env"))
    local_config = load_local_config(config_file)
    supabase_url = os.getenv("SUPABASE_URL", env.get("SUPABASE_URL", ""))
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", env.get("SUPABASE_ANON_KEY", ""))
    user_id = os.getenv("USER_ID", env.get("USER_ID", local_config.get("USER_ID", "")))
    endpoint_id = os.getenv("ENDPOINT_ID", env.get("ENDPOINT_ID", user_id))
    interval_seconds = int(os.getenv("INTERVAL_SECONDS", env.get("INTERVAL_SECONDS", "900")))
    software_refresh_seconds = int(os.getenv("SOFTWARE_REFRESH_SECONDS", env.get("SOFTWARE_REFRESH_SECONDS", "21600")))
    command_poll_seconds = int(os.getenv("COMMAND_POLL_SECONDS", env.get("COMMAND_POLL_SECONDS", "20")))
    command_timeout_seconds = int(os.getenv("COMMAND_TIMEOUT_SECONDS", env.get("COMMAND_TIMEOUT_SECONDS", "60")))
    verbose = is_truthy(os.getenv("VERBOSE", env.get("VERBOSE", "0")))
    console_log = is_truthy(os.getenv("CONSOLE_LOG", env.get("CONSOLE_LOG", "1")))

    setup_logging(log_file, verbose=verbose, console_log=console_log)

    if not user_id:
        logging.info("USER_ID not set; opening popup for user input")
        user_id = prompt_user_id_popup()
        if not is_valid_uuid(user_id):
            raise ValueError(f"Invalid UUID format: {user_id}")
        local_config["USER_ID"] = user_id
        save_local_config(config_file, local_config)
        logging.info("USER_ID saved to %s", config_file)
    elif not is_valid_uuid(user_id):
        raise ValueError(f"Invalid UUID format in USER_ID: {user_id}")

    if endpoint_id and not is_valid_uuid(endpoint_id):
        raise ValueError(f"Invalid UUID format in ENDPOINT_ID: {endpoint_id}")

    logging.info(
        "Agent started. User ID: %s... Endpoint ID: %s... metrics=%ss software_refresh=%ss command_poll=%ss",
        user_id[:8],
        endpoint_id[:8] if endpoint_id else "none",
        interval_seconds,
        software_refresh_seconds,
        command_poll_seconds,
    )

    cycle = 0
    software_snapshot: list[SoftwareRow] = []
    software_snapshot_at = 0.0

    next_metric_run = 0.0
    next_command_poll = 0.0

    try:
        while True:
            now_ts = time.time()

            if now_ts >= next_command_poll:
                if supabase_url and supabase_anon_key and endpoint_id:
                    try:
                        process_pending_commands(
                            supabase_url,
                            supabase_anon_key,
                            endpoint_id,
                            command_timeout_seconds,
                        )
                    except Exception as exc:
                        logging.exception("Command poll failed: %s", exc)
                next_command_poll = now_ts + command_poll_seconds

            if now_ts >= next_metric_run:
                cycle += 1
                started = time.time()
                if not software_snapshot or (now_ts - software_snapshot_at) >= software_refresh_seconds:
                    software_snapshot = collect_installed_software(logger=logging.getLogger())
                    software_snapshot_at = now_ts
                    logging.info("Software snapshot refreshed: %s entries", len(software_snapshot))
                    if supabase_url and supabase_anon_key and endpoint_id:
                        upsert_software_inventory(
                            supabase_url,
                            supabase_anon_key,
                            endpoint_id,
                            user_id,
                            software_snapshot,
                        )
                        logging.info("Software inventory upserted: %s entries", len(software_snapshot))
                    if verbose and software_snapshot:
                        for item in software_snapshot:
                            logging.debug(
                                "Software: %s | %s | %s | install_date=%s",
                                item.get("name"),
                                item.get("version"),
                                item.get("publisher"),
                                item.get("install_date"),
                            )
                    elif not software_snapshot:
                        logging.warning("No installed software found. Check registry permissions or try with admin rights.")

                payload = collect_metrics(software_snapshot)
                if verbose:
                    logging.debug(
                        "Cycle %s metrics cpu=%s%% (model=%s, temp=%s°C, top_proc=%s@%s%%) gpu_temp=%s°C memory=%s%% storage=%s%% battery=%s software=%s uptime=%ss",
                        cycle,
                        payload["cpu"]["percent"],
                        payload["cpu"]["model"] or "unknown",
                        payload["cpu"]["temp_celsius"] or "N/A",
                        payload["cpu"]["top_process_name"] or "N/A",
                        payload["cpu"]["top_process_cpu_percent"] or 0,
                        payload["gpu"]["temp_celsius"] or "N/A",
                        payload["memory"]["percent"],
                        payload["storage"]["percent"],
                        payload["battery"]["percent"],
                        payload["software_count"],
                        payload["cpu"]["uptime_seconds"] or 0,
                    )
                if supabase_url and supabase_anon_key:
                    push_to_supabase(supabase_url, supabase_anon_key, user_id, payload)
                    logging.info("Cycle %s pushed to Supabase in %.2fs", cycle, time.time() - started)
                else:
                    logging.info("Cycle %s collected (Supabase not configured)", cycle)

                next_metric_run = now_ts + interval_seconds

            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Agent stopped by user")
    except Exception as exc:
        logging.exception("Collection/sync failed: %s", exc)


if __name__ == "__main__":
    main()
