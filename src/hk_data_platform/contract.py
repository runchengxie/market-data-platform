from __future__ import annotations

from market_data_platform.contract import *  # noqa: F403
from market_data_platform.deprecations import warn_deprecated_import

warn_deprecated_import("hk_data_platform.contract", "market_data_platform.contract")
