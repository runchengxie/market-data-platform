"""Project-specific exceptions."""

from __future__ import annotations


class RQDataTickError(Exception):
    """Base class for package errors."""


class ProviderRequestError(RQDataTickError):
    """Provider request failed with a classified error category."""

    def __init__(self, category: str, stage: str, message: str) -> None:
        self.category = category
        self.stage = stage
        super().__init__(f"{category} during {stage}: {message}")


class DownloadError(RQDataTickError):
    """Download failed."""
