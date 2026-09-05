"""Agent-to-server reporting over the device-token API.

Every call here authenticates with the per-device token rather than a shared key, which is what
lets the patch tables keep RLS on with no anonymous access. Failures are returned, never raised
past the caller's log line: a server that is briefly unreachable must not stop an agent from
collecting metrics.

When a DeviceSession is supplied, HTTP 401 responses trigger one automatic re-enrollment attempt
using the org code stored in config.json.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests

from enrollment import device_headers

if TYPE_CHECKING:
    from device_session import DeviceSession

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60


def _api_request(
    api_base: str,
    device_token: str,
    method: str,
    path: str,
    *,
    session: DeviceSession | None = None,
    **kwargs: Any,
) -> requests.Response | None:
    if session is not None:
        return session.request(method, path, **kwargs)

    base = (api_base or "").strip().rstrip("/")
    if not base or not device_token:
        return None
    url = f"{base}{path}"
    headers = {**device_headers(device_token), **(kwargs.pop("headers", None) or {})}
    try:
        return requests.request(method, url, headers=headers, **kwargs)
    except requests.RequestException as exc:
        log.warning("%s %s failed to send: %s", method.upper(), path, exc)
        return None


def report_updates(
    api_base: str,
    device_token: str,
    *,
    scan: dict[str, Any],
    os_facts: dict[str, Any],
    agent_version: str,
    platform_tag: str,
    timeout: int = DEFAULT_TIMEOUT,
    session: DeviceSession | None = None,
) -> bool:
    """Publish the update scan, agent capabilities and reboot state. True when accepted."""
    if not (api_base or "").strip() or not device_token:
        return False

    body = {
        "updates": scan.get("updates") or [],
        "capabilities": scan.get("capabilities") or {},
        "reboot_pending": bool(scan.get("reboot_pending")),
        "os_facts": os_facts or {},
        "agent_version": agent_version,
        "platform": platform_tag,
    }

    resp = _api_request(
        api_base,
        device_token,
        "POST",
        "/api/agent/updates",
        session=session,
        json=body,
        timeout=timeout,
    )
    if resp is None:
        return False

    if resp.status_code >= 400:
        log.warning("Update report rejected (HTTP %s): %s", resp.status_code, resp.text[:300])
        return False

    try:
        result = resp.json()
        log.info(
            "Update report accepted: %s updates (%s skipped), reboot_pending=%s",
            result.get("accepted"),
            result.get("skipped"),
            result.get("reboot_pending"),
        )
    except ValueError:
        log.info("Update report accepted")
    return True


def fetch_patch_jobs(
    api_base: str,
    device_token: str,
    timeout: int = DEFAULT_TIMEOUT,
    session: DeviceSession | None = None,
) -> list[dict[str, Any]]:
    """Jobs the engine has released to this endpoint (`status=sent`)."""
    resp = _api_request(
        api_base,
        device_token,
        "GET",
        "/api/agent/jobs",
        session=session,
        timeout=timeout,
    )
    if resp is None:
        return []
    if resp.status_code >= 400:
        log.warning("Patch job poll rejected (HTTP %s): %s", resp.status_code, resp.text[:300])
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, list) else []


def report_job_status(
    api_base: str,
    device_token: str,
    job_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    version_after: str | None = None,
    reboot_required: bool | None = None,
    scan: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    session: DeviceSession | None = None,
) -> bool:
    if not (api_base or "").strip() or not device_token or not job_id:
        return False
    body: dict[str, Any] = {"id": job_id, "status": status}
    if exit_code is not None:
        body["exit_code"] = exit_code
    if stdout is not None:
        body["stdout"] = stdout
    if stderr is not None:
        body["stderr"] = stderr
    if error_code is not None:
        body["error_code"] = error_code
    if error_message is not None:
        body["error_message"] = error_message
    if version_after is not None:
        body["version_after"] = version_after
    if reboot_required is not None:
        body["reboot_required"] = reboot_required
    if scan is not None:
        body["scan"] = scan
    resp = _api_request(
        api_base,
        device_token,
        "POST",
        "/api/agent/jobs",
        session=session,
        json=body,
        timeout=timeout,
    )
    if resp is None:
        return False
    if resp.status_code >= 400:
        log.warning("Patch job report rejected (HTTP %s): %s", resp.status_code, resp.text[:300])
        return False
    return True


def fetch_pending_commands(
    api_base: str,
    device_token: str,
    timeout: int = 20,
    session: DeviceSession | None = None,
) -> list[dict[str, Any]]:
    """Diagnostic console commands queued for this endpoint. Empty list on any failure."""
    resp = _api_request(
        api_base,
        device_token,
        "GET",
        "/api/agent/commands",
        session=session,
        timeout=timeout,
    )
    if resp is None:
        return []
    if resp.status_code >= 400:
        log.warning("Command poll rejected (HTTP %s): %s", resp.status_code, resp.text[:300])
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    commands = data.get("commands") if isinstance(data, dict) else None
    return commands if isinstance(commands, list) else []


def report_telemetry(
    api_base: str,
    device_token: str,
    *,
    metrics: dict[str, Any] | None = None,
    inventory: list[dict[str, Any]] | None = None,
    os_facts: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    session: DeviceSession | None = None,
) -> bool:
    """Push metrics and/or software inventory through Vizhi. No Supabase keys required."""
    if not (api_base or "").strip() or not device_token:
        return False
    if not metrics and not inventory and not os_facts:
        return False
    body: dict[str, Any] = {}
    if metrics is not None:
        body["metrics"] = metrics
    if inventory is not None:
        body["inventory"] = inventory
    if os_facts is not None:
        body["os_facts"] = os_facts
    resp = _api_request(
        api_base,
        device_token,
        "POST",
        "/api/agent/telemetry",
        session=session,
        json=body,
        timeout=timeout,
    )
    if resp is None:
        return False
    if resp.status_code >= 400:
        log.warning("Telemetry rejected (HTTP %s): %s", resp.status_code, resp.text[:300])
        return False
    return True


def report_command_result(
    api_base: str,
    device_token: str,
    command_id: str,
    result_text: str,
    timeout: int = 20,
    session: DeviceSession | None = None,
) -> bool:
    if not (api_base or "").strip() or not device_token or not command_id:
        return False
    resp = _api_request(
        api_base,
        device_token,
        "POST",
        "/api/agent/commands",
        session=session,
        json={"id": command_id, "result": result_text},
        timeout=timeout,
    )
    if resp is None:
        return False
    if resp.status_code >= 400:
        log.warning("Command result rejected (HTTP %s): %s", resp.status_code, resp.text[:300])
        return False
    return True
