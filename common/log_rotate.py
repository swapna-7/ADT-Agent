"""Rotate agent.log so endpoints do not keep unbounded PII-adjacent output."""

from __future__ import annotations

import logging
from pathlib import Path


def rotate_logs(log_path: Path, max_size_mb: int = 10, keep_files: int = 3) -> None:
    path = Path(log_path)
    if not path.is_file():
        return
    max_bytes = max(1, int(max_size_mb)) * 1024 * 1024
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    keep = max(1, int(keep_files))
    oldest = path.with_name(f"{path.name}.{keep}")
    try:
        if oldest.exists():
            oldest.unlink()
    except OSError:
        pass
    for index in range(keep - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dest = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            try:
                src.replace(dest)
            except OSError:
                logging.getLogger(__name__).warning("Could not rotate %s", src)
    try:
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        logging.getLogger(__name__).warning("Could not rotate %s", path)
