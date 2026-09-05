"""Chocolatey optional scan channel tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import chocolatey


def test_choco_not_installed():
    with patch.object(chocolatey.shutil, "which", return_value=None):
        with patch.object(chocolatey.os.path, "isfile", return_value=False):
            cap = chocolatey.detect_chocolatey()
    assert cap["chocolatey"] is False
    assert cap["choco_path"] is None


def test_choco_list_parse():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "firefox|154.0\ngooglechrome|124.0.1234.56\n"
    with patch.object(chocolatey.subprocess, "run", return_value=mock_result):
        rows = chocolatey.scan_chocolatey_installed(r"C:\ProgramData\chocolatey\bin\choco.exe")
    assert len(rows) == 2
    assert rows[0]["choco_id"] == "firefox"
    assert rows[0]["version"] == "154.0"
    assert rows[1]["choco_id"] == "googlechrome"


def test_choco_outdated_parse():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "firefox|152.0.4|154.0|false\n7zip|23.01|24.09|false\n"
    with patch.object(chocolatey.subprocess, "run", return_value=mock_result):
        rows = chocolatey.scan_chocolatey_outdated(r"C:\ProgramData\chocolatey\bin\choco.exe")
    assert len(rows) == 2
    assert rows[0]["choco_id"] == "firefox"
    assert rows[0]["available_version"] == "154.0"
    assert rows[1]["package_name"] == "7zip"


def test_choco_outdated_pinned_skipped():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "firefox|152.0.4|154.0|true\n"
    with patch.object(chocolatey.subprocess, "run", return_value=mock_result):
        rows = chocolatey.scan_chocolatey_outdated(r"C:\ProgramData\chocolatey\bin\choco.exe")
    assert rows == []


def test_choco_outdated_exit_2_treated_as_success():
    mock_result = MagicMock()
    mock_result.returncode = 2
    mock_result.stdout = ""
    with patch.object(chocolatey.subprocess, "run", return_value=mock_result):
        rows = chocolatey.scan_chocolatey_outdated(r"C:\ProgramData\chocolatey\bin\choco.exe")
    assert rows == []


def test_choco_dedup_with_registry_app():
    manifest = [
        {"canonical_id": "mozilla-firefox", "choco_id": "firefox", "match_names": ["Mozilla Firefox"]},
    ]
    registry_rows = [
        {"source": "registry_app", "update_uid": "mozilla-firefox", "package_name": "Mozilla Firefox"},
    ]
    choco_rows = [
        {
            "source": "chocolatey",
            "update_uid": "choco:firefox",
            "choco_id": "firefox",
            "package_name": "firefox",
        }
    ]
    kept = chocolatey.dedupe_chocolatey_updates(choco_rows, registry_rows, manifest)
    assert kept == []


def test_choco_no_manifest_match():
    choco_rows = [
        {
            "source": "chocolatey",
            "update_uid": "choco:paint.net",
            "choco_id": "paint.net",
            "package_name": "paint.net",
            "available_version": "5.0.14",
        }
    ]
    kept = chocolatey.dedupe_chocolatey_updates(choco_rows, [], [])
    assert len(kept) == 1
    assert kept[0]["choco_id"] == "paint.net"


def test_choco_install_success():
    upgrade = MagicMock(returncode=0, stdout=" upgraded ", stderr="")
    verify = MagicMock(returncode=0, stdout="firefox|154.0\n", stderr="")
    with patch.object(chocolatey.subprocess, "run", side_effect=[upgrade, verify]):
        result = chocolatey.install_choco_package(
            r"C:\ProgramData\chocolatey\bin\choco.exe",
            "firefox",
            "154.0",
        )
    assert result["ok"] is True
    assert result["version_after"] == "154.0"


def test_choco_merge_inventory():
    inventory = [
        {
            "name": "Mozilla Firefox (x64 en-US)",
            "software_name": "Mozilla Firefox (x64 en-US)",
            "version": "152.0.4",
            "sources": ["registry"],
        }
    ]
    choco_rows = [{"choco_id": "firefox", "version": "152.0.4", "source": "chocolatey"}]
    manifest = [
        {
            "canonical_id": "mozilla-firefox",
            "choco_id": "firefox",
            "match_names": ["Mozilla Firefox", "Firefox"],
        }
    ]
    merged = chocolatey.merge_chocolatey_inventory(inventory, choco_rows, manifest)
    assert "chocolatey" in merged[0]["sources"]
