"""Multi-source Windows software inventory with per-source evidence.

Registry alone misses MSI-only products, Store apps, and services whose binary is the only thing
carrying a version. Six collectors run instead, and every row records which collectors saw it and
the proof each one produced, so a finding can always be traced back to something on the machine.

All six run inside a single PowerShell invocation. Six separate `powershell.exe` launches cost
several seconds each on a cold machine, which is why this is one script returning one JSON object.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_COMMON = Path(__file__).resolve().parent.parent / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))
from normalize import identity_key, normalize_row, parse_version

SoftwareRow = dict[str, Any]

# Sources in descending trust order: earlier sources win on conflicting version/publisher values.
SOURCE_PRIORITY = ["registry", "msi", "appx", "kb", "services", "files"]

DEFAULT_TIMEOUT = 180

# One script, one process. Emits {"registry":[...], "msi":[...], ..., "os":{...}}.
_INVENTORY_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

function Get-RegistryApps {
  $views = @(
    @{ Hive='HKLM'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall';            View='64' },
    @{ Hive='HKLM'; Path='SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'; View='32' },
    @{ Hive='HKCU'; Path='SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall';            View='user' }
  )
  $out = New-Object System.Collections.ArrayList
  foreach ($v in $views) {
    $full = "$($v.Hive):\$($v.Path)"
    if (-not (Test-Path $full)) { continue }
    Get-ChildItem $full | ForEach-Object {
      $p = Get-ItemProperty $_.PSPath
      if (-not $p.DisplayName) { return }
      if ($p.SystemComponent -eq 1) { return }
      if ($p.ParentKeyName) { return }
      $null = $out.Add([pscustomobject]@{
        name         = "$($p.DisplayName)".Trim()
        version      = "$($p.DisplayVersion)"
        publisher    = "$($p.Publisher)"
        install_date = "$($p.InstallDate)"
        install_location = "$($p.InstallLocation)"
        registry_key = "$($v.Hive)\$($v.Path)\$($_.PSChildName)"
        arch         = $v.View
      })
    }
  }
  return $out
}

function Get-MsiApps {
  # Installer\Products carries the authoritative MSI product code without the
  # Win32_Product reconfiguration side effect.
  $roots = @(
    'HKLM:\SOFTWARE\Classes\Installer\Products',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products'
  )
  $out = New-Object System.Collections.ArrayList
  foreach ($root in $roots) {
    if (-not (Test-Path $root)) { continue }
    Get-ChildItem $root | ForEach-Object {
      $key = $_
      $p = Get-ItemProperty $key.PSPath
      $name = $p.ProductName
      if (-not $name) {
        $ip = Get-ItemProperty (Join-Path $key.PSPath 'InstallProperties')
        if ($ip) { $name = $ip.DisplayName }
      }
      if (-not $name) { return }
      $ver = $null
      $pub = $null
      $ip = Get-ItemProperty (Join-Path $key.PSPath 'InstallProperties')
      if ($ip) { $ver = $ip.DisplayVersion; $pub = $ip.Publisher }
      $null = $out.Add([pscustomobject]@{
        name         = "$name".Trim()
        version      = "$ver"
        publisher    = "$pub"
        product_code = "$($key.PSChildName)"
      })
    }
  }
  return $out
}

function Get-AppxApps {
  Get-AppxPackage -AllUsers | Where-Object { -not $_.IsFramework } | ForEach-Object {
    [pscustomobject]@{
      name           = "$($_.Name)"
      display_name   = "$($_.Name)"
      version        = "$($_.Version)"
      publisher      = "$($_.Publisher)"
      package_family = "$($_.PackageFamilyName)"
      install_path   = "$($_.InstallLocation)"
    }
  }
}

function Get-ServiceApps {
  # A service's own binary version is often the only evidence a server-side product is installed.
  Get-CimInstance Win32_Service | Where-Object { $_.PathName } | ForEach-Object {
    $raw = $_.PathName
    $exe = $null
    if ($raw -match '^\s*"([^"]+)"') { $exe = $Matches[1] }
    elseif ($raw -match '^\s*([^\s]+\.exe)') { $exe = $Matches[1] }
    if (-not $exe -or -not (Test-Path $exe)) { return }
    $info = (Get-Item $exe).VersionInfo
    if (-not $info -or -not $info.FileVersion) { return }
    [pscustomobject]@{
      name         = "$($info.ProductName)"
      fallback     = "$($_.Name)"
      version      = "$($info.ProductVersion)"
      publisher    = "$($info.CompanyName)"
      service_name = "$($_.Name)"
      exe_path      = "$exe"
      state        = "$($_.State)"
    }
  }
}

function Get-FileApps {
  # Versioned executables directly under Program Files catch installers that never wrote an
  # uninstall key. Depth is capped: a full disk walk is not worth the IO on an endpoint.
  $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ -and (Test-Path $_) }
  $out = New-Object System.Collections.ArrayList
  foreach ($root in $roots) {
    Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      $vendorDir = $_
      Get-ChildItem -Path $vendorDir.FullName -Filter *.exe -File -Recurse -Depth 2 -ErrorAction SilentlyContinue |
        Select-Object -First 4 | ForEach-Object {
          $info = $_.VersionInfo
          if (-not $info -or -not $info.ProductName -or -not $info.ProductVersion) { return }
          $null = $out.Add([pscustomobject]@{
            name      = "$($info.ProductName)"
            version   = "$($info.ProductVersion)"
            publisher = "$($info.CompanyName)"
            file_path = "$($_.FullName)"
          })
        }
    }
  }
  return $out
}

function Get-HotFixApps {
  Get-CimInstance Win32_QuickFixEngineering | ForEach-Object {
    [pscustomobject]@{
      name         = "$($_.HotFixID)"
      version      = "$($_.Description)"
      publisher    = "Microsoft"
      install_date = "$($_.InstalledOn)"
      kb           = "$($_.HotFixID)"
    }
  }
}

function Get-OsFacts {
  $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
  $hotfixes = @(Get-CimInstance Win32_QuickFixEngineering |
    ForEach-Object { [pscustomobject]@{ kb = "$($_.HotFixID)"; installed_on = "$($_.InstalledOn)" } })
  [pscustomobject]@{
    product_name   = "$($cv.ProductName)"
    display_version= "$($cv.DisplayVersion)"
    build          = "$($cv.CurrentBuildNumber)"
    ubr            = "$($cv.UBR)"
    edition        = "$($cv.EditionID)"
    installed_kbs  = $hotfixes
  }
}

$result = [pscustomobject]@{
  registry = @(Get-RegistryApps)
  msi      = @(Get-MsiApps)
  appx     = @(Get-AppxApps)
  services = @(Get-ServiceApps)
  files    = @(Get-FileApps)
  hotfix   = @(Get-HotFixApps)
  os       = Get-OsFacts
}

$json = $result | ConvertTo-Json -Depth 5 -Compress
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($env:VIZHI_INVENTORY_OUT, $json, $utf8)
"""


