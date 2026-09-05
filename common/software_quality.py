"""Shared filters and categorization for installed-software inventory rows."""

from __future__ import annotations

import re
from typing import Literal

PackageType = Literal["gui", "dpkg", "rpm", "snap", "flatpak", "registry", "other"]

_GUID_RE = re.compile(
    r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$"
)
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")
_WINDOWS_UPDATE_RE = re.compile(
    r"^(?:KB\d+|Update for|Security Update|Hotfix for|Service Pack|Definition Update)",
    re.IGNORECASE,
)
_LINUX_LIB_RE = re.compile(r"^(?:lib|python3?|node|g[lib]|linux-(?:image|headers|modules))", re.IGNORECASE)


def is_guid_name(name: str) -> bool:
    return bool(_GUID_RE.match(name.strip()))


def is_windows_update_name(name: str) -> bool:
    return bool(_WINDOWS_UPDATE_RE.match(name.strip()))


def is_plausible_software_row(
    name: str,
    version: str | None,
    publisher: str | None,
    *,
    package_type: str | None = None,
    source: str | None = None,
) -> bool:
    """Return False for rows that should not be upserted or shown as installed apps."""
    n = (name or "").strip()
    if len(n) < 2:
        return False
    if is_guid_name(n):
        return False
    if _PUNCT_ONLY_RE.match(n):
        return False
    if is_windows_update_name(n):
        return False

    v = (version or "").strip().lower()
    p = (publisher or "").strip().lower()

    # Filesystem fallback fingerprint: folder name + fake version
    if source == "filesystem" or (v == "installed" and p in ("unknown", "", "—", "-")):
        return False
    if v == "installed" and p in ("unknown", "", "—", "-"):
        return False

    # Skip bare library packages from dpkg/rpm unless explicitly typed otherwise
    if package_type in ("dpkg", "rpm") and _LINUX_LIB_RE.match(n):
        return False

    return True


def categorize_linux_package(name: str, *, from_snap: bool = False, from_flatpak: bool = False) -> PackageType:
    if from_snap:
        return "snap"
    if from_flatpak:
        return "flatpak"
    n = name.strip().lower()
    if n.endswith(".desktop") or "/" in name:
        return "gui"
    return "dpkg"


def filter_software_rows(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    """Drop implausible rows; preserve package_type / source when present."""
    out: list[dict[str, str | None]] = []
    for row in rows:
        name = str(row.get("name") or "")
        version = row.get("version")
        publisher = row.get("publisher")
        package_type = row.get("package_type")
        source = row.get("source")
        if is_plausible_software_row(
            name,
            str(version) if version is not None else None,
            str(publisher) if publisher is not None else None,
            package_type=str(package_type) if package_type else None,
            source=str(source) if source else None,
        ):
            out.append(row)
    return out
