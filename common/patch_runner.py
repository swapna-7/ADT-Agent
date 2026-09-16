"""Claim and execute patch_jobs using native installers.

Mirrors command_poller: a daemon thread so a long WUA install cannot block metrics or command
polling. The server never sends a shell string; every job names a source and an exact identifier.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from command_poller import start_command_poller
from job_signing import sign_patch_job
from report import fetch_patch_jobs, report_job_status
from updates import apply_update, scan_updates

if TYPE_CHECKING:
    from device_session import DeviceSession
    from pathlib import Path

log = logging.getLogger(__name__)

_VERSION_SOURCES = frozenset({"registry_app", "npm_global", "pip_global", "chocolatey"})


def _version_for(
    scan: dict[str, Any],
    source: str,
    update_uid: str,
    package_name: str | None,
) -> str | None:
    for row in scan.get("updates") or []:
        if str(row.get("source")) != source:
            continue
        if str(row.get("update_uid")) == update_uid:
            return str(row.get("current_version") or row.get("available_version") or "") or None
        if package_name and str(row.get("package_name") or "") == package_name:
            return str(row.get("current_version") or "") or None
    return None


def report_job_failed(
    api_base: str,
    token: str,
    job_id: str,
    *,
    error_code: str,
    error_message: str,
    session: "DeviceSession | None" = None,
) -> None:
    """POST status=failed; never raises — log if the report itself fails."""
    try:
        report_job_status(
            api_base,
            token,
            job_id,
            status="failed",
            exit_code=1,
            error_code=error_code,
            error_message=error_message,
            session=session,
        )
    except Exception as exc:
        log.exception(
            "Could not report patch job %s as failed (%s): %s",
            job_id[:8],
            error_code,
            exc,
        )


def _verify_job_signature_fatal(
    api_base: str,
    token: str,
    job_id: str,
    *,
    signing_key: str | None,
    signature: str | None,
    endpoint_id: str,
    source: str,
    update_uid: str,
    package_name: str | None,
    target_version: str | None,
    session: "DeviceSession | None",
) -> bool:
    """Return True only when HMAC is valid; otherwise report failed and return False."""
    if not signing_key:
        log.error("Patch job %s rejected: no_signing_key", job_id[:8])
        report_job_failed(
            api_base,
            token,
            job_id,
            error_code="no_signing_key",
            error_message=(
                "JOB_SIGNING_KEY missing from config.json — job rejected"
            ),
            session=session,
        )
        return False

    if not signature:
        log.error("Patch job %s rejected: no_signature", job_id[:8])
        report_job_failed(
            api_base,
            token,
            job_id,
            error_code="no_signature",
            error_message="job_signature absent — job rejected",
            session=session,
        )
        return False

    try:
        expected = sign_patch_job(
            signing_key,
            job_id=job_id,
            endpoint_id=endpoint_id,
            source=source,
            update_uid=update_uid,
            package_name=package_name,
            target_version=target_version,
        )
    except Exception as exc:
        log.error("Patch job %s rejected: hmac_compute_error", job_id[:8])
        report_job_failed(
            api_base,
            token,
            job_id,
            error_code="hmac_compute_error",
            error_message=f"HMAC computation failed: {exc}",
            session=session,
        )
        return False

    import hmac

    try:
        valid = hmac.compare_digest(expected, signature)
    except Exception as exc:
        log.error("Patch job %s rejected: hmac_compare_error", job_id[:8])
        report_job_failed(
            api_base,
            token,
            job_id,
            error_code="hmac_compute_error",
            error_message=f"HMAC comparison failed: {exc}",
            session=session,
        )
        return False

    if not valid:
        log.error("Patch job %s rejected: signature_invalid", job_id[:8])
        report_job_failed(
            api_base,
            token,
            job_id,
            error_code="signature_invalid",
            error_message=(
                "HMAC mismatch — job rejected; possible tampering"
            ),
            session=session,
        )
        return False

    return True


def process_patch_jobs(
    api_base: str,
    device_token: str,
    endpoint_role: str | None = None,
    session: DeviceSession | None = None,
    config_path: "Path | None" = None,
) -> bool:
    """Pick up at most one job per poll so a fleet of pending installs cannot starve the rest of the agent.

    Returns False always: a patch job never stops the agent process.
    """
    token = session.device_token if session is not None else device_token
    if not api_base or not token:
        return False

    jobs = fetch_patch_jobs(
        api_base, token, session=session, config_path=config_path
    )
    if not jobs:
        return False

    job = jobs[0]
    job_id = str(job.get("id") or "")
    source = str(job.get("source") or "")
    update_uid = str(job.get("update_uid") or "")
    package_name = str(job.get("package_name") or "") or None
    target_version = str(job.get("target_version") or "") or None
    prior_status = str(job.get("status") or "")
    signature = str(job.get("job_signature") or "") or None
    endpoint_id = str(
        job.get("endpoint_id")
        or (session.endpoint_id if session is not None else "")
        or ""
    )
    if not job_id or not source or not update_uid:
        return False

    signing_key = None
    if session is not None:
        signing_key = str(getattr(session, "job_signing_key", "") or "").strip() or None
        if not signing_key:
            signing_key = str(session._config.get("JOB_SIGNING_KEY") or "").strip() or None
    if not _verify_job_signature_fatal(
        api_base,
        token,
        job_id,
        signing_key=signing_key,
        signature=signature,
        endpoint_id=endpoint_id,
        source=source,
        update_uid=update_uid,
        package_name=package_name,
        target_version=target_version,
        session=session,
    ):
        return False

    if prior_status == "rebooting":
        return _verify_and_finish(
            api_base,
            token,
            job_id,
            source,
            update_uid,
            package_name,
            endpoint_role=endpoint_role,
            version_after=None,
            stdout="",
            session=session,
        )

    log.info("Patch job %s claimed: %s %s", job_id[:8], source, update_uid)
    report_job_status(api_base, token, job_id, status="received", session=session)

    pre_scan = scan_updates(
        api_base=api_base,
        device_token=token,
        endpoint_role=endpoint_role,
    )
    version_before = _version_for(pre_scan, source, update_uid, package_name)

    last_reported = "received"

    def on_progress(phase: str) -> None:
        nonlocal last_reported
        if phase in {"downloading", "installing"} and phase != last_reported:
            report_job_status(api_base, token, job_id, status=phase, session=session)
            last_reported = phase

    result = apply_update(
        source,
        update_uid,
        package_name=package_name,
        target_version=target_version,
        on_progress=on_progress,
        api_base=api_base,
        device_token=token,
        endpoint_role=endpoint_role,
        version_before=version_before,
    )

    if result.get("not_applicable"):
        report_job_status(
            api_base,
            token,
            job_id,
            status="not_applicable",
            exit_code=0,
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            error_code="not_applicable",
            error_message=result.get("error_message"),
            session=session,
        )
        return False

    if not result.get("ok"):
        report_job_status(
            api_base,
            token,
            job_id,
            status="failed",
            exit_code=int(result.get("exit_code") or 1),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            error_code=result.get("error_code"),
            error_message=result.get("error_message"),
            session=session,
        )
        return False

    if result.get("reboot_required") and source in _VERSION_SOURCES:
        report_job_status(
            api_base,
            token,
            job_id,
            status="completed_pending_reboot",
            exit_code=0,
            stdout=str(result.get("stdout") or ""),
            version_after=result.get("version_after"),
            reboot_required=True,
            session=session,
        )
        return False

    if result.get("reboot_required"):
        report_job_status(
            api_base,
            token,
            job_id,
            status="rebooting",
            exit_code=0,
            stdout=str(result.get("stdout") or ""),
            reboot_required=True,
            session=session,
        )
        return False

    return _verify_and_finish(
        api_base,
        token,
        job_id,
        source,
        update_uid,
        package_name,
        endpoint_role=endpoint_role,
        version_after=result.get("version_after"),
        version_before=version_before,
        stdout=str(result.get("stdout") or ""),
        session=session,
    )


def _verify_and_finish(
    api_base: str,
    device_token: str,
    job_id: str,
    source: str,
    update_uid: str,
    package_name: str | None,
    *,
    endpoint_role: str | None = None,
    version_after: str | None,
    version_before: str | None = None,
    stdout: str,
    session: DeviceSession | None = None,
) -> bool:
    report_job_status(api_base, device_token, job_id, status="verifying", session=session)
    time.sleep(2)
    scan = scan_updates(
        api_base=api_base,
        device_token=device_token,
        endpoint_role=endpoint_role,
    )
    after = version_after or _version_for(scan, source, update_uid, package_name)

    if source in _VERSION_SOURCES and version_before and after:
        from updates import _version_gt

        if not _version_gt(after, version_before):
            err_code = (
                "version_unchanged_after_choco_upgrade"
                if source == "chocolatey"
                else "version_unchanged_after_install"
            )
            report_job_status(
                api_base,
                device_token,
                job_id,
                status="failed",
                exit_code=1,
                error_code=err_code,
                error_message=err_code,
                version_after=after,
                stdout=stdout,
                scan=dict(scan),
                session=session,
            )
            return False

    still_offered = any(
        str(row.get("source")) == source and str(row.get("update_uid")) == update_uid
        for row in scan.get("updates") or []
    )
    if still_offered and source in {"windows_update", "apt", "dnf"}:
        report_job_status(
            api_base,
            device_token,
            job_id,
            status="failed",
            exit_code=1,
            error_code="still_not_installed",
            error_message="Installer succeeded but the OS still offers this update.",
            version_after=after,
            stdout=stdout,
            scan=dict(scan),
            session=session,
        )
        return False

    report_job_status(
        api_base,
        device_token,
        job_id,
        status="completed",
        exit_code=0,
        stdout=stdout,
        version_after=after,
        reboot_required=False,
        scan=dict(scan),
        session=session,
    )
    return False


def start_patch_poller(
    api_base: str,
    device_token: str,
    interval_seconds: float,
    endpoint_role: str | None = None,
    session: DeviceSession | None = None,
    config_path: "Path | None" = None,
) -> None:
    start_command_poller(
        lambda: process_patch_jobs(
            api_base,
            session.device_token if session is not None else device_token,
            endpoint_role,
            session=session,
            config_path=config_path,
        ),
        interval_seconds,
    )
