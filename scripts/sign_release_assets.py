#!/usr/bin/env python3
"""Sign release binaries with Ed25519 (SHA-256 digest) and write latest.json."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ASSETS = {
    "windows": "adt-agent-windows.exe",
    "linux": "adt-agent-linux",
    "macos": "adt-agent-macos",
}


def load_ed25519_private_key(key_text: str) -> Ed25519PrivateKey:
    text = key_text.strip()
    if not text:
        raise ValueError("empty signing key")
    if "BEGIN" in text:
        key = load_pem_private_key(text.encode("utf-8"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("PEM key must be Ed25519")
        return key
    raw = base64.b64decode(text)
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw)
    if len(raw) == 64:
        return Ed25519PrivateKey.from_private_bytes(raw[:32])
    raise ValueError("signing key must be Ed25519 PEM or base64 (32 or 64 bytes)")


def main() -> int:
    root = Path(os.environ.get("RELEASE_ASSETS_DIR", "release-assets"))
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    key_text = os.environ.get("AGENT_SIGNING_PRIVATE_KEY", "").strip()
    private_key = load_ed25519_private_key(key_text) if key_text else None

    assets: dict[str, dict[str, str]] = {}
    for platform, name in ASSETS.items():
        path = root / name
        if not path.is_file():
            print(f"missing release asset {path}", file=sys.stderr)
            return 1
        digest = hashlib.sha256(path.read_bytes()).digest()
        sig_b64 = ""
        if private_key is not None:
            sig_b64 = base64.b64encode(private_key.sign(digest)).decode("ascii")
            (root / f"{name}.sig").write_bytes(base64.b64decode(sig_b64))
        assets[platform] = {
            "name": name,
            "sha256": digest.hex(),
            "sig_b64": sig_b64,
        }

    payload = {"version": version, "assets": assets}
    Path("latest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
