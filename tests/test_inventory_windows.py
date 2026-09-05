"""Windows inventory deduplication tests."""

from __future__ import annotations

import sys
from pathlib import Path

_WINDOWS = Path(__file__).resolve().parents[1] / "windows"
if str(_WINDOWS) not in sys.path:
    sys.path.insert(0, str(_WINDOWS))

from inventory_windows import _dedupe_registry_items, _Merger  # noqa: E402


def test_inventory_deduplication_windows():
    items = [
        {
            "name": "Google Chrome",
            "version": "120.0.6099.130",
            "publisher": "Google LLC",
            "arch": "64",
            "registry_key": "HKLM\\...\\64",
        },
        {
            "name": "Google Chrome",
            "version": "120.0.6099.130",
            "publisher": "Google LLC",
            "arch": "32",
            "registry_key": "HKLM\\...\\32",
        },
    ]
    merged = _dedupe_registry_items(items)
    assert len(merged) == 1
    assert "64" in merged[0]["arch"] and "32" in merged[0]["arch"]

    merger = _Merger()
    for item in merged:
        merger.add(
            source="registry",
            name=item["name"],
            version=item["version"],
            publisher=item["publisher"],
            package_type="registry",
            evidence={"arch": item["arch"]},
        )
    rows = merger.result()
    assert len(rows) == 1
    assert rows[0]["sources"] == ["registry"]


def test_inventory_per_user_not_deduplicated():
    items = [
        {
            "name": "My App",
            "version": "2.0.0",
            "publisher": "Vendor",
            "arch": "64",
        },
        {
            "name": "My App",
            "version": "1.0.0",
            "publisher": "Vendor",
            "arch": "user",
        },
    ]
    merged = _dedupe_registry_items(items)
    assert len(merged) == 2

    merger = _Merger()
    for item in merged:
        merger.add(
            source="registry",
            name=item["name"],
            version=item["version"],
            publisher=item["publisher"],
            package_type="registry",
            evidence={"arch": item["arch"]},
        )
    rows = merger.result()
    assert len(rows) == 2
