from __future__ import annotations

"""Stable package facade for rqdata asset helpers.

Keep only package-level defaults and thin compatibility hooks here. The heavier
public export surface lives in ``public_api.py``.
"""

from importlib import import_module
from types import ModuleType

from market_data_platform.artifacts import RQDATA_ASSETS_DIR as DEFAULT_RQDATA_ASSETS_DIR
from . import args as _args

DEFAULT_OUT_ROOT = DEFAULT_RQDATA_ASSETS_DIR.as_posix()
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIRROR_MAX_ATTEMPTS = 3
DEFAULT_MIRROR_BACKOFF_SECONDS = 1.0
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = 30.0


def _resolve_fields(args) -> tuple[list[str], dict]:
    from .shared import _load_hk_financial_fields, _resolve_fields_with_overrides

    return _resolve_fields_with_overrides(
        args,
        load_hk_financial_fields_override=_load_hk_financial_fields,
    )


_PUBLIC_API_MODULE: ModuleType | None = None
__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_HK_DAILY_FIELDS",
    "DEFAULT_HK_EXCHANGE_RATE_FIELDS",
    "DEFAULT_HK_INDUSTRY_CHANGE_LEVEL",
    "DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX",
    "DEFAULT_HK_INDUSTRY_SOURCE",
    "DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL",
    "DEFAULT_HK_SHARES_FIELDS",
    "DEFAULT_HK_SOUTHBOUND_TRADING_TYPES",
    "DEFAULT_INTRADAY_ASSET_ALIAS",
    "DEFAULT_INTRADAY_DAILY_ASSET_DIR",
    "DEFAULT_INTRADAY_DISTRIBUTION_NAME",
    "DEFAULT_PACKAGE_DAILY_SNAPSHOT",
    "DEFAULT_PACKAGE_INSTRUMENTS_FILE",
    "DEFAULT_PACKAGE_PRESET",
    "DEFAULT_HK_VALUATION_FIELDS",
    "DEFAULT_MIRROR_BACKOFF_SECONDS",
    "DEFAULT_MIRROR_MAX_ATTEMPTS",
    "DEFAULT_MIRROR_MAX_BACKOFF_SECONDS",
    "DEFAULT_OUT_ROOT",
    "DEFAULT_PIPELINE_FUNDAMENTALS_NAME",
    "MirrorFetchError",
    "MirrorQuotaError",
    "STARTER_HK_FINANCIAL_FIELDS",
    "build_hk_industry_labels_file",
    "build_hk_intraday_asset",
    "build_hk_daily_clean_layer",
    "build_hk_pit_fundamentals_file",
    "export_hk_instruments",
    "inspect_hk_asset_health",
    "inspect_hk_current_health",
    "inspect_hk_data_assets",
    "inspect_hk_intraday_health",
    "inspect_hk_pit_coverage",
    "list_hk_financial_fields",
    "mirror_hk_announcement",
    "mirror_hk_daily",
    "mirror_hk_dividends",
    "mirror_hk_exchange_rate",
    "mirror_hk_ex_factors",
    "mirror_hk_financial_details",
    "mirror_hk_industry_changes",
    "mirror_hk_instrument_industry",
    "mirror_hk_pit_financials",
    "patch_hk_pit_financials",
    "rebase_hk_asset_metadata",
    "mirror_hk_shares",
    "mirror_hk_southbound",
    "mirror_hk_valuation",
    "sync_hk_intraday",
]


def _load_public_api_module() -> ModuleType:
    global _PUBLIC_API_MODULE
    if _PUBLIC_API_MODULE is None:
        _PUBLIC_API_MODULE = import_module("market_data_platform.hk_assets.public_api")
    return _PUBLIC_API_MODULE


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = _load_public_api_module()
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