def _run_inventory_script(timeout: int) -> dict[str, Any] | None:
    """Run the collectors and read back their JSON.

    The result goes through a file rather than stdout: Windows PowerShell encodes redirected
    output with the console codepage, which corrupts the registered-trademark symbols that appear
    in publisher and service names, and those names are what product matching keys off.
    """
    handle, out_path = tempfile.mkstemp(prefix="vizhi-inv-", suffix=".json")
    os.close(handle)
    env = {**os.environ, "VIZHI_INVENTORY_OUT": out_path}
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _INVENTORY_PS,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logging.warning("Inventory PowerShell run failed: %s", exc)
        _unlink(out_path)
        return None

    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            output = fh.read().strip()
    except OSError as exc:
        logging.warning("Inventory output unreadable: %s", exc)
        return None
    finally:
        _unlink(out_path)

    if not output:
        logging.warning(
            "Inventory PowerShell produced no output: %s", (proc.stderr or "").strip()[:300]
        )
        return None

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logging.warning("Inventory PowerShell output was not valid JSON")
        return None
    return data if isinstance(data, dict) else None


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _as_list(value: Any) -> list[dict[str, Any]]:
    """PowerShell collapses a single-element array to a bare object; normalize that away."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "null", "n/a"}:
        return None
    return text


def _source_rank(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def _version_precision(version: str | None) -> tuple[int, tuple[int, ...]]:
    """Rank two versions of one release: more components first, then higher value."""
    parsed = parse_version(version)
    return (len(parsed), parsed)


class _Merger:
    """Accumulates rows keyed by canonical identity, unioning sources and evidence.

    Sources are split by what they can prove. `registry`, `msi` and `appx` establish that a product
    of a given version is installed, so they may create rows. `services` and `files` only observe a
    binary's embedded version, and one install directory routinely holds several executables
    stamped with different versions — letting them create rows turns one 7-Zip into four. They
    therefore corroborate an existing product where possible and only create a row for a product no
    authoritative source saw at all.
    """

    AUTHORITATIVE = {"registry", "msi", "appx", "kb"}

    def __init__(self) -> None:
        self.rows: dict[str, SoftwareRow] = {}
        # (vendor, product) -> identity keys, so a product can be found regardless of version.
        self.by_product: dict[tuple[str | None, str], list[str]] = {}
        self._deferred: list[dict[str, Any]] = []

    def add(
        self,
        *,
        source: str,
        name: str | None,
        version: str | None,
        publisher: str | None,
        evidence: dict[str, Any],
        package_type: str,
        install_date: str | None = None,
    ) -> None:
        display = _text(name)
        if not display:
            return

        normalized = normalize_row(display, version, publisher, package_type=package_type)
        entry = {
            "source": source,
            "display": display,
            "version": version,
            "publisher": publisher,
            "evidence": evidence,
            "package_type": package_type,
            "install_date": install_date,
            "normalized": normalized,
        }

        # Versionless authoritative rows and every corroborating row are resolved in result(),
        # once the full set of versioned products is known.
        if source not in self.AUTHORITATIVE or normalized.version is None:
            self._deferred.append(entry)
            return

        self._insert(entry)

    def _insert(self, entry: dict[str, Any]) -> None:
        normalized = entry["normalized"]
        key = identity_key(normalized)
        source = entry["source"]

        existing = self.rows.get(key)
        if existing is not None:
            self._merge_into(existing, entry)
            return

        self.rows[key] = {
            "name": entry["display"],
            "version": _text(entry["version"]) or "unknown",
            "publisher": _text(entry["publisher"]) or "unknown",
            "install_date": entry["install_date"],
            "package_type": entry["package_type"],
            "source": source,
            "sources": [source],
            "evidence": {source: entry["evidence"]},
            "normalized_vendor": normalized.vendor,
            "normalized_product": normalized.product,
            "normalized_version": normalized.version,
            "match_confident": normalized.confident,
        }
        self.by_product.setdefault((normalized.vendor, normalized.product), []).append(key)

    def _merge_into(self, row: SoftwareRow, entry: dict[str, Any]) -> None:
        source = entry["source"]
        if source not in row["sources"]:
            row["sources"].append(source)
        row["evidence"].setdefault(source, entry["evidence"])

        # The most trusted collector that saw this product owns its display name.
        if _source_rank(source) < _source_rank(str(row["source"])):
            row["source"] = source
            row["package_type"] = entry["package_type"]
            row["name"] = entry["display"]

        if row["publisher"] == "unknown" and _text(entry["publisher"]):
            row["publisher"] = _text(entry["publisher"])
        if row["version"] == "unknown" and _text(entry["version"]):
            row["version"] = _text(entry["version"])
        # Rows sharing an identity key are the same release seen by different collectors, which
        # report it at different precision (8.0.21 vs 8.0.21.35325). Keep the most precise so
        # advisory comparison has the build number when one collector knew it.
        incoming = entry["normalized"].version
        if incoming and _version_precision(incoming) > _version_precision(row["normalized_version"]):
            row["normalized_version"] = incoming
            if _text(entry["version"]):
                row["version"] = _text(entry["version"])
        if not row.get("install_date") and entry["install_date"]:
            row["install_date"] = entry["install_date"]

    def _best_row_for_product(self, vendor: str | None, product: str) -> SoftwareRow | None:
        """Highest known version of a product, which is the one actually in use."""
        keys = self.by_product.get((vendor, product))
        if not keys:
            return None
        candidates = [self.rows[k] for k in keys if k in self.rows]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda r: parse_version(r.get("normalized_version")),
        )

    def result(self) -> list[SoftwareRow]:
        for entry in self._deferred:
            normalized = entry["normalized"]
            target = self._best_row_for_product(normalized.vendor, normalized.product)
            if target is not None:
                self._merge_into(target, entry)
            elif entry["source"] in self.AUTHORITATIVE:
                self._insert(entry)
            else:
                # No authoritative source saw this product; a binary on disk is the only evidence
                # it exists, so keep it rather than losing the finding.
                self._insert(entry)
        self._deferred.clear()

        rows = list(self.rows.values())
        for row in rows:
            row["sources"].sort(key=_source_rank)
        rows.sort(key=lambda r: str(r.get("name", "")).lower())
        return rows


def _registry_merge_key(item: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Key for merging HKLM 32/64 views; HKCU stays separate unless exact match with HKLM."""
    name = (_text(item.get("name")) or "").lower()
    publisher = (_text(item.get("publisher")) or "").lower()
    version = (_text(item.get("version")) or "").lower()
    arch = _text(item.get("arch")) or ""
    if not name:
        return None
    if arch == "user":
        return (name, publisher, version, "user")
    return (name, publisher, version, "hklm")


