"""A lightweight heartbeat: a periodic 'still working' line, not a progress bar / TUI.

Prints to stderr so stdout stays clean for command output; works headless under launchd.
Set heartbeat_seconds <= 0 to disable.
"""

from __future__ import annotations

import sys
import threading
import time
from types import TracebackType


class Heartbeat:
    def __init__(self, interval_seconds: int, activity: str = "starting", stream=sys.stderr):
        self.interval = interval_seconds
        self._activity = activity
        self._stream = stream
        self._start = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def update(self, activity: str) -> None:
        with self._lock:
            self._activity = activity

    def _emit(self) -> None:
        elapsed = int(time.monotonic() - self._start)
        with self._lock:
            activity = self._activity
        self._stream.write(f"… still working — {elapsed}s elapsed — {activity}\n")
        self._stream.flush()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._emit()

    def __enter__(self) -> Heartbeat:
        if self.interval and self.interval > 0:
            self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def progress(message: str, stream=sys.stderr) -> None:
    """Emit a discrete progress line (e.g. per-chunk completion)."""
    stream.write(f"{message}\n")
    stream.flush()
