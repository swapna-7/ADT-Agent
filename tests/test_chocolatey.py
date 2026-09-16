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
    manifest = [{"choco_id": "firefox", "canonical_id": "mozilla-firefox"}]
    with patch.object(chocolatey.subprocess, "run", side_effect=[upgrade, verify]):
        result = chocolatey.install_choco_package(
            r"C:\ProgramData\chocolatey\bin\choco.exe",
            "firefox",
            "154.0",
            manifest_cache=manifest,
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


def test_install_rejects_empty_manifest_without_subprocess():
    with patch.object(chocolatey.subprocess, "run") as run:
        result = chocolatey.install_choco_package(
            r"C:\ProgramData\chocolatey\bin\choco.exe",
            "firefox",
            "1.0.0",
            manifest_cache=[],
        )
    run.assert_not_called()
    assert result["error_code"] == "manifest_cache_empty"


def test_install_rejects_malformed_id_without_subprocess():
    manifest = [{"choco_id": "firefox"}]
    with patch.object(chocolatey.subprocess, "run") as run:
        result = chocolatey.install_choco_package(
            r"C:\ProgramData\chocolatey\bin\choco.exe",
            "bad;id && calc",
            "1.0.0",
            manifest_cache=manifest,
        )
    run.assert_not_called()
    assert result["error_code"] == "unknown_canonical_id"


def test_install_argv_only_allowlisted_id():
    mock_upgrade = MagicMock()
    mock_upgrade.returncode = 0
    mock_upgrade.stdout = "Chocolatey upgraded 1/1 packages.\n"
    mock_upgrade.stderr = ""
    mock_list = MagicMock()
    mock_list.returncode = 0
    mock_list.stdout = "googlechrome|131.0.0\n"
    with patch.object(
        chocolatey.subprocess,
        "run",
        side_effect=[mock_upgrade, mock_list],
    ) as run:
        result = chocolatey.install_choco_package(
            r"C:\ProgramData\chocolatey\bin\choco.exe",
            "GoogleChrome",
            "131.0.0",
            manifest_cache=[{"choco_id": "googlechrome"}],
        )
    assert result["ok"] is True
    upgrade_cmd = run.call_args_list[0].args[0]
    assert upgrade_cmd[1] == "upgrade"
    assert upgrade_cmd[2] == "googlechrome"
    assert "--version" in upgrade_cmd
    assert "131.0.0" in upgrade_cmd
    assert run.call_args_list[0].kwargs.get("shell") is False


def test_merge_choco_annotates_registry_single_row():
    inventory = [
        {
            "name": "Google Chrome",
            "software_name": "Google Chrome",
            "version": "130.0",
            "publisher": "Google LLC",
            "source": "registry",
            "sources": ["registry"],
            "evidence": {},
        }
    ]
    choco_rows = [{"choco_id": "googlechrome", "version": "130.0"}]
    manifest = [
        {
            "canonical_id": "google-chrome",
            "choco_id": "googlechrome",
            "match_names": ["Google Chrome"],
            "category": "BROWSER",
        }
    ]
    merged = chocolatey.merge_chocolatey_inventory(inventory, choco_rows, manifest)
    assert len(merged) == 1
    assert "chocolatey" in merged[0]["sources"]
    assert merged[0]["evidence"]["choco_id"] == "googlechrome"
    assert merged[0]["delivery_channel"] == "registry"


def test_merge_choco_higher_version_becomes_primary():
    inventory = [
        {
            "name": "Google Chrome",
            "software_name": "Google Chrome",
            "version": "152.0.4",
            "publisher": "Google LLC",
            "source": "registry",
            "sources": ["registry"],
            "evidence": {},
        }
    ]
    choco_rows = [{"choco_id": "googlechrome", "version": "154.0"}]
    manifest = [
        {
            "canonical_id": "google-chrome",
            "choco_id": "googlechrome",
            "match_names": ["Google Chrome"],
        }
    ]
    merged = chocolatey.merge_chocolatey_inventory(inventory, choco_rows, manifest)
    assert len(merged) == 1
    assert merged[0]["version"] == "154.0"
    assert merged[0]["delivery_channel"] == "chocolatey"
    assert "registry" in merged[0]["sources"]
    assert "chocolatey" in merged[0]["sources"]