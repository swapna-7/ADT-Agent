"""Guarantees the vulnerability engine depends on.

Every assertion here is a case that previously produced a wrong verdict: one product split across
several rows, or two separately-versioned components collapsed into one. Both make the engine
compare an advisory against the wrong version.
"""

from __future__ import annotations

import pytest

from normalize import (  # noqa: E402
    compare_distro_versions,
    compare_versions,
    identity_key,
    normalize_row,
    normalize_vendor,
    normalize_version,
    parse_version,
)


@pytest.mark.parametrize(
    "name, publisher",
    [
        ("Google Chrome", "Google LLC"),
        ("Google Chrome (64-bit)", "Google Inc."),
        ("Chrome", None),
    ],
)
def test_one_product_never_becomes_three(name: str, publisher: str | None) -> None:
    result = normalize_row(name, "151.0.7922.174", publisher)
    assert (result.vendor, result.product) == ("google", "chrome")
    assert result.confident


def test_same_release_from_different_collectors_shares_one_identity() -> None:
    """The registry reports 8.0.21.35325 and MSI its own packed 64.84.40925 for one install."""
    registry = normalize_row("Microsoft .NET Runtime - 8.0.21 (x64)", "8.0.21.35325", "Microsoft")
    msi = normalize_row("Microsoft .NET Runtime - 8.0.21 (x64)", "64.84.40925", "Microsoft")

    assert identity_key(registry) == identity_key(msi)
    # The build number is still available for advisory comparison.
    assert registry.version == "8.0.21.35325"
    # The MSI packed version is discarded rather than believed.
    assert msi.version == "8.0.21"


def test_genuinely_different_releases_stay_separate() -> None:
    old = normalize_row("Microsoft .NET Runtime - 8.0.8 (x64)", "8.0.8", "Microsoft")
    new = normalize_row("Microsoft .NET Runtime - 8.0.21 (x64)", "8.0.21", "Microsoft")
    assert identity_key(old) != identity_key(new)


@pytest.mark.parametrize(
    "name, expected_product",
    [
        ("Microsoft Edge", "edge"),
        ("Microsoft Edge WebView2 Runtime", "edge_webview2"),
        ("Microsoft Edge Update", "microsoft_edge_update"),
        ("Microsoft .NET Host - 8.0.21 (x64)", ".net_host"),
        ("Microsoft .NET Host FX Resolver - 8.0.8 (x64)", ".net_host_fx_resolver"),
        ("Microsoft ASP.NET Core 8.0.8 - Shared Framework", "aspnet_core"),
        ("Java Auto Updater", "java_auto_updater"),
    ],
)
def test_separately_versioned_components_do_not_collapse(name: str, expected_product: str) -> None:
    """These ship and version independently, so sharing a product identity misdates the advisory."""
    assert normalize_row(name, "1.0.0", "Microsoft").product == expected_product


def test_trademark_symbols_do_not_change_identity() -> None:
    plain = normalize_row(".NET Framework", "4.8.9221.0", "Microsoft")
    marked = normalize_row("Microsoft\u00ae .NET Framework", "4.8.9221.0", "Microsoft\u00ae")
    assert (marked.vendor, marked.product) == (plain.vendor, plain.product)
    assert marked.product == ".net_framework"


@pytest.mark.parametrize(
    "left, right",
    [("Google LLC", "Google, Inc."), ("Mozilla Corporation", "Mozilla"), ("Zoho Pvt Ltd", "Zoho")],
)
def test_vendor_suffixes_are_not_part_of_identity(left: str, right: str) -> None:
    assert normalize_vendor(left) == normalize_vendor(right)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("139.0.7258.128", "139.0.7258.128"),
        ("8.6.4 (64-bit)", "8.6.4"),
        ("v2.43.0.windows.1", "2.43.0"),
        ("unknown", None),
        ("", None),
        (None, None),
        ("20250101", None),
    ],
)
def test_version_normalization(raw: str | None, expected: str | None) -> None:
    assert normalize_version(raw) == expected


@pytest.mark.parametrize(
    "left, right, expected",
    [
        ("139.0.7258.128", "139.0.7258.127", 1),
        ("139.0.7258.128", "140.0.1.1", -1),
        ("8.0.21", "8.0.21.0", 0),
        ("1.2", "1.2.0.0", 0),
    ],
)
def test_version_comparison_pads_missing_components(left: str, right: str, expected: int) -> None:
    assert compare_versions(left, right) == expected


def test_unrecognised_products_are_kept_not_guessed() -> None:
    result = normalize_row("Acme Internal Tool 3", "3.1", "Acme Pty Ltd")
    assert not result.confident
    assert result.vendor == "acme"
    assert result.product == "acme_internal_tool"


def test_distro_packages_match_on_package_name() -> None:
    result = normalize_row("libssl3", "3.0.11-1", None, package_type="deb")
    assert (result.vendor, result.product) == ("openssl", "openssl")
    assert result.confident


def test_deb_alias_matches_dpkg() -> None:
    deb = normalize_row("openssh-server", "1:9.6p1-3ubuntu13.5", None, package_type="deb")
    dpkg = normalize_row("openssh-server", "1:9.6p1-3ubuntu13.5", None, package_type="dpkg")
    assert identity_key(deb) == identity_key(dpkg)


def test_distro_versions_are_preserved_verbatim() -> None:
    """Debian publishes fixes as full package versions, so nothing may be trimmed off."""
    result = normalize_row("openssh-server", "1:9.6p1-3ubuntu13.5", None, package_type="deb")
    assert result.version == "1:9.6p1-3ubuntu13.5"
    assert result.product == "openssh"


@pytest.mark.parametrize(
    "installed, fixed, expected",
    [
        # The distinction a truncating normalizer would lose.
        ("1:9.6p1-3ubuntu13.4", "1:9.6p1-3ubuntu13.5", -1),
        ("1:9.6p1-3ubuntu13.5", "1:9.6p1-3ubuntu13.5", 0),
        ("1:9.6p1-3ubuntu13.6", "1:9.6p1-3ubuntu13.5", 1),
        ("9.6p2-1", "9.6p1-1", 1),
        # An upstream release is older than the same release with a patch level.
        ("9.6-1", "9.6p1-1", -1),
        # Epoch outranks everything after it.
        ("1:1.0-1", "2:0.9-1", -1),
        # Numeric components compare as numbers, not text.
        ("1.10-1", "1.9-1", 1),
        ("3.0.11-1", "3.0.9-1", 1),
    ],
)
def test_distro_version_comparison(installed: str, fixed: str, expected: int) -> None:
    assert compare_distro_versions(installed, fixed) == expected


def test_parse_version_tolerates_non_numeric_components() -> None:
    assert parse_version("9.6p1") == (9, 6)
    assert parse_version(None) == ()
