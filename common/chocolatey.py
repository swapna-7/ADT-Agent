"""Optional Chocolatey scan channel — discovery and supplementary updates only."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any

log = logging.getLogger(__name__)

CHOCO_FIXED = r"C:\ProgramData\chocolatey\bin\choco.exe"


def detect_chocolatey() -> dict[str, Any]:
    """Return chocolatey capability info for patch_capabilities."""
    choco_path = shutil.which("choco") or (CHOCO_FIXED if os.path.isfile(CHOCO_FIXED) else None)
    if not choco_path:
        return {"chocolatey": False, "choco_path": None, "choco_version": None}
    version: str | None = None
    try:
        result = subprocess.run(
            [choco_path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            version = (result.stdout or "").strip() or None
    except Exception:
        version = None
    return {
        "chocolatey": bool(version),
        "choco_path": choco_path,
        "choco_version": version,
    }


def scan_chocolatey_installed(choco_path: str) -> list[dict[str, Any]]:
    """Installed packages from choco list --local-only."""
    try:
        result = subprocess.run(
            [choco_path, "list", "--local-only", "--limit-output", "--no-color"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        log.warning("choco list failed: %s", exc)
        return []

    rows: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        pkg_id, version = parts[0].strip(), parts[1].strip()
        if not pkg_id or not version:
            continue
        rows.append(
            {
                "choco_id": pkg_id,
                "version": version,
                "source": "chocolatey",
            }
        )
    return rows


def scan_chocolatey_outdated(choco_path: str) -> list[dict[str, Any]]:
    """Packages Chocolatey reports as outdated."""
    try:
        result = subprocess.run(
            [choco_path, "outdated", "--limit-output", "--no-color"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        log.warning("choco outdated failed: %s", exc)
        return []

    if result.returncode not in (0, 2):
        log.warning(
            "choco outdated exit %s: %s",
            result.returncode,
            (result.stderr or "")[:200],
        )
        return []

    rows: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        pkg_id = parts[0].strip()
        version_cur = parts[1].strip()
        version_avail = parts[2].strip()
        pinned = parts[3].strip().lower() == "true" if len(parts) > 3 else False
        if pinned or not pkg_id or not version_avail:
            continue
        rows.append(
            {
                "source": "chocolatey",
                "update_uid": f"choco:{pkg_id}",
                "title": f"{pkg_id} {version_avail}",
                "package_name": pkg_id,
                "choco_id": pkg_id,
                "kb_ids": [],
                "cve_ids": [],
                "update_class": "optional",
                "category": "APPLICATION",
                "requires_reboot": False,
                "size_bytes": 0,
                "current_version": version_cur,
                "available_version": version_avail,
                "evidence": {"install_method": "chocolatey"},
            }
        )
    return rows


def _manifest_choco_index(manifest: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for entry in manifest:
        choco_id = str(entry.get("choco_id") or "").strip().lower()
        if choco_id:
            index[choco_id] = entry
    return index


def _registry_app_canonicals(registry_rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in registry_rows:
        uid = str(row.get("update_uid") or "")
        if uid:
            out.add(uid)
    return out


def dedupe_chocolatey_updates(
    choco_rows: list[dict[str, Any]],
    registry_rows: list[dict[str, Any]],
    manifest: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Drop choco rows when registry_app already covers the same manifest app."""
    if not choco_rows:
        return []
    manifest = manifest or []
    choco_index = _manifest_choco_index(manifest)
    registry_canonicals = _registry_app_canonicals(registry_rows)
    kept: list[dict[str, Any]] = []
    for row in choco_rows:
        pkg_id = str(row.get("choco_id") or row.get("package_name") or "").strip().lower()
        entry = choco_index.get(pkg_id)
        if entry and str(entry.get("canonical_id") or "") in registry_canonicals:
            continue
        kept.append(row)
    return kept


