from __future__ import annotations

from .package_assets import AVAILABLE_PART_CHOICES

REFRESH_ASSETS = (
    "instruments",
    "etf_instruments",
    "daily",
    "daily_clean",
    "etf_daily",
    "etf_daily_clean",
    "valuation",
    "ex_factors",
    "dividends",
    "shares",
    "industry_changes",
    "southbound",
)
INSPECT_ASSETS = (
    "daily",
    "daily_clean",
    "valuation",
    "ex_factors",
    "dividends",
    "shares",
    "industry_changes",
    "southbound",
)
DEFAULT_PHASES = ("refresh", "inspect", "package")
DEFAULT_PACKAGE_PARTS = tuple(part for part in AVAILABLE_PART_CHOICES if part != "announcement")
PATCH_MERGE_SUPPORTED_ASSET_ORDER = ("daily", "valuation", "ex_factors", "dividends", "shares")
PATCH_MERGE_SUPPORTED_ASSETS = frozenset(PATCH_MERGE_SUPPORTED_ASSET_ORDER)
REPAIR_ASSETS = PATCH_MERGE_SUPPORTED_ASSET_ORDER
DEFAULT_DAILY_PATCH_LOOKBACK_DAYS = 20
DEFAULT_DATED_PATCH_LOOKBACK_DAYS = 40
PROVIDER_PERMISSION_EXIT_CODE = 78
