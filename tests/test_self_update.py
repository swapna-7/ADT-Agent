"""Self-update version compare and replace helpers. Network is not exercised here."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from self_update import (  # noqa: E402
    auto_update_enabled,
    backup_path,
    cleanup_previous_backup,
    download_and_verify,
    is_newer,
    parse_semver,
    read_auto_update_flag,
    replace_installed_binary,
    should_self_update,
)
from version import AGENT_VERSION  # noqa: E402


def test_version_file_matches_module() -> None:
    file_ver = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="utf-8").strip()
    assert file_ver == AGENT_VERSION


def test_parse_semver_strips_v_and_pre_release() -> None:
    assert parse_semver("v2.1.0") == (2, 1, 0)
    assert parse_semver("2.1.0-rc1") == (2, 1, 0)
    assert parse_semver("2") == (2,)


def test_is_newer() -> None:
    assert is_newer("2.1.0", "2.0.0")
    assert is_newer("2.1.1", "2.1.0")
    assert not is_newer("2.1.0", "2.1.0")
    assert not is_newer("2.0.9", "2.1.0")


def test_auto_update_defaults_on() -> None:
    assert auto_update_enabled(None)
    assert auto_update_enabled("")
    assert auto_update_enabled("1")
    assert not auto_update_enabled("0")
    assert not auto_update_enabled("false")


def test_auto_update_flag_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_AUTO_UPDATE", "0")
    assert read_auto_update_flag({"AGENT_AUTO_UPDATE": "1"}, {"AGENT_AUTO_UPDATE": "0"}) is True
    assert read_auto_update_flag({}, {"AGENT_AUTO_UPDATE": "1"}) is True
    assert read_auto_update_flag({}, {}) is False
    monkeypatch.delenv("AGENT_AUTO_UPDATE", raising=False)
    assert read_auto_update_flag({}, {}) is True


def test_auto_update_flag_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_AUTO_UPDATE", raising=False)
    local_config: dict[str, str] = {"AGENT_AUTO_UPDATE": "0"}
    env: dict[str, str] = {}
    assert read_auto_update_flag(local_config, env) is False
    local_config["AGENT_AUTO_UPDATE"] = "1"
    assert read_auto_update_flag(local_config, env) is True


def test_should_self_update_skips_unfrozen() -> None:
    assert should_self_update(frozen=False, install_exe=Path(sys.executable)) is False


def test_backup_path_exe_and_unix() -> None:
    # PosixPath treats backslashes as literal characters, so use platform-native paths.
    if sys.platform == "win32":
        assert (
            backup_path(Path(r"C:\Program Files\ADT Agent\adt-agent.exe")).name
            == "adt-agent.old"
        )
    else:
        assert backup_path(Path("/opt/vizhi-agent/adt-agent.exe")).name == "adt-agent.old"
    assert backup_path(Path("/opt/vizhi-agent/adt-agent")).name == "adt-agent.old"


def test_replace_installed_binary_does_not_touch_env(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env_file = install_dir / ".env"
    config = tmp_path / "config.json"
    env_file.write_text("KEEP=1\n", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    installed = install_dir / "adt-agent"
    installed.write_bytes(b"old-binary")
    staging = tmp_path / "update" / "adt-agent"
    staging.parent.mkdir()
    staging.write_bytes(b"new-binary")

    assert replace_installed_binary(staging, installed)
    assert installed.read_bytes() == b"new-binary"
    assert env_file.read_text(encoding="utf-8") == "KEEP=1\n"
    assert config.read_text(encoding="utf-8") == "{}"
    backup = backup_path(installed)
    assert backup.is_file()
    assert backup.read_bytes() == b"old-binary"
    cleanup_previous_backup(installed)
    assert not backup.exists()


def test_download_and_verify_compares_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"vizhi-agent-update-payload"
    expected = hashlib.sha256(payload).hexdigest()

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def iter_content(self, chunk_size: int = 0):
            yield payload

    monkeypatch.setattr("self_update.requests.get", lambda *args, **kwargs: FakeResponse())

    dest = tmp_path / "staging" / "adt-agent.exe"
    assert download_and_verify(
        "https://vizhi.rcsaware.com/api/agent/update/download?platform=windows",
        dest,
        expected,
        device_token="test-token",
    )
    assert dest.read_bytes() == payload


def test_sha256_of_replaced_file(tmp_path: Path) -> None:
    payload = b"vizhi-agent-payload"
    staging = tmp_path / "new.bin"
    dest = tmp_path / "adt-agent"
    dest.write_bytes(b"old")
    staging.write_bytes(payload)
    assert replace_installed_binary(staging, dest)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert digest == hashlib.sha256(payload).hexdigest()
