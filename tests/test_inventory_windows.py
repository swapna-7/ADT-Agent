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


def test_appx_package_id_merges_and_keeps_newest_version():
    merger = _Merger()
    merger.add(
        source="appx",
        name="5319275A.WhatsAppDesktop",
        version="2.2632.100.0",
        publisher="CN=24803D75-212C-471A-BC57-9EF86AB91435",
        package_type="appx",
        evidence={"package_family": "5319275A.WhatsAppDesktop_jy2w6q20qfdj"},
    )
    merger.add(
        source="appx",
        name="5319275A.WhatsAppDesktop",
        version="2.2634.101.0",
        publisher="CN=24803D75-212C-471A-BC57-9EF86AB91435",
        package_type="appx",
        evidence={"package_family": "5319275A.WhatsAppDesktop_jy2w6q20qfdj"},
    )
    rows = merger.result()
    assert len(rows) == 1
    assert rows[0]["version"].startswith("2.2634")
    assert rows[0]["normalized_product"] == "whatsapp"


def test_file_probe_dotnet_merges_into_runtime():
    merger = _Merger()
    merger.add(
        source="registry",
        name="Microsoft .NET Runtime - 8.0.30 (x64)",
        version="8.0.30.36317",
        publisher="Microsoft Corporation",
        package_type="registry",
        evidence={},
    )
    merger.add(
        source="files",
        name=".NET",
        version="8.0.30 @Commit: a83db3e0eb2defb6220e15dae2f1a0462fdbf99f",
        publisher="Microsoft Corporation",
        package_type="other",
        evidence={"file_path": "C:\\Program Files\\dotnet\\dotnet.exe"},
    )
    rows = merger.result()
    assert len(rows) == 1
    assert "files" in rows[0]["sources"]
    assert rows[0]["normalized_product"] == ".net_runtime"


def test_startup_and_scheduled_task_smoke_and_delivery_channel():
    merger = _Merger()
    merger.add(
        source="registry",
        name="Acme Agent",
        version="1.2.3",
        publisher="Acme",
        package_type="registry",
        evidence={"registry_key": "HKLM\\..."},
    )
    merger.add(
        source="startup",
        name="Acme Agent",
        version="1.2.3",
        publisher="Acme",
        package_type="startup",
        evidence={"command": "C:\\Program Files\\Acme\\agent.exe"},
    )
    merger.add(
        source="scheduled_task",
        name="Other Helper",
        version="scheduled_task",
        publisher="Task Scheduler",
        package_type="scheduled_task",
        evidence={"exe_path": "C:\\Tools\\helper.exe"},
    )
    rows = merger.result()
    by_name = {str(r["name"]): r for r in rows}
    assert "Acme Agent" in by_name
    assert "startup" in by_name["Acme Agent"]["sources"]
    assert by_name["Acme Agent"]["delivery_channel"] == "registry"
    assert "Other Helper" in by_name
    assert by_name["Other Helper"]["delivery_channel"] == "scheduled_task"
