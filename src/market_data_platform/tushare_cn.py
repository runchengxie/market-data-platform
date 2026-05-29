"""Backward-compatible imports for the CN TuShare provider."""

from market_data_platform.deprecations import warn_deprecated_import
from market_data_platform.providers.tushare_cn import *  # noqa: F403

warn_deprecated_import(
    "market_data_platform.tushare_cn",
    "market_data_platform.providers.tushare_cn",
)
