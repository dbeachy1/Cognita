"""Bounded durable-job output and exact identity helpers.

The actual process is launched by the guest supervisor through Microsandbox.
This module owns the broker-side rules needed to page output and avoid replaying
an unprovable process after restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

MAX_RETAINED_OUTPUT = 8 * 1024 * 1024
MAX_POLL_OUTPUT = 1024 * 1024


@dataclass(frozen=True)
class OutputPage:
    data: bytes
    first_available_offset: int
    next_offset: int
    truncated: bool


class OutputRing:
    """Tail-retaining output buffer with stable byte offsets."""

    def __init__(self, limit: int = MAX_RETAINED_OUTPUT) -> None:
        if not 1 <= limit <= MAX_RETAINED_OUTPUT:
            raise ValueError("output limit is outside the broker bounds")
        self.limit = limit
        self._data = bytearray()
        self._base = 0
        self._total = 0
        self._lock = RLock()

    def append(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("job output must be bytes")
        with self._lock:
            self._data.extend(data)
            self._total += len(data)
            if len(self._data) > self.limit:
                trim = len(self._data) - self.limit
                del self._data[:trim]
                self._base += trim

    def page(self, offset: int = 0, limit: int = MAX_POLL_OUTPUT) -> OutputPage:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("output offset is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_POLL_OUTPUT:
            raise ValueError("output page limit is invalid")
        with self._lock:
            start = max(offset, self._base)
            end = min(start + limit, self._base + len(self._data))
            if start > self._base + len(self._data):
                start = self._base + len(self._data)
            begin = start - self._base
            finish = end - self._base
            return OutputPage(bytes(self._data[begin:finish]), self._base,
                              self._base + len(self._data), offset < self._base)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total


def process_identity_is_current(*, expected_pid: int, expected_start_token: str,
                                observed_pid: int | None, observed_start_token: str | None) -> bool:
    """Require both PID and kernel start token before signaling a process."""
    return (isinstance(expected_pid, int) and expected_pid > 0 and observed_pid == expected_pid
            and isinstance(expected_start_token, str) and bool(expected_start_token)
            and observed_start_token == expected_start_token)
