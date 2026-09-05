"""Canonical vendor + product + version for inventory rows.

Six collectors see the same application under six spellings. "Google Chrome",
"Google Chrome Browser" and "Chrome" must collapse to one product, or the vulnerability engine
compares an advisory against three half-populated records and gets all three wrong.

Everything here is deliberately conservative: when a name cannot be recognised the raw value is
cleaned but kept, never guessed at. A wrong canonical name is worse than an unmatched one.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Trailing noise that is not part of the product identity.
_NOISE_PATTERNS = [
    r"\s*\(64[- ]?bit\)\s*$",
    r"\s*\(32[- ]?bit\)\s*$",
    r"\s*\(x64\)\s*$",
    r"\s*\(x86\)\s*$",
    r"\s*\(en-US\)\s*$",
    r"\s*\b(x64|x86|amd64|arm64|win64|win32)\b\s*$",
    r"\s*-\s*(en-US|English \(United States\))\s*$",
]

# Version-ish tails inside a display name, e.g. "Notepad++ 8.6.4" or "Python 3.12.1 (64-bit)".
_TRAILING_VERSION = re.compile(r"\s+v?\d+(?:\.\d+)*(?:[a-z]\d*)?\s*$", re.IGNORECASE)

_TRADEMARK = re.compile(r"[®™©]|\((?:r|tm|c)\)", re.IGNORECASE)

# A version stated in the display name, e.g. "Microsoft .NET Runtime - 8.0.21 (x64)".
_NAME_VERSION = re.compile(r"[-–—\s]\s*v?(\d+(?:\.\d+){1,3})\b")

_VENDOR_SUFFIXES = re.compile(
    r"\b(inc|inc\.|incorporated|corp|corp\.|corporation|llc|l\.l\.c\.|ltd|ltd\.|limited|"
    r"gmbh|s\.a\.|s\.a|sa|ab|oy|plc|co|co\.|company|foundation|software|technologies|"
    r"technology|systems|labs|group|holdings|pty|pvt|private|bv|b\.v\.)\b\.?",
    re.IGNORECASE,
)

# Canonical identities. Matched against the cleaned, lowercased display name.
# Order matters: the first matching rule wins, so put specific products before generic ones.
_PRODUCT_RULES: list[tuple[re.Pattern[str], str, str]] = [
    # Browsers
    (re.compile(r"^google chrome(?! for business)"), "google", "chrome"),
    (re.compile(r"^chrome$"), "google", "chrome"),
    # "webview2"/"update" have no trailing word boundary, so match them as bare substrings.
    (re.compile(r"^microsoft edge(?!.*(webview|update))"), "microsoft", "edge"),
    (re.compile(r"^microsoft edge webview"), "microsoft", "edge_webview2"),
    (re.compile(r"^mozilla firefox"), "mozilla", "firefox"),
    (re.compile(r"^firefox$"), "mozilla", "firefox"),
    (re.compile(r"^mozilla thunderbird"), "mozilla", "thunderbird"),
    (re.compile(r"^brave"), "brave", "brave"),
    (re.compile(r"^opera\b"), "opera", "opera"),
    (re.compile(r"^vivaldi"), "vivaldi", "vivaldi"),
    # Runtimes. .NET and Java ship many separately-versioned components under one brand, so each
    # component is its own product: collapsing them compares an 8.0 runtime advisory against a
    # language-pack MSI version and reaches a confidently wrong verdict.
    (re.compile(r"^java auto updater"), "oracle", "java_auto_updater"),
    (re.compile(r"^(oracle )?java\b(?!.*updater)|^java\(tm\)|^jdk\b|^jre\b"), "oracle", "java"),
    (re.compile(r"^openjdk|^eclipse temurin|^adoptopenjdk"), "eclipse", "temurin"),
    (re.compile(r"^node\.?js"), "openjs", "node.js"),
    (re.compile(r"^python\b(?!.*launcher)"), "python", "python"),
    (re.compile(r"^microsoft \.net framework|^\.net framework"), "microsoft", ".net_framework"),
    (
        re.compile(r"^(microsoft )?(windows )?\.net desktop runtime|^microsoft windows desktop runtime"),
        "microsoft",
        ".net_desktop_runtime",
    ),
    # ASP.NET Core ships its own shared framework and carries advisories separately from the
    # base runtime, so it must not fold into .net_runtime.
    (re.compile(r"^microsoft aspnetcore|^(microsoft )?asp\.net core"), "microsoft", "aspnet_core"),
    (re.compile(r"^(microsoft )?\.net runtime"), "microsoft", ".net_runtime"),
    # ".NET Core Runtime" is what 3.1 and earlier called the same shared runtime.
    (re.compile(r"^(microsoft )?\.net core runtime"), "microsoft", ".net_runtime"),
    (re.compile(r"^(microsoft )?\.net sdk"), "microsoft", ".net_sdk"),
    # The FX resolver is a separate MSI from the host and versions independently of it.
    (re.compile(r"^(microsoft )?\.net host fx resolver"), "microsoft", ".net_host_fx_resolver"),
    (re.compile(r"^(microsoft )?\.net host"), "microsoft", ".net_host"),
    (re.compile(r"^microsoft visual c\+\+"), "microsoft", "visual_c++_redistributable"),
    # Productivity
    (re.compile(r"^microsoft 365|^microsoft office"), "microsoft", "office"),
    (re.compile(r"^microsoft teams"), "microsoft", "teams"),
    (re.compile(r"^microsoft onedrive|^onedrive$"), "microsoft", "onedrive"),
    (re.compile(r"^microsoft outlook|^outlook \("), "microsoft", "outlook"),
    (re.compile(r"^adobe acrobat reader|^adobe reader"), "adobe", "acrobat_reader"),
    (re.compile(r"^adobe acrobat(?! reader)"), "adobe", "acrobat"),
    (re.compile(r"^libreoffice"), "libreoffice", "libreoffice"),
    (re.compile(r"^zoom(?: workplace| meetings)?$|^zoom\b"), "zoom", "zoom"),
    (re.compile(r"^slack$|^slack\b"), "salesforce", "slack"),
    (re.compile(r"^notepad\+\+"), "notepad-plus-plus", "notepad++"),
    (re.compile(r"^7-zip"), "7-zip", "7-zip"),
    (re.compile(r"^winrar"), "rarlab", "winrar"),
    (re.compile(r"^vlc media player|^vlc$"), "videolan", "vlc_media_player"),
    # Dev tools
    (re.compile(r"^(microsoft )?visual studio code(?! insiders)|^vs code$"), "microsoft", "visual_studio_code"),
    (re.compile(r"^(microsoft )?visual studio \d{4}"), "microsoft", "visual_studio"),
    (re.compile(r"^git\b(?! extensions)"), "git", "git"),
    (re.compile(r"^docker desktop"), "docker", "docker_desktop"),
    (re.compile(r"^postman"), "postman", "postman"),
    (re.compile(r"^wireshark"), "wireshark", "wireshark"),
    (re.compile(r"^openssh"), "openbsd", "openssh"),
    (re.compile(r"^openssl"), "openssl", "openssl"),
    (re.compile(r"^putty"), "putty", "putty"),
    (re.compile(r"^filezilla"), "filezilla", "filezilla"),
    (re.compile(r"^apache tomcat|^tomcat"), "apache", "tomcat"),
    (re.compile(r"^nginx"), "nginx", "nginx"),
    (re.compile(r"^mysql server|^mysql\b"), "oracle", "mysql"),
    (re.compile(r"^postgresql"), "postgresql", "postgresql"),
    (re.compile(r"^mongodb"), "mongodb", "mongodb"),
    (re.compile(r"^redis"), "redis", "redis"),
    # Security
    (re.compile(r"^teamviewer"), "teamviewer", "teamviewer"),
    (re.compile(r"^anydesk"), "anydesk", "anydesk"),
    (re.compile(r"^wazuh"), "wazuh", "wazuh"),
]

# Distro package names that mean the same product as a GUI display name.
_LINUX_PACKAGE_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^google-chrome(-stable|-beta|-unstable)?$"), "google", "chrome"),
    (re.compile(r"^chromium(-browser)?$"), "chromium", "chromium"),
    (re.compile(r"^firefox(-esr)?$"), "mozilla", "firefox"),
    (re.compile(r"^thunderbird$"), "mozilla", "thunderbird"),
    (re.compile(r"^openssh-(server|client)$"), "openbsd", "openssh"),
    (re.compile(r"^openssl$|^libssl\d"), "openssl", "openssl"),
    (re.compile(r"^nginx(-\w+)?$"), "nginx", "nginx"),
    (re.compile(r"^apache2$|^httpd$"), "apache", "http_server"),
    (re.compile(r"^mysql-server(-\d+\.\d+)?$"), "oracle", "mysql"),
    (re.compile(r"^mariadb-server(-\d+\.\d+)?$"), "mariadb", "mariadb"),
    (re.compile(r"^postgresql(-\d+)?$"), "postgresql", "postgresql"),
    (re.compile(r"^python3(\.\d+)?$"), "python", "python"),
    (re.compile(r"^nodejs$|^node$"), "openjs", "node.js"),
    (re.compile(r"^git$"), "git", "git"),
    (re.compile(r"^sudo$"), "sudo_project", "sudo"),
    (re.compile(r"^bash$"), "gnu", "bash"),
    (re.compile(r"^glibc$|^libc6$"), "gnu", "glibc"),
    (re.compile(r"^linux-image-.*|^kernel$|^kernel-core$"), "linux", "linux_kernel"),
    (re.compile(r"^vlc$"), "videolan", "vlc_media_player"),
    (re.compile(r"^libreoffice(-\w+)?$"), "libreoffice", "libreoffice"),
]

_VERSION_CORE = re.compile(r"(\d+(?:\.\d+)*)")


class Normalized(NamedTuple):
    vendor: str | None
    product: str
    """Most precise version available, used for advisory comparison."""
    version: str | None
    """Vendor release identity used for de-duplication; see _versions()."""
    release: str | None
    """True when a curated rule matched, i.e. safe to compare against advisory ranges by identity."""
    confident: bool


def clean_display_name(raw: str) -> str:
    """Strip architecture/locale noise and a trailing version from a display name."""
    name = (raw or "").strip()
    if not name:
        return ""
    name = _TRADEMARK.sub("", name)
    for pattern in _NOISE_PATTERNS:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)
    name = _TRAILING_VERSION.sub("", name)
    return re.sub(r"\s{2,}", " ", name).strip(" -–—\t")


def version_from_name(raw: str) -> str | None:
    """Version stated in the display name, e.g. '8.0.21' from 'Microsoft .NET Runtime - 8.0.21'."""
    match = _NAME_VERSION.search(_TRADEMARK.sub("", raw or ""))
    return match.group(1) if match else None


DISTRO_PACKAGE_TYPES = {"deb", "dpkg", "rpm", "snap", "flatpak"}


def normalize_distro_version(raw: str | None) -> str | None:
    """Keep a distro package version verbatim.

    Debian and Red Hat publish fixes as full package versions ("1:9.6p1-3ubuntu13.5"), and the
    epoch, upstream patch level and distro revision are all significant. Reducing that to dotted
    numerics would drop "p1" and "-3ubuntu13.5", which is the difference between patched and
    vulnerable, so the string is preserved and compared with packaging rules instead.
    """
    if not raw:
        return None
    value = raw.strip()
    if not value or value.lower() in {"unknown", "n/a", "-", "—", "installed", "none"}:
        return None
    return value


def _versions(raw_name: str, raw_version: str | None) -> tuple[str | None, str | None]:
    """Split a raw row into (comparable version, release identity).

    The same installed product reaches us with different version strings depending on the
    collector: the registry reports ".NET Runtime - 8.0.21" as 8.0.21.35325 while MSI reports its
    own packed 64.84.40925. Comparing those against each other splits one product into several
    rows, so the release stated in the display name is used as the de-duplication identity and the
    most precise numeric is kept for advisory comparison. When the collector's leading component
    contradicts the name outright its value is packaging metadata and is discarded.
    """
    collector = normalize_version(raw_version)
    named = normalize_version(version_from_name(raw_name))
    if not named:
        return collector, collector
    if not collector:
        return named, named
    if parse_version(named)[:1] != parse_version(collector)[:1]:
        return named, named
    return collector, named


def normalize_vendor(raw: str | None) -> str | None:
    """Lowercase a publisher and drop corporate suffixes so 'Google LLC' == 'Google Inc.'."""
    if not raw:
        return None
    value = raw.strip()
    if not value or value.lower() in {"unknown", "n/a", "-", "—"}:
        return None
    value = re.sub(r"[.,]", " ", value)
    value = _VENDOR_SUFFIXES.sub(" ", value)
    value = re.sub(r"[^\w\s+-]", " ", value)
    value = re.sub(r"\s{2,}", " ", value).strip().lower()
    return value.replace(" ", "_") or None


def normalize_version(raw: str | None) -> str | None:
    """Reduce a display version to comparable dotted numerics.

    "139.0.7258.128" stays as-is; "8.6.4 (64-bit)" becomes "8.6.4"; "unknown" becomes None. Extra
    build suffixes are dropped rather than guessed at, because a wrong version silently produces a
    wrong vulnerability verdict.
    """
    if not raw:
        return None
    value = raw.strip()
    if not value or value.lower() in {"unknown", "n/a", "-", "—", "installed"}:
        return None
    match = _VERSION_CORE.search(value)
    if not match:
        return None
    core = match.group(1)
    # Guard against registry junk like "20250101" masquerading as a version.
    if "." not in core and len(core) > 6:
        return None
    return core


def parse_version(version: str | None) -> tuple[int, ...]:
    """Comparable tuple for a normalized version. Missing parts sort as 0."""
    if not version:
        return ()
    parts: list[int] = []
    for chunk in version.split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def compare_versions(left: str | None, right: str | None) -> int:
    """-1 / 0 / 1 comparing two normalized versions, padding the shorter with zeros."""
    a = parse_version(left)
    b = parse_version(right)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _split_distro_version(version: str) -> tuple[int, str, str]:
    """Split "1:9.6p1-3ubuntu13.5" into (epoch, upstream, revision)."""
    epoch = 0
    rest = version
    if ":" in rest:
        head, _, tail = rest.partition(":")
        if head.isdigit():
            epoch = int(head)
            rest = tail
    upstream, _, revision = rest.rpartition("-")
    if not upstream:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def _distro_chunks(value: str) -> list[tuple[int, int | str]]:
    """Break a version part into alternating non-numeric and numeric runs.

    Numbers compare numerically so 10 sorts above 9, and text compares as text so "p2" beats "p1".
    Numeric runs sort above text runs at the same position, which makes "9.6" older than "9.6p1".
    """
    chunks: list[tuple[int, int | str]] = []
    for text, digits in re.findall(r"([^0-9]*)([0-9]*)", value):
        if text:
            chunks.append((0, text))
        if digits:
            chunks.append((1, int(digits)))
    return chunks


def compare_distro_versions(left: str | None, right: str | None) -> int:
    """-1 / 0 / 1 comparing two distro package versions by epoch, then upstream, then revision.

    Approximates dpkg's algorithm closely enough for the "is the installed version at least the
    fixed version" question, which is the only comparison the vulnerability engine asks.
    """
    if left == right:
        return 0
    if not left:
        return -1
    if not right:
        return 1

    l_epoch, l_up, l_rev = _split_distro_version(left)
    r_epoch, r_up, r_rev = _split_distro_version(right)
    if l_epoch != r_epoch:
        return -1 if l_epoch < r_epoch else 1

    for a, b in ((l_up, r_up), (l_rev, r_rev)):
        chunks_a = _distro_chunks(a)
        chunks_b = _distro_chunks(b)
        for chunk_a, chunk_b in zip(chunks_a, chunks_b):
            if chunk_a == chunk_b:
                continue
            if chunk_a[0] != chunk_b[0]:
                return 1 if chunk_a[0] > chunk_b[0] else -1
            return 1 if chunk_a[1] > chunk_b[1] else -1  # type: ignore[operator]
        if len(chunks_a) != len(chunks_b):
            return 1 if len(chunks_a) > len(chunks_b) else -1
    return 0


def normalize_row(
    name: str,
    version: str | None = None,
    publisher: str | None = None,
    *,
    package_type: str | None = None,
) -> Normalized:
    """Map one raw inventory row onto a canonical identity."""
    cleaned = clean_display_name(name)
    lowered = cleaned.lower()

    # Distro packages carry precise names and packaging-significant versions, so they are matched
    # before display-name heuristics and their version is never reduced to dotted numerics.
    if package_type in DISTRO_PACKAGE_TYPES:
        exact = normalize_distro_version(version)
        package_key = (name or "").strip().lower()
        for pattern, vendor, product in _LINUX_PACKAGE_RULES:
            if pattern.match(package_key):
                return Normalized(vendor, product, exact, exact, True)
        fallback = re.sub(r"[^\w+.-]+", "_", package_key).strip("_")
        return Normalized(
            normalize_vendor(publisher),
            fallback or package_key or "unknown",
            exact,
            exact,
            False,
        )

    norm_version, release = _versions(name, version)

    for pattern, vendor, product in _PRODUCT_RULES:
        if pattern.match(lowered):
            return Normalized(vendor, product, norm_version, release, True)

    fallback_product = re.sub(r"[^\w+.-]+", "_", lowered).strip("_")
    return Normalized(
        normalize_vendor(publisher),
        fallback_product or lowered or "unknown",
        norm_version,
        release,
        False,
    )


def identity_key(normalized: Normalized) -> str:
    """Merge key: collectors that saw the same product produce the same key."""
    vendor = normalized.vendor or "unknown"
    return f"{vendor}:{normalized.product}:{normalized.release or 'unknown'}"
