"""Small stderr progress helper for long local I/O commands."""

from __future__ import annotations

import sys
import time
from typing import TextIO


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "0B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


class ProgressBar:
    """Render a compact single-line progress bar to stderr."""

    def __init__(
        self,
        *,
        label: str,
        total_units: int,
        total_bytes: int | None = None,
        enabled: bool = False,
        stream: TextIO | None = None,
        min_interval_seconds: float = 0.2,
    ) -> None:
        self.label = label
        self.total_units = max(0, total_units)
        self.total_bytes = total_bytes
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.min_interval_seconds = min_interval_seconds
        self.completed_units = 0
        self.completed_bytes = 0
        self._last_render = 0.0
        self._last_width = 0
        self._closed = False

    def update(
        self,
        *,
        units: int = 1,
        bytes_done: int = 0,
        suffix: str | None = None,
        force: bool = False,
    ) -> None:
        if not self.enabled or self._closed:
            return
        self.completed_units += units
        self.completed_bytes += bytes_done
        now = time.monotonic()
        if not force and now - self._last_render < self.min_interval_seconds:
            return
        self._last_render = now
        self._render(suffix=suffix)

    def close(self, *, suffix: str | None = None) -> None:
        if not self.enabled or self._closed:
            return
        self._render(suffix=suffix)
        self.stream.write("\n")
        self.stream.flush()
        self._closed = True

    def _render(self, *, suffix: str | None) -> None:
        total = self.total_units or max(self.completed_units, 1)
        units = min(self.completed_units, total)
        pct = min(1.0, units / total)
        width = 28
        filled = int(width * pct)
        bar = "#" * filled + "-" * (width - filled)
        line = (
            f"{self.label} [{bar}] {pct * 100:5.1f}% "
            f"{self.completed_units}/{self.total_units}"
        )
        if self.total_bytes is not None:
            line += f" {format_bytes(self.completed_bytes)}/{format_bytes(self.total_bytes)}"
        if suffix:
            line += f" {suffix}"
        padding = " " * max(0, self._last_width - len(line))
        self.stream.write(f"\r{line}{padding}")
        self.stream.flush()
        self._last_width = len(line)
