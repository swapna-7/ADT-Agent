"""First-run enrollment: baked-in code, --code, or TTY prompt."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from config_io import write_config
from enrollment import EnrollmentError, enroll, machine_guid, normalize_code, system_hostname

log = logging.getLogger(__name__)

ENROLL_PROMPT = (
    "Enter your Vizhi organisation enrollment code (VZ-XXXX-XXXX-XXXX): "
)
DEVICE_NAME_PROMPT = "Device name [{default}]: "


def needs_enrollment(config: dict[str, Any]) -> bool:
    token = str(config.get("DEVICE_TOKEN") or "").strip()
    return not token


def get_or_prompt_enrollment_code(local_config: dict[str, Any]) -> str | None:
    """Mode B: code baked in by org installer. Mode A: interactive prompt."""
    code = str(local_config.get("ENROLLMENT_CODE") or "").strip()
    if code:
        log.info("Using baked-in enrollment code: %s…", code[:8])
        return code

    if not sys.stdin.isatty():
        log.error(
            "No TTY and no ENROLLMENT_CODE in config.json — exiting"
        )
        raise SystemExit(2)

    raw = input(ENROLL_PROMPT).strip()
    return raw if raw else None


def get_device_name(
    local_config: dict[str, Any],
    *,
    override: str | None = None,
) -> str:
    """User-supplied display name; defaults to hostname when unset."""
    if override and override.strip():
        return override.strip()

    configured = str(local_config.get("DEVICE_NAME") or "").strip()
    if configured:
        return configured

    default_name = system_hostname()
    if sys.stdin.isatty():
        name_input = input(DEVICE_NAME_PROMPT.format(default=default_name)).strip()
        return name_input if name_input else default_name

    return default_name


def enroll_with_code(
    api_base: str,
    config_path: Path,
    agent_version: str,
    code: str,
    *,
    role: str = "",
    device_name: str | None = None,
    existing_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enroll with a known code. No TTY required. Writes config.json on success."""
    normalized = normalize_code(code)
    guid = machine_guid()
    resolved_name = (device_name or "").strip() or None

    try:
        result = enroll(
            api_base,
            normalized,
            agent_version,
            role=role or None,
            device_name=resolved_name,
        )
    except EnrollmentError as exc:
        print(f"Enrollment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    prior = existing_config or {}
    config: dict[str, Any] = {
        "ENDPOINT_ID": result["ENDPOINT_ID"],
        "DEVICE_TOKEN": result["DEVICE_TOKEN"],
        "ENROLLMENT_CODE": normalized,
        "API_BASE": (api_base or "").strip().rstrip("/"),
        "ROLE": result.get("ROLE") or role or "",
        "MACHINE_GUID": guid or "",
        "AGENT_AUTO_UPDATE": prior.get("AGENT_AUTO_UPDATE") or "1",
        "DEVICE_NAME": resolved_name or str(prior.get("DEVICE_NAME") or "").strip(),
        "JOB_SIGNING_KEY": result.get("JOB_SIGNING_KEY")
        or str(prior.get("JOB_SIGNING_KEY") or "").strip(),
    }
    write_config(config, config_path)
    print("Enrollment complete. Agent starting.")
    return config


def prompt_and_enroll(
    api_base: str,
    config_path: Path,
    agent_version: str,
    *,
    role: str = "",
    code: str | None = None,
    name: str | None = None,
    local_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enroll via --code, baked-in ENROLLMENT_CODE, or prompt on a TTY."""
    cfg = local_config or {}
    given = (code or "").strip()
    if not given:
        given = get_or_prompt_enrollment_code(cfg) or ""

    if not given:
        log.error("No enrollment code provided.")
        raise SystemExit(2)

    device_name = get_device_name(cfg, override=name)

    # Persist DEVICE_NAME (and code) before enroll so GUI/installer values survive failures.
    prior = dict(cfg)
    prior["DEVICE_NAME"] = device_name
    prior["ENROLLMENT_CODE"] = normalize_code(given)
    if (api_base or "").strip():
        prior["API_BASE"] = (api_base or "").strip().rstrip("/")
    write_config(prior, config_path)

    return enroll_with_code(
        api_base,
        config_path,
        agent_version,
        given,
        role=role,
        device_name=device_name,
        existing_config=prior,
    )