def _name_matches_manifest(display_name: str, entry: dict[str, Any]) -> bool:
    name_lower = display_name.lower()
    for match_name in entry.get("match_names") or []:
        needle = str(match_name).strip().lower()
        if not needle:
            continue
        if name_lower == needle or needle in name_lower or name_lower.startswith(needle):
            return True
    return False


def _merge_registry_and_choco_row(
    registry_row: dict[str, Any],
    choco_row: dict[str, Any],
    choco_id: str,
) -> dict[str, Any]:
    """Keep the higher version as primary; union sources and annotate delivery."""
    from version_compare import compare_versions

    merged = dict(registry_row)
    reg_ver = str(registry_row.get("version") or "0")
    choco_ver = str(choco_row.get("version") or "0")
    try:
        choco_is_primary = compare_versions(choco_ver, reg_ver) > 0
    except Exception:
        choco_is_primary = False

    if choco_is_primary:
        merged["version"] = choco_row.get("version")
        merged["source"] = "chocolatey"
        merged["package_type"] = "chocolatey"
        merged["delivery_channel"] = "chocolatey"
    else:
        merged["delivery_channel"] = str(
            registry_row.get("delivery_channel")
            or registry_row.get("source")
            or "registry"
        )

    sources = list(registry_row.get("sources") or [])
    if registry_row.get("source") and registry_row["source"] not in sources:
        sources.append(str(registry_row["source"]))
    if "chocolatey" not in sources:
        sources.append("chocolatey")
    merged["sources"] = sources

    evidence = dict(registry_row.get("evidence") or {})
    evidence["choco_id"] = choco_id
    merged["evidence"] = evidence
    return merged


