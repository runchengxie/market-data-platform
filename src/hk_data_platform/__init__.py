"""Backward-compatible imports for the renamed market_data_platform package."""

from __future__ import annotations

from market_data_platform import __version__ as __version__
from market_data_platform.deprecations import warn_deprecated_import

warn_deprecated_import("hk_data_platform", "market_data_platform")
