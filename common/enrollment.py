"""Agent self-enrollment: the device obtains its own identity from an org enrollment code.

Flow:
    downloaded binary is run as admin/root (optionally with --code VZ-XXXX-XXXX-XXXX)
        -> POST {api}/api/agent/enroll {code, hostname, machine_guid, platform, ...}
        -> {endpoint_id, device_token}
        -> persisted to config.json

After that the agent authenticates with `device_token` and never needs the code again. Re-running
the binary on the same machine re-enrolls into the same endpoint row, preserving history.
"""

from __future__ import annotations

import logging
import os
import platform as platform_mod
import re
import socket
import subprocess
import sys
import uuid
from typing import Any

import requests

ENROLL_PATH = "/api/agent/enroll"
DEFAULT_TIMEOUT = 30

CODE_RE = re.compile(r"^VZ(-[A-Z0-9]{4})+$")


class EnrollmentError(RuntimeError):
    """Enrollment could not complete; message is safe to show the installing admin."""


def normalize_code(raw: str) -> str:
    """Uppercase, strip separators, and re-group so hand-typed codes still validate."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    body = cleaned[2:] if cleaned.startswith("VZ") else cleaned
    groups = [body[i : i + 4] for i in range(0, len(body), 4)]
    return "VZ-" + "-".join(groups) if groups else ""


def is_valid_code(raw: str) -> bool:
    return bool(CODE_RE.match(normalize_code(raw)))


def system_hostname() -> str:
    """Best-effort computer name; stored on the endpoint as hostname, not the display name."""
    for candidate in (
        lambda: platform_mod.node(),
        lambda: socket.gethostname(),
    ):
        try:
            value = str(candidate() or "").strip()
        except Exception:
            continue
        if value:
            # Strip any DNS suffix so "PC-001.corp.local" matches "PC-001".
            return value.split(".")[0]
    return "unknown-host"


def system_username() -> str | None:
    """Logged-on account as shown in Windows System Information → User Name (e.g. ASUS\\swapn)."""
    if sys.platform == "win32":
        return _windows_username()
    return _posix_username()


def _clean_username(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    upper = text.replace("/", "\\").upper()
    if upper in {"SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE"}:
        return None
    if upper.endswith("\\SYSTEM") or upper.startswith("NT AUTHORITY\\"):
        return None
    return text.replace("/", "\\")


def _windows_username() -> str | None:
    for getter in (
        _windows_wmi_username,
        _windows_logonui_username,
        _windows_env_username,
    ):
        try:
            cleaned = _clean_username(getter())
        except Exception:
            continue
        if cleaned:
            return cleaned
    return None


def _windows_wmi_username() -> str | None:
    """Same value msinfo32 reports as User Name (currently logged-on user)."""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance -ClassName Win32_ComputerSystem).UserName",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return (proc.stdout or "").strip() or None


def _windows_logonui_username() -> str | None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        for name in ("LastLoggedOnSAMUser", "LastLoggedOnUser"):
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def _windows_env_username() -> str | None:
    user = (os.environ.get("USERNAME") or "").strip()
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    if not user:
        return None
    if domain and domain.upper() not in {"", "NT AUTHORITY"}:
        return f"{domain}\\{user}"
    return user


def _posix_username() -> str | None:
    for candidate in (
        lambda: os.environ.get("SUDO_USER"),
        lambda: os.environ.get("USER"),
        lambda: os.environ.get("LOGNAME"),
        lambda: os.getlogin(),
    ):
        try:
            cleaned = _clean_username(candidate())
        except Exception:
            continue
        if cleaned:
            return cleaned
    return None


def machine_guid() -> str | None:
    """Stable per-machine identifier, so a rename does not create a duplicate endpoint."""
    if sys.platform == "win32":
        return _windows_machine_guid()
    return _linux_machine_id()


def _windows_machine_guid() -> str | None:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            text = str(value).strip()
            return text or None
    except Exception:
        return None


def _linux_machine_id() -> str | None:
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except Exception:
            continue
    try:
        return str(uuid.getnode())
    except Exception:
        return None


def os_version_string() -> str:
    if sys.platform == "win32":
        release = platform_mod.release()
        version = platform_mod.version()
        return f"Windows {release} (build {version})".strip()
    try:
        pretty = _linux_pretty_name()
        if pretty:
            return pretty
    except Exception:
        pass
    return f"{platform_mod.system()} {platform_mod.release()}".strip()


def _linux_pretty_name() -> str | None:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"') or None
    except Exception:
        return None
    return None


def platform_tag() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def enroll(
    api_base: str,
    code: str,
    agent_version: str,
    *,
    role: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, str]:
    """Register this machine and return {'ENDPOINT_ID': ..., 'DEVICE_TOKEN': ...}."""
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise EnrollmentError("No Vizhi server URL configured. Rebuild the agent with VIZHI_API_BASE.")

    normalized = normalize_code(code)
    if not is_valid_code(normalized):
        raise EnrollmentError(
            "Enrollment code looks malformed. Expected the form VZ-XXXX-XXXX-XXXX "
            "as shown under Add endpoint in Vizhi."
        )

    body = {
        "code": normalized,
        "hostname": system_hostname(),
        "username": system_username(),
        "machine_guid": machine_guid(),
        "platform": platform_tag(),
        "os_version": os_version_string(),
        "agent_version": agent_version,
    }
    if role:
        body["role"] = role

    url = f"{base}{ENROLL_PATH}"
    logging.info("Enrolling %s with Vizhi at %s", body["hostname"], url)

    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise EnrollmentError(f"Could not reach {url}: {exc}") from exc

    if 300 <= resp.status_code < 400:
        location = (resp.headers.get("Location") or str(resp.url) or "").strip()
        raise EnrollmentError(
            f"Vizhi enrollment was redirected (HTTP {resp.status_code}) to {location}. "
            "POST /api/agent/enroll must be public. If you downloaded from a local portal, "
            "re-run with --api <portal-url> (for example --api http://localhost:3000)."
        )

    if resp.status_code >= 400:
        raise EnrollmentError(_server_error_message(resp))

    try:
        data = resp.json()
    except ValueError as exc:
        raise EnrollmentError("Vizhi returned an unreadable enrollment response.") from exc

    endpoint_id = str(data.get("endpoint_id") or "").strip()
    device_token = str(data.get("device_token") or "").strip()
    if not endpoint_id or not device_token:
        raise EnrollmentError("Vizhi did not return a device identity. Contact your administrator.")

    logging.info(
        "Enrolled successfully endpoint=%s… reused_existing=%s",
        endpoint_id[:8],
        bool(data.get("reused_existing")),
    )
    role = str(data.get("role") or "").strip()
    out: dict[str, str] = {"ENDPOINT_ID": endpoint_id, "DEVICE_TOKEN": device_token}
    if role:
        out["ROLE"] = role
    return out


def _server_error_message(resp: Any) -> str:
    try:
        payload = resp.json()
        message = str(payload.get("error") or "").strip()
        if message:
            return message
    except Exception:
        pass
    return f"Enrollment failed with HTTP {resp.status_code}."


def device_headers(device_token: str) -> dict[str, str]:
    """Auth headers for every /api/agent/* call after enrollment."""
    return {
        "Authorization": f"Bearer {device_token}",
        "Content-Type": "application/json",
    }


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def run_powershell_json(script: str, timeout: int) -> Any:
    """Run a PowerShell snippet expected to emit JSON. Returns None on any failure."""
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    output = (proc.stdout or "").strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None