def merge_chocolatey_inventory(
    inventory_rows: list[dict[str, Any]],
    choco_rows: list[dict[str, Any]],
    manifest: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Enrich registry inventory rows with chocolatey source or add choco-only rows."""
    if not choco_rows:
        return inventory_rows

    manifest = manifest or []
    choco_by_id = {str(r["choco_id"]).lower(): r for r in choco_rows if r.get("choco_id")}
    choco_index = _manifest_choco_index(manifest)

    rows = [dict(r) for r in inventory_rows]

    for choco_id, choco_row in choco_by_id.items():
        entry = choco_index.get(choco_id)
        matched_idx: int | None = None

        if entry:
            for idx, row in enumerate(rows):
                software_name = str(
                    row.get("software_name") or row.get("name") or row.get("display_name") or ""
                )
                if _name_matches_manifest(software_name, entry):
                    matched_idx = idx
                    break

        if matched_idx is not None:
            rows[matched_idx] = _merge_registry_and_choco_row(
                rows[matched_idx],
                choco_row,
                choco_id,
            )
            continue

        display = choco_id.replace(".", " ").replace("-", " ").title()
        rows.append(
            {
                "name": display,
                "software_name": display,
                "version": choco_row.get("version"),
                "publisher": "Unknown (Chocolatey)",
                "package_type": "chocolatey",
                "source": "chocolatey",
                "delivery_channel": "chocolatey",
                "sources": ["chocolatey"],
                "evidence": {"choco_id": choco_id},
                "category": str(entry.get("category") if entry else "APPLICATION"),
            }
        )

    return rows


def enrich_inventory_snapshot(
    rows: list[dict[str, Any]],
    api_base: str | None,
    device_token: str | None,
) -> list[dict[str, Any]]:
    """Merge chocolatey installed list into an inventory snapshot."""
    cap = detect_chocolatey()
    choco_path = cap.get("choco_path")
    if not cap.get("chocolatey") or not choco_path:
        return rows
    choco_rows = scan_chocolatey_installed(str(choco_path))
    if not choco_rows:
        return rows
    manifest: list[dict[str, Any]] = []
    if api_base and device_token:
        try:
            from updates import fetch_app_manifest

            manifest = fetch_app_manifest(api_base, device_token) or []
        except Exception as exc:
            log.warning("manifest fetch for choco inventory merge failed: %s", exc)
    normalized = [
        {**r, "software_name": r.get("software_name") or r.get("name")}
        for r in rows
    ]
    merged = merge_chocolatey_inventory(normalized, choco_rows, manifest)
    for row in merged:
        if row.get("software_name") and not row.get("name"):
            row["name"] = row["software_name"]
    return merged


_CHOCO_ID_RE = re.compile(r"^[a-z0-9][\w.-]{0,99}$", re.IGNORECASE)
_CHOCO_VERSION_RE = re.compile(r"^[\w.+-]{1,64}$")


def _safe_choco_id(value: str) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned or not _CHOCO_ID_RE.fullmatch(cleaned):
        return None
    return cleaned.lower()


def _safe_choco_version(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if not _CHOCO_VERSION_RE.fullmatch(cleaned):
        return None
    return cleaned


def install_choco_package(
    choco_path: str,
    choco_id: str,
    version_available: str | None,
    manifest_cache: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Upgrade a package via Chocolatey using argv-only invocation (no shell)."""
    if manifest_cache is None:
        return {
            "exit_code": -1,
            "ok": False,
            "error_code": "manifest_unavailable",
            "error_message": (
                "app_version_manifest cache unavailable; cannot validate choco_id"
            ),
            "version_after": None,
        }

    if not manifest_cache:
        return {
            "exit_code": -1,
            "ok": False,
            "error_code": "manifest_cache_empty",
            "error_message": (
                "app_version_manifest cache is empty; cannot validate choco_id — install blocked"
            ),
            "version_after": None,
        }

    safe_id = _safe_choco_id(choco_id)
    if not safe_id:
        return {
            "exit_code": 1,
            "ok": False,
            "error_code": "unknown_canonical_id",
            "error_message": "Chocolatey package id is missing or malformed.",
            "version_after": None,
        }

    allowed_ids = {
        str(entry.get("choco_id") or "").strip().lower()
        for entry in manifest_cache
        if str(entry.get("choco_id") or "").strip()
    }
    if safe_id not in allowed_ids:
        return {
            "exit_code": -1,
            "ok": False,
            "error_code": "unknown_canonical_id",
            "error_message": (
                f"choco_id '{safe_id}' not in manifest allowlist — install blocked"
            ),
            "version_after": None,
        }

    safe_version = _safe_choco_version(version_available)
    if version_available and str(version_available).strip() and not safe_version:
        return {
            "exit_code": 1,
            "ok": False,
            "error_code": "invalid_target_version",
            "error_message": "Chocolatey target version is malformed.",
            "version_after": None,
        }
    cmd = [
        choco_path,
        "upgrade",
        safe_id,
        "--yes",
        "--no-color",
        "--limit-output",
        "--fail-on-error-output",
    ]
    if safe_version:
        cmd.extend(["--version", safe_version, "--ignore-checksums"])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "ok": False,
            "error_code": "timeout",
            "error_message": "choco upgrade timed out after 600s",
            "version_after": None,
        }
    except Exception as exc:
        return {
            "exit_code": -1,
            "ok": False,
            "error_code": "subprocess_error",
            "error_message": str(exc),
            "version_after": None,
        }

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    success = proc.returncode in (0, 2)

    version_after: str | None = None
    try:
        verify = subprocess.run(
            [
                choco_path,
                "list",
                "--local-only",
                "--limit-output",
                "--exact",
                safe_id,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
        for line in (verify.stdout or "").splitlines():
            if "|" in line and line.split("|")[0].strip().lower() == safe_id:
                version_after = line.split("|", 1)[1].strip()
                break
    except Exception:
        pass

    reboot_required = "reboot" in stdout.lower() or "restart" in stdout.lower()

    return {
        "exit_code": proc.returncode,
        "stdout": stdout[-2000:],
        "stderr": stderr[-1000:],
        "reboot_required": reboot_required,
        "version_after": version_after,
        "ok": success,
        "error_code": None if success else "choco_upgrade_failed",
        "error_message": stderr[:500] if not success else None,
    }
