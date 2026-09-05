"""Replace the installed agent binary with a newer GitHub Release, via Vizhi.

Agents never call GitHub. Vizhi authenticates the device token, reads the private
release, and either describes it (`/api/agent/update/latest`) or 302s the download
(`/api/agent/update/download`). This module only runs when the frozen binary is
already at the install path; `.env` and `config.json` are never touched.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from enrollment import device_headers, platform_tag
from version import AGENT_VERSION

log = logging.getLogger(__name__)

LATEST_PATH = "/api/agent/update/latest"
DOWNLOAD_PATH = "/api/agent/update/download"
DEFAULT_INTERVAL_SECONDS = 21600
JITTER_SECONDS = 15 * 60
DOWNLOAD_TIMEOUT = 120
LATEST_TIMEOUT = 30
CHUNK_SIZE = 1024 * 256
REPLACE_ATTEMPTS = 8
MAX_DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_SLEEP = 30


def is_truthy(value: str | None, default: bool = True) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_semver(raw: str | None) -> tuple[int, ...]:
    """Numeric (major, minor, patch, ...) for dotted versions. Leading v is ignored."""
    text = (raw or "").strip().lstrip("vV")
    core = text.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for chunk in core.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts or (0,))


def is_newer(remote: str, current: str) -> bool:
    return parse_semver(remote) > parse_semver(current)


def auto_update_enabled(raw: str | None) -> bool:
    return is_truthy(raw, default=True)


def read_auto_update_flag(local_config: dict[str, str], env: dict[str, str]) -> bool:
    """Re-read on every check. config.json wins over .env wins over OS environment."""
    for source in (
        local_config.get("AGENT_AUTO_UPDATE"),
        env.get("AGENT_AUTO_UPDATE"),
        os.getenv("AGENT_AUTO_UPDATE"),
    ):
        if source is not None:
            return auto_update_enabled(str(source))
    return True


def next_check_deadline(interval_seconds: int, *, initial: bool = False) -> float:
    """Absolute unix time of the next check. First check is jitter-only so a fleet
    does not wait a full interval after the one-time 2.1.0 reinstall."""
    interval = max(60, int(interval_seconds))
    jitter_cap = min(JITTER_SECONDS, max(1, int(interval * 0.1)))
    jitter = random.uniform(0, jitter_cap)
    delay = jitter if initial else interval + jitter
    return time.time() + delay


def backup_path(install_exe: Path) -> Path:
    if install_exe.suffix:
        return install_exe.with_suffix(".old")
    return install_exe.with_name(install_exe.name + ".old")


def cleanup_previous_backup(install_exe: Path) -> None:
    """Drop leftover `.old` once the new binary has actually started."""
    backup = backup_path(install_exe)
    try:
        if backup.is_file():
            backup.unlink()
            log.info("Removed previous agent backup %s", backup)
    except OSError as exc:
        log.warning("Could not remove agent backup %s: %s", backup, exc)


def should_self_update(*, frozen: bool, install_exe: Path) -> bool:
    if not frozen:
        return False
    try:
        return Path(sys.executable).resolve() == Path(install_exe).resolve()
    except OSError:
        return False


def _absolute_url(api_base: str, url: str) -> str:
    text = (url or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text
    base = (api_base or "").strip().rstrip("/") + "/"
    return urljoin(base, text.lstrip("/"))


def fetch_latest(
    api_base: str,
    device_token: str,
    *,
    platform: str | None = None,
    timeout: int = LATEST_TIMEOUT,
) -> dict[str, Any] | None:
    base = (api_base or "").strip().rstrip("/")
    if not base or not device_token:
        return None
    os_name = platform or platform_tag()
    try:
        resp = requests.get(
            f"{base}{LATEST_PATH}",
            params={"platform": os_name},
            headers=device_headers(device_token),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.warning("Agent update check failed to send: %s", exc)
        return None
    if resp.status_code >= 400:
        log.warning(
            "Agent update check rejected (HTTP %s): %s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("Agent update check returned non-JSON")
        return None
    return data if isinstance(data, dict) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_and_verify(
    vizhi_download_url: str,
    dest: Path,
    expected_sha256: str,
    *,
    device_token: str,
    timeout: int = DOWNLOAD_TIMEOUT,
) -> bool:
    """Download from the Vizhi /download URL only. Each retry issues a fresh 302."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        log.warning("Refusing to download an agent update with no sha256")
        return False

    headers = device_headers(device_token)
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        digest = hashlib.sha256()
        try:
            with requests.get(
                vizhi_download_url,
                headers=headers,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    log.warning(
                        "Download attempt %s failed: HTTP %s",
                        attempt,
                        resp.status_code,
                    )
                    if attempt < MAX_DOWNLOAD_ATTEMPTS:
                        time.sleep(DOWNLOAD_RETRY_SLEEP)
                    continue
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        digest.update(chunk)
        except Exception as exc:
            log.warning("Download attempt %s exception: %s", attempt, exc)
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < MAX_DOWNLOAD_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_SLEEP)
            continue

        actual = digest.hexdigest()
        if actual != expected:
            log.warning(
                "SHA-256 mismatch on attempt %s: expected %s got %s",
                attempt,
                expected,
                actual,
            )
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < MAX_DOWNLOAD_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_SLEEP)
            continue
        return True
    return False


def replace_installed_binary(staging: Path, install_exe: Path) -> bool:
    """Rename the running binary to `.old` and copy the new file into place.

    Windows allows renaming a running image; Linux/macOS keep the old inode open
    until this process exits. Supervisors then start the new path.
    """
    dst = Path(install_exe)
    dst.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_path(dst)
    last_error: Exception | None = None

    for _ in range(REPLACE_ATTEMPTS):
        try:
            if backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    pass
            if dst.exists():
                try:
                    dst.rename(backup)
                except OSError:
                    pass
            shutil.copy2(staging, dst)
            if os.name != "nt":
                os.chmod(dst, 0o755)
            return True
        except OSError as exc:
            last_error = exc
            time.sleep(1.5)

    if last_error:
        log.warning("Could not replace installed agent at %s: %s", dst, last_error)
    return False


def maybe_apply_update(
    *,
    api_base: str,
    device_token: str,
    data_dir: Path,
    install_exe: Path,
    current_version: str = AGENT_VERSION,
) -> bool:
    """Download and install a newer binary. True when the process should exit."""
    latest = fetch_latest(api_base, device_token)
    if not latest:
        return False

    remote_version = str(latest.get("version") or "").strip()
    sha256 = str(latest.get("sha256") or "").strip()
    url = str(latest.get("url") or "").strip() or f"{DOWNLOAD_PATH}?platform={platform_tag()}"
    if not remote_version or not sha256:
        log.warning("Agent update payload missing version or sha256")
        return False

    if not is_newer(remote_version, current_version):
        log.debug(
            "Agent is up to date (%s, remote %s)",
            current_version,
            remote_version,
        )
        return False

    log.info(
        "Agent update available: %s -> %s",
        current_version,
        remote_version,
    )
    staging_dir = Path(data_dir) / "update"
    staging = staging_dir / Path(install_exe).name
    download_url = _absolute_url(api_base, url)
    if not download_and_verify(download_url, staging, sha256, device_token=device_token):
        return False

    if not replace_installed_binary(staging, install_exe):
        return False

    try:
        staging.unlink(missing_ok=True)
    except OSError:
        pass

    log.info("Installed agent %s at %s", remote_version, install_exe)
    return True
