"""Installer verification before execution — Part 3 hardening."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)

ALLOWED_VERIFY_METHODS = frozenset({"authenticode", "sha256_pin", "dpkg_gpg", "none_allowed"})


def verify_download(
    file_path: str,
    manifest_entry: dict[str, Any],
) -> tuple[bool, str | None, str | None]:
    """
    Verify downloaded installer before execution.
    Returns (ok, error_code, error_message).
    """
    verify_method = str(manifest_entry.get("verify_method") or "").strip().lower()
    if not verify_method:
        return False, "verify_method_missing", "Manifest row missing verify_method — fail closed"

    if verify_method not in ALLOWED_VERIFY_METHODS:
        return False, "verify_method_unknown", f"Unknown verify_method: {verify_method}"

    if verify_method == "none_allowed":
        if not str(manifest_entry.get("review_note") or "").strip():
            return False, "review_note_required", "none_allowed requires review_note"
        return True, None, None

    if verify_method == "sha256_pin":
        expected = str(manifest_entry.get("expected_sha256") or "").strip().lower()
        if not expected:
            return False, "hash_pin_missing", "expected_sha256 not available from server"
        digest = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest().lower()
        if actual != expected:
            return False, "hash_mismatch", f"SHA-256 mismatch: expected {expected[:16]}… got {actual[:16]}…"
        return True, None, None

    if verify_method == "authenticode":
        publisher_hint = str(manifest_entry.get("publisher_hint") or "").strip()
        if not publisher_hint:
            return False, "publisher_hint_missing", "authenticode verify requires publisher_hint"
        script = (
            "$sig = Get-AuthenticodeSignature -FilePath "
            f"'{file_path.replace(chr(39), chr(39)+chr(39))}'; "
            "Write-Output $sig.Status; "
            "Write-Output ($sig.SignerCertificate.Subject -join '|')"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False, "signature_timeout", "Authenticode check timed out"
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if not lines:
            return False, "signature_invalid", "No Authenticode signature result"
        status = lines[0]
        subject = lines[1] if len(lines) > 1 else ""
        if status != "Valid":
            return False, "signature_invalid", f"Authenticode status: {status}"
        if publisher_hint.lower() not in subject.lower():
            return False, "publisher_mismatch", f"Certificate subject does not match {publisher_hint}"
        return True, None, None

    if verify_method == "dpkg_gpg":
        # Verified implicitly by apt without --allow-unauthenticated at install time.
        return True, None, None

    return False, "verify_method_unknown", f"Unhandled verify_method: {verify_method}"


def assert_apt_command_safe(cmd: list[str]) -> None:
    joined = " ".join(cmd).lower()
    if "--allow-unauthenticated" in joined:
        raise ValueError("apt install must never use --allow-unauthenticated")
