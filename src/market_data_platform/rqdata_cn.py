"""Backward-compatible imports for the CN RQData provider."""

from market_data_platform.deprecations import warn_deprecated_import
from market_data_platform.providers.rqdata_cn import *  # noqa: F403

warn_deprecated_import(
    "market_data_platform.rqdata_cn",
    "market_data_platform.providers.rqdata_cn",
)