def _dedupe_registry_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same DisplayName in 32/64-bit HKLM becomes one row with both arch values in evidence."""
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in items:
        key = _registry_merge_key(item)
        if key is None:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(item)
            continue
        arch = _text(item.get("arch"))
        existing_arch = _text(existing.get("arch"))
        arches: list[str] = []
        for value in (existing_arch, arch):
            if value and value not in arches:
                arches.append(value)
        if len(arches) > 1:
            existing["arch"] = ",".join(arches)
        elif arch and not existing_arch:
            existing["arch"] = arch
        for field in ("version", "publisher", "install_date", "install_location", "registry_key"):
            if not _text(existing.get(field)) and _text(item.get(field)):
                existing[field] = item.get(field)
    return list(merged.values())


def collect_windows_inventory(
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[SoftwareRow], dict[str, Any]]:
    """Collect from all Windows sources.

    Returns (rows, os_facts). os_facts carries the OS build, UBR and installed KB list, which is
    what lets the server decide whether a Windows advisory applies to this exact build.
    """
    data = _run_inventory_script(timeout)
    if data is None:
        return [], {}

    merger = _Merger()

    for item in _dedupe_registry_items(_as_list(data.get("registry"))):
        merger.add(
            source="registry",
            name=item.get("name"),
            version=item.get("version"),
            publisher=item.get("publisher"),
            install_date=_text(item.get("install_date")),
            package_type="registry",
            evidence={
                "registry_key": _text(item.get("registry_key")),
                "arch": _text(item.get("arch")),
                "install_location": _text(item.get("install_location")),
            },
        )

    for item in _as_list(data.get("msi")):
        merger.add(
            source="msi",
            name=item.get("name"),
            version=item.get("version"),
            publisher=item.get("publisher"),
            package_type="msi",
            evidence={"msi_product_code": _text(item.get("product_code"))},
        )

    for item in _as_list(data.get("appx")):
        merger.add(
            source="appx",
            name=item.get("display_name") or item.get("name"),
            version=item.get("version"),
            publisher=item.get("publisher"),
            package_type="appx",
            evidence={
                "package_family": _text(item.get("package_family")),
                "install_path": _text(item.get("install_path")),
            },
        )

    for item in _as_list(data.get("services")):
        merger.add(
            source="services",
            name=item.get("name") or item.get("fallback"),
            version=item.get("version"),
            publisher=item.get("publisher"),
            package_type="service",
            evidence={
                "service_name": _text(item.get("service_name")),
                "exe_path": _text(item.get("exe_path")),
                "state": _text(item.get("state")),
            },
        )

    for item in _as_list(data.get("hotfix")):
        kb = _text(item.get("kb")) or _text(item.get("name"))
        merger.add(
            source="kb",
            name=kb or item.get("name"),
            version=item.get("version"),
            publisher=item.get("publisher") or "Microsoft",
            install_date=_text(item.get("install_date")),
            package_type="kb",
            evidence={"kb": kb},
        )

    for item in _as_list(data.get("files")):
        merger.add(
            source="files",
            name=item.get("name"),
            version=item.get("version"),
            publisher=item.get("publisher"),
            package_type="other",
            evidence={"file_path": _text(item.get("file_path"))},
        )

    os_block = data.get("os")
    os_facts: dict[str, Any] = {}
    if isinstance(os_block, dict):
        os_facts = {
            "product_name": _text(os_block.get("product_name")),
            "display_version": _text(os_block.get("display_version")),
            "build": _text(os_block.get("build")),
            "ubr": _text(os_block.get("ubr")),
            "edition": _text(os_block.get("edition")),
            "installed_kbs": sorted(
                {
                    kb
                    for kb in (
                        _text(entry.get("kb")) for entry in _as_list(os_block.get("installed_kbs"))
                    )
                    if kb
                }
            ),
        }

    return merger.result(), os_facts
