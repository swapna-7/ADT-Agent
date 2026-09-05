"""Install verification tests — Part 3."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from install_verify import assert_apt_command_safe, verify_download


def test_install_aborts_on_hash_mismatch() -> None:
    entry = {
        "verify_method": "sha256_pin",
        "expected_sha256": "abc123",
    }
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"corrupted")
        path = tmp.name
    ok, code, _msg = verify_download(path, entry)
    Path(path).unlink(missing_ok=True)
    assert ok is False
    assert code == "hash_mismatch"


def test_install_aborts_on_hash_match() -> None:
    payload = b"good installer bytes"
    digest = hashlib.sha256(payload).hexdigest()
    entry = {"verify_method": "sha256_pin", "expected_sha256": digest}
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(payload)
        path = tmp.name
    ok, code, _msg = verify_download(path, entry)
    Path(path).unlink(missing_ok=True)
    assert ok is True
    assert code is None


def test_manifest_entry_without_verify_method_fails_closed() -> None:
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"x")
        path = tmp.name
    ok, code, _msg = verify_download(path, {})
    Path(path).unlink(missing_ok=True)
    assert ok is False
    assert code == "verify_method_missing"


def test_install_aborts_on_signature_mismatch() -> None:
    entry = {
        "verify_method": "authenticode",
        "publisher_hint": "Expected Publisher",
    }
    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as tmp:
        tmp.write(b"fake")
        path = tmp.name

    fake_proc = type("P", (), {"stdout": "NotSigned\nWrong Publisher", "returncode": 0})()
    with patch("install_verify.subprocess.run", return_value=fake_proc):
        ok, code, _msg = verify_download(path, entry)
    Path(path).unlink(missing_ok=True)
    assert ok is False
    assert code in {"signature_invalid", "publisher_mismatch"}


def test_apt_command_never_allows_unauthenticated() -> None:
    with pytest.raises(ValueError):
        assert_apt_command_safe(["apt-get", "install", "-y", "--allow-unauthenticated", "pkg.deb"])


def test_msi_exit_3010_marks_reboot_pending_not_failed() -> None:
    from updates import _interpret_install_exit

    success, reboot = _interpret_install_exit(3010, "auto", False)
    assert success is True
    assert reboot is True


def test_requires_reboot_always_overrides_exit_0() -> None:
    from updates import _interpret_install_exit

    success, reboot = _interpret_install_exit(0, "always", False)
    assert success is True
    assert reboot is True
