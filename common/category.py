"""Software category assignment — mirrors adt/lib/software-category rules."""

from __future__ import annotations

from typing import Any

CATEGORIES = frozenset(
    {
        "OS_UPDATE",
        "APPLICATION",
        "RUNTIME",
        "DEVELOPER_TOOL",
        "SYSTEM_SERVICE",
        "SECURITY_TOOL",
        "DRIVER",
    }
)

REGISTRY_CATEGORY_BY_CANONICAL: dict[str, str] = {
    "dotnet-runtime": "OS_UPDATE",
    "dotnet-sdk": "OS_UPDATE",
    "vcredist-x64": "OS_UPDATE",
    "vcredist-x86": "OS_UPDATE",
    "directx-runtime": "OS_UPDATE",
    "windows-sdk": "OS_UPDATE",
    "google-chrome": "APPLICATION",
    "google-chrome-linux": "APPLICATION",
    "mozilla-firefox": "APPLICATION",
    "mozilla-firefox-esr": "APPLICATION",
    "vlc": "APPLICATION",
    "notepadplusplus": "APPLICATION",
    "vscode": "APPLICATION",
    "vscode-linux": "APPLICATION",
    "git-for-windows": "RUNTIME",
    "7-zip": "APPLICATION",
    "zoom-client": "APPLICATION",
    "teams-desktop": "APPLICATION",
    "slack-desktop": "APPLICATION",
    "adobe-reader": "APPLICATION",
    "putty": "APPLICATION",
    "winscp": "APPLICATION",
    "nodejs": "RUNTIME",
    "python": "RUNTIME",
    "java-openjdk": "RUNTIME",
    "docker-desktop": "DEVELOPER_TOOL",
    "nginx-windows": "SYSTEM_SERVICE",
    "postgresql": "SYSTEM_SERVICE",
    "cisco-anyconnect": "SECURITY_TOOL",
    "cloudflare-warp": "SECURITY_TOOL",
    "crowdstrike-falcon": "SECURITY_TOOL",
}

LINUX_OS_PACKAGES = frozenset(
    {
        "linux-image",
        "linux-headers",
        "libc6",
        "systemd",
        "openssh-server",
        "openssl",
        "kernel",
    }
)

LINUX_SERVICE_PACKAGES = frozenset({"nginx", "postgresql", "mysql-server", "apache2"})


def assign_category(row: dict[str, Any]) -> str:
    source = str(row.get("source") or row.get("package_type") or "").lower()
    canonical = str(row.get("canonical_id") or row.get("update_uid") or row.get("package_name") or "")
    category = row.get("category")
    if isinstance(category, str) and category in CATEGORIES:
        return category

    if source in {"npm_global", "pip_global"}:
        return "DEVELOPER_TOOL"

    if source == "service":
        return "SYSTEM_SERVICE"

    if source == "kb":
        return "OS_UPDATE"

    if source == "appx":
        return "APPLICATION"

    if source == "registry_app":
        mapped = REGISTRY_CATEGORY_BY_CANONICAL.get(canonical)
        return mapped or "APPLICATION"

    if source == "windows_update":
        categories = row.get("categories") or row.get("wua_categories") or []
        if isinstance(categories, list):
            cats = [str(c).lower() for c in categories]
            if any("driver" in c for c in cats):
                return "DRIVER"
        severity = str(row.get("MsrcSeverity") or row.get("msrc_severity") or "").lower()
        return "OS_UPDATE"

    if source in {"apt", "dnf"}:
        pkg = str(row.get("package_name") or row.get("software_name") or "").lower()
        repo = str(row.get("repo") or row.get("evidence", {}).get("repo") or "").lower()
        if "security" in repo or row.get("is_security"):
            return "OS_UPDATE"
        if any(pkg.startswith(p) for p in LINUX_OS_PACKAGES):
            return "OS_UPDATE"
        if pkg in LINUX_SERVICE_PACKAGES:
            return "SYSTEM_SERVICE"
        return "APPLICATION"

    if source in {"snap", "flatpak"}:
        return "APPLICATION"

    return "APPLICATION"
