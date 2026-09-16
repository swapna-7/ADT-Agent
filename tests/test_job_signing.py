"""HMAC job signature verification tests."""

from __future__ import annotations

import sys
from pathlib import Path

_COMMON = Path(__file__).resolve().parents[1] / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from job_signing import sign_patch_job, verify_patch_job_signature  # noqa: E402


FIELDS = dict(
    job_id="11111111-1111-1111-1111-111111111111",
    endpoint_id="22222222-2222-2222-2222-222222222222",
    source="chocolatey",
    update_uid="choco:googlechrome",
    package_name="googlechrome",
    target_version="131.0.0",
)


def test_valid_signature():
    key = "test-signing-key-abcdefghijklmnopqrstuvwxyz"
    sig = sign_patch_job(key, **FIELDS)
    assert verify_patch_job_signature(key, sig, **FIELDS) is True


def test_tampered_update_uid_rejected():
    key = "test-signing-key-abcdefghijklmnopqrstuvwxyz"
    sig = sign_patch_job(key, **FIELDS)
    assert (
        verify_patch_job_signature(
            key,
            sig,
            **{**FIELDS, "update_uid": "choco:evil"},
        )
        is False
    )


def test_cross_endpoint_key_rejected():
    sig = sign_patch_job("endpoint-a-key", **FIELDS)
    assert verify_patch_job_signature("endpoint-b-key", sig, **FIELDS) is False


def test_missing_key_fail_closed():
    assert verify_patch_job_signature(None, "abc", **FIELDS) is False
    assert verify_patch_job_signature("key", None, **FIELDS) is False
