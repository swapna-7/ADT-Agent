"""HMAC verification for patch jobs — fail closed if signature missing/invalid."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_job_message(
    *,
    job_id: str,
    endpoint_id: str,
    source: str,
    update_uid: str,
    package_name: str | None,
    target_version: str | None,
) -> str:
    payload = {
        "job_id": job_id,
        "endpoint_id": endpoint_id,
        "source": source,
        "update_uid": update_uid,
        "package_name": package_name or "",
        "target_version": target_version or "",
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def sign_patch_job(key: str, **fields: Any) -> str:
    message = canonical_job_message(
        job_id=str(fields["job_id"]),
        endpoint_id=str(fields["endpoint_id"]),
        source=str(fields["source"]),
        update_uid=str(fields["update_uid"]),
        package_name=fields.get("package_name"),
        target_version=fields.get("target_version"),
    )
    digest = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    # base64url without padding
    import base64

    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_patch_job_signature(
    key: str | None,
    signature: str | None,
    *,
    job_id: str,
    endpoint_id: str,
    source: str,
    update_uid: str,
    package_name: str | None,
    target_version: str | None,
) -> bool:
    if not key or not signature:
        return False
    expected = sign_patch_job(
        key,
        job_id=job_id,
        endpoint_id=endpoint_id,
        source=source,
        update_uid=update_uid,
        package_name=package_name,
        target_version=target_version,
    )
    try:
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False
