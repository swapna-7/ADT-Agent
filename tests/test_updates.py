"""Update classification helpers.

Winget parsing is retired; Windows classification uses MsrcSeverity via WUA COM.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from updates import (  # noqa: E402
    _msrc_to_class,
    _wua_com_error_detail,
    apply_update,
    apply_windows_update,
    detect_linux_distro,
    has_root,
)


@pytest.mark.parametrize(
    "severity, expected_class, expected_security",
    [
        ("Critical", "critical", True),
        ("critical", "critical", True),
        ("Important", "security", True),
        ("important", "security", True),
        ("Moderate", "optional", True),
        ("Low", "optional", True),
        (None, "optional", False),
        ("", "optional", False),
        ("Unspecified", "optional", False),
    ],
)
def test_msrc_severity_maps_to_update_class(
    severity: str | None,
    expected_class: str,
    expected_security: bool,
) -> None:
    assert _msrc_to_class(severity) == (expected_class, expected_security)


def test_winget_apply_is_retired() -> None:
    result = apply_update("winget", "Google.Chrome")
    assert result["ok"] is False
    assert result["error_code"] == "winget_retired"


def test_unknown_source_fails() -> None:
    with patch("updates.has_root", return_value=True):
        result = apply_update("mystery", "x")
    assert result["ok"] is False
    assert result["error_code"] == "unknown_source"


def test_apply_without_root_is_blocked() -> None:
    with patch("updates.has_root", return_value=False):
        result = apply_update("apt", "openssl")
    assert result["error_code"] == "blocked_no_root"


def test_detect_linux_distro_debian(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n', encoding="utf-8")

    def fake_open(path, *args, **kwargs):
        if str(path) == "/etc/os-release":
            return release.open(*args, **kwargs)
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)
    distro_id, manager = detect_linux_distro()
    assert distro_id == "ubuntu"
    assert manager == "apt"


def test_detect_linux_distro_rhel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = tmp_path / "os-release"
    release.write_text('ID=rhel\nID_LIKE="fedora"\nVERSION_ID="9.4"\n', encoding="utf-8")

    def fake_open(path, *args, **kwargs):
        if str(path) == "/etc/os-release":
            return release.open(*args, **kwargs)
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(
        "updates.shutil.which",
        lambda name: "/usr/bin/dnf" if name == "dnf" else None,
    )
    distro_id, manager = detect_linux_distro()
    assert distro_id == "rhel"
    assert manager == "dnf"


def test_has_root_is_callable() -> None:
    assert callable(has_root)


def test_wua_com_error_includes_hresult() -> None:
    exc = SimpleNamespace(args=(0x80240017,), hresult=0x80240017)
    detail = _wua_com_error_detail(exc)  # type: ignore[arg-type]
    assert "HRESULT=0x80240017" in detail


def test_apply_windows_update_honors_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "win32com", MagicMock())
    monkeypatch.setitem(sys.modules, "win32com.client", MagicMock())

    def hang(*_args: object, **_kwargs: object) -> dict[str, object]:
        time.sleep(2)
        return {"ok": True}

    monkeypatch.setattr("updates._apply_windows_update_com", hang)
    result = apply_windows_update("11111111-1111-1111-1111-111111111111", timeout=0)
    assert result["ok"] is False
    assert result["error_code"] == "wua_timeout"
    assert "timed out" in (result["error_message"] or "")


def test_apply_windows_update_returns_worker_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "win32com", MagicMock())
    monkeypatch.setitem(sys.modules, "win32com.client", MagicMock())

    def instant(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "result_code=2",
            "stderr": "",
            "reboot_required": False,
            "version_after": None,
            "error_code": None,
            "error_message": None,
            "not_applicable": False,
        }

    monkeypatch.setattr("updates._apply_windows_update_com", instant)
    result = apply_windows_update("11111111-1111-1111-1111-111111111111", timeout=5)
    assert result["ok"] is True
    assert result["stdout"] == "result_code=2"
