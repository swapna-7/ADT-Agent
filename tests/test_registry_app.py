"""registry_app scan tests."""

from __future__ import annotations

from unittest.mock import patch

from updates import _match_installed_app, scan_registry_app_channel


def test_fuzzy_match_firefox_display_name():
    installed = {
        "Mozilla Firefox (x64 en-US)": {
            "display_name": "Mozilla Firefox (x64 en-US)",
            "version_current": "152.0.4",
            "publisher": "Mozilla Corporation",
        }
    }
    entry = {
        "match_names": ["Mozilla Firefox", "Firefox"],
        "publisher_hint": "Mozilla Corporation",
    }
    matched = _match_installed_app(entry, installed)
    assert matched is not None
    assert matched["version_current"] == "152.0.4"


def test_skip_version_check_respects_cooldown():
    manifest = [
        {
            "canonical_id": "zoom-client",
            "enabled": True,
            "skip_version_check": True,
            "install_cooldown_active": True,
            "update_class": "optional",
            "category": "APPLICATION",
        }
    ]
    installed = {
        "Zoom": {
            "display_name": "Zoom",
            "version_current": "6.0.0",
            "publisher": "Zoom Video Communications",
        }
    }
    with patch("updates.fetch_app_manifest", return_value=manifest):
        with patch("updates.read_installed_apps", return_value=installed):
            rows = scan_registry_app_channel("https://x", "token", None)
    assert rows == []


def test_registry_app_uses_resolved_version():
    manifest = [
        {
            "canonical_id": "mozilla-firefox",
            "enabled": True,
            "skip_version_check": False,
            "resolved_version": "154.0",
            "match_names": ["Mozilla Firefox"],
            "publisher_hint": "Mozilla Corporation",
            "update_class": "security",
            "category": "APPLICATION",
        }
    ]
    installed = {
        "Mozilla Firefox (x64 en-US)": {
            "display_name": "Mozilla Firefox (x64 en-US)",
            "version_current": "152.0.4",
            "publisher": "Mozilla Corporation",
        }
    }
    with patch("updates.fetch_app_manifest", return_value=manifest):
        with patch("updates.read_installed_apps", return_value=installed):
            rows = scan_registry_app_channel("https://x", "token", None)
    assert len(rows) == 1
    assert rows[0]["available_version"] == "154.0"
    assert rows[0]["source"] == "registry_app"
