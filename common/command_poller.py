"""Background command polling so remote commands are not blocked by long metric cycles."""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

PollFn = Callable[[], bool]


class CommandPoller:
    """Poll Supabase commands on a daemon thread, independent of the metrics loop."""

    def __init__(self, poll_fn: PollFn, interval_seconds: float) -> None:
        self._poll_fn = poll_fn
        self._interval = max(1.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    should_exit = self._poll_fn()
                if should_exit:
                    logging.info("Stop command handled; exiting agent process")
                    os._exit(0)
            except Exception:
                logging.exception("Command poll failed")
            if self._stop.wait(self._interval):
                break

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="adt-command-poller",
            daemon=True,
        )
        self._thread.start()
        logging.info("Background command poller started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 5)


def start_command_poller(poll_fn: PollFn, interval_seconds: float) -> CommandPoller:
    poller = CommandPoller(poll_fn, interval_seconds)
    poller.start()
    return poller
