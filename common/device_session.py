"""Mutable device identity for /api/agent/* calls with automatic re-enrollment on 401."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import requests

from config_io import load_config, update_config
from enrollment import EnrollmentError, device_headers, enroll, normalize_code

log = logging.getLogger(__name__)


class DeviceSession:
    """Holds endpoint credentials and re-enrolls when the server rejects the device token."""

    def __init__(
        self,
        api_base: str,
        config_path: Path,
        agent_version: str,
        config: dict[str, Any],
    ) -> None:
        self.api_base = (api_base or "").strip().rstrip("/")
        self.config_path = config_path
        self.agent_version = agent_version
        self._lock = threading.Lock()
        self._config = dict(config)
        self.endpoint_id = str(config.get("ENDPOINT_ID") or "").strip()
        self.device_token = str(config.get("DEVICE_TOKEN") or "").strip()

    @property
    def enrollment_code(self) -> str:
        return str(self._config.get("ENROLLMENT_CODE") or "").strip()

    def reload(self) -> None:
        with self._lock:
            self._config = load_config(self.config_path)
            self.endpoint_id = str(self._config.get("ENDPOINT_ID") or "").strip()
            self.device_token = str(self._config.get("DEVICE_TOKEN") or "").strip()

    def recover_auth(self) -> bool:
        """Re-enroll with the stored org code when the current device token is no longer valid."""
        with self._lock:
            code = self.enrollment_code
            if not code:
                log.error(
                    "Device token rejected and no ENROLLMENT_CODE is stored — "
                    "reinstall the agent from Vizhi to enroll again."
                )
                return False

            log.warning(
                "Device token rejected by Vizhi; re-enrolling with stored enrollment code %s…",
                code[:8],
            )
            try:
                result = enroll(
                    self.api_base,
                    code,
                    self.agent_version,
                    role=str(self._config.get("ROLE") or "").strip() or None,
                )
            except EnrollmentError as exc:
                log.error("Automatic re-enrollment failed: %s", exc)
                return False

            self.endpoint_id = result["ENDPOINT_ID"]
            self.device_token = result["DEVICE_TOKEN"]
            if result.get("ROLE"):
                self._config["ROLE"] = result["ROLE"]

            normalized = normalize_code(code)

            def patch(cfg: dict[str, Any]) -> dict[str, Any]:
                cfg["ENDPOINT_ID"] = self.endpoint_id
                cfg["DEVICE_TOKEN"] = self.device_token
                cfg["ENROLLMENT_CODE"] = normalized
                cfg["API_BASE"] = self.api_base
                if self._config.get("MACHINE_GUID"):
                    cfg["MACHINE_GUID"] = self._config["MACHINE_GUID"]
                if self._config.get("AGENT_AUTO_UPDATE"):
                    cfg["AGENT_AUTO_UPDATE"] = self._config["AGENT_AUTO_UPDATE"]
                if result.get("ROLE"):
                    cfg["ROLE"] = result["ROLE"]
                return cfg

            self._config = update_config(self.config_path, patch)
            log.info(
                "Re-enrolled successfully endpoint=%s…",
                self.endpoint_id[:8],
            )
            return True

    def request(
        self,
        method: str,
        path: str,
        *,
        retry_on_auth_failure: bool = True,
        **kwargs: Any,
    ) -> requests.Response | None:
        """Authenticated HTTP call to Vizhi; retries once after re-enrollment on HTTP 401."""
        if not self.api_base:
            return None

        url = path if path.startswith("http") else f"{self.api_base}{path}"
        attempts = 2 if retry_on_auth_failure else 1

        for attempt in range(attempts):
            extra_headers = kwargs.pop("headers", None) or {}
            headers = {**device_headers(self.device_token), **extra_headers}
            try:
                resp = requests.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                log.warning("%s %s failed to send: %s", method.upper(), path, exc)
                return None

            if resp.status_code == 401 and retry_on_auth_failure and attempt == 0:
                if self.recover_auth():
                    continue
            return resp

        return None
