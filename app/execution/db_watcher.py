"""Detects replacement of the analytical database file (periodic SAP B1 refresh).

On change: fires registered callbacks (executor.reopen now; metadata index
rebuild + cache invalidation join in M2/M7). Runs as a daemon thread; the
check_once() method is exposed separately so tests and health checks can probe
deterministically without sleeping.
"""

import threading
from collections.abc import Callable
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("db_watcher")


class DatabaseWatcher:
    def __init__(self, db_path: Path | str, interval_s: int = 60) -> None:
        self._db_path = Path(db_path)
        self._interval_s = interval_s
        self._callbacks: list[Callable[[], None]] = []
        self._fingerprint = self._current_fingerprint()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_change_detected: float | None = None

    def _current_fingerprint(self) -> tuple[float, int] | None:
        try:
            stat = self._db_path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime, stat.st_size)

    def on_change(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def check_once(self) -> bool:
        current = self._current_fingerprint()
        if current == self._fingerprint:
            return False
        log.info("db_refresh_detected", old=self._fingerprint, new=current)
        self._fingerprint = current
        import time

        self.last_change_detected = time.time()
        for callback in self._callbacks:
            try:
                callback()
            except Exception:
                log.exception("db_refresh_callback_failed")
        return True

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            self.check_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="db-watcher", daemon=True)
        self._thread.start()
        log.info("db_watcher_started", interval_s=self._interval_s)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
