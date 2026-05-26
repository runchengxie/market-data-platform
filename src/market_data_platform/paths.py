from __future__ import annotations

import os
from pathlib import Path

DATA_PLATFORM_ROOT_ENV = "DATA_PLATFORM_ROOT"
HK_DATA_PLATFORM_ROOT_ENV = "HK_DATA_PLATFORM_ROOT"
CSTREE_ARTIFACTS_ROOT_ENV = "CSTREE_ARTIFACTS_ROOT"

SUPPORTED_MARKETS = {"hk", "cn"}
SUPPORTED_PROVIDERS_BY_MARKET = {
    "hk": {"rqdata"},
    "cn": {"rqdata", "tushare"},
}


def normalize_market(market: str | None = None) -> str:
    value = str(market or "hk").strip().lower()
    if value not in SUPPORTED_MARKETS:
        supported = ", ".join(sorted(SUPPORTED_MARKETS))
        raise ValueError(f"Unsupported market '{value}'. Supported markets: {supported}.")
    return value


def normalize_provider(provider: str | None = None, *, market: str | None = None) -> str:
    market = normalize_market(market)
    value = str(provider or "rqdata").strip().lower()
    supported = SUPPORTED_PROVIDERS_BY_MARKET[market]
    if value not in supported:
        available = ", ".join(sorted(supported))
        raise ValueError(
            f"Unsupported provider '{value}' for market '{market}'. Supported providers: "
            f"{available}."
        )
    return value


def current_contract_relative_path(market: str | None = None) -> Path:
    market = normalize_market(market)
    return Path("metadata") / "current_assets" / f"{market}_current.json"


CURRENT_CONTRACT_RELATIVE_PATH = current_contract_relative_path("hk")
DATASET_REGISTRY_RELATIVE_PATH = Path("metadata") / "dataset_registry.csv"

HK_ASSET_PATH_SPECS: dict[str, tuple[str, ...]] = {
    "daily": ("assets", "rqdata", "hk", "daily", "hk_all_daily_latest"),
    "daily_clean": ("assets", "rqdata", "hk", "daily", "hk_all_daily_clean_latest"),
    "intraday": ("assets", "rqdata", "hk", "intraday", "hk_intraday_latest"),
    "tick_depth_raw": ("assets", "rqdata", "hk", "tick_depth", "hk_tick_depth_latest"),
    "tick_depth_daily": (
        "assets",
        "rqdata",
        "hk",
        "tick_depth_daily",
        "hk_tick_depth_daily_latest",
    ),
    "execution_cost_model": (
        "assets",
        "rqdata",
        "hk",
        "execution_cost",
        "hk_execution_cost_model_latest",
    ),
    "etf_daily": ("assets", "rqdata", "hk", "daily", "hk_etf_daily_latest"),
    "etf_daily_clean": ("assets", "rqdata", "hk", "daily", "hk_etf_daily_clean_latest"),
    "etf_instruments": (
        "assets",
        "rqdata",
        "hk",
        "instruments",
        "hk_etf_instruments_latest.parquet",
    ),
    "valuation": ("assets", "rqdata", "hk", "valuation", "hk_all_valuation_latest"),
    "instruments": (
        "assets",
        "rqdata",
        "hk",
        "instruments",
        "hk_all_instruments_latest.parquet",
    ),
    "pit": ("assets", "rqdata", "hk", "pit_financials", "hk_all_2000_2025_full_market_latest"),
    "ex_factors": ("assets", "rqdata", "hk", "ex_factors", "hk_all_ex_factors_latest"),
    "dividends": ("assets", "rqdata", "hk", "dividends", "hk_all_dividends_latest"),
    "shares": ("assets", "rqdata", "hk", "shares", "hk_all_shares_latest"),
    "exchange_rate": ("assets", "rqdata", "hk", "exchange_rate", "hk_exchange_rate_latest"),
    "southbound": ("assets", "rqdata", "hk", "southbound", "hk_connect_southbound_latest"),
    "financial_details": (
        "assets",
        "rqdata",
        "hk",
        "financial_details",
        "hk_financial_details_latest",
    ),
    "industry_changes": (
        "assets",
        "rqdata",
        "hk",
        "industry_changes",
        "hk_all_industry_changes_latest",
    ),
    "universe_by_date": ("assets", "universe", "hk_all_full_by_date.csv"),
    "universe_symbols": ("assets", "universe", "hk_all_full_symbols.txt"),
    "universe_meta": ("assets", "universe", "hk_all_full_by_date.meta.yml"),
}

CN_ASSET_PATH_SPECS: dict[str, tuple[str, ...]] = {
    "daily": ("assets", "rqdata", "cn", "daily", "cn_all_daily_latest"),
    "daily_clean": ("assets", "rqdata", "cn", "daily", "cn_all_daily_clean_latest"),
    "valuation": ("assets", "rqdata", "cn", "valuation", "cn_all_valuation_latest"),
    "instruments": (
        "assets",
        "rqdata",
        "cn",
        "instruments",
        "cn_all_instruments_latest.parquet",
    ),
    "pit": ("assets", "rqdata", "cn", "pit_financials", "cn_all_pit_financials_latest"),
    "ex_factors": ("assets", "rqdata", "cn", "ex_factors", "cn_all_ex_factors_latest"),
    "dividends": ("assets", "rqdata", "cn", "dividends", "cn_all_dividends_latest"),
    "shares": ("assets", "rqdata", "cn", "shares", "cn_all_shares_latest"),
    "industry": ("assets", "rqdata", "cn", "industry", "cn_industry_latest"),
    "industry_citic": ("assets", "rqdata", "cn", "industry_citic", "cn_industry_citic_latest"),
    "industry_sw": ("assets", "rqdata", "cn", "industry_sw", "cn_industry_sw_latest"),
    "st_flags": ("assets", "rqdata", "cn", "st_flags", "cn_st_flags_latest"),
    "suspend": ("assets", "rqdata", "cn", "suspend", "cn_suspend_latest"),
    "limit_status": ("assets", "rqdata", "cn", "limit_status", "cn_limit_status_latest"),
    "index_components": (
        "assets",
        "rqdata",
        "cn",
        "index_components",
        "cn_index_components_latest",
    ),
    "northbound": ("assets", "rqdata", "cn", "northbound", "cn_northbound_latest"),
    "universe_by_date": ("assets", "universe", "cn_all_full_by_date.csv"),
    "universe_symbols": ("assets", "universe", "cn_all_full_symbols.txt"),
    "universe_meta": ("assets", "universe", "cn_all_full_by_date.meta.yml"),
}

TUSHARE_CN_ASSET_PATH_SPECS: dict[str, tuple[str, ...]] = {
    "instruments": (
        "assets",
        "tushare",
        "cn",
        "instruments",
        "cn_all_instruments_latest.parquet",
    ),
    "trade_cal": ("assets", "tushare", "cn", "trade_cal", "cn_trade_cal_latest.parquet"),
    "daily": ("assets", "tushare", "cn", "daily", "cn_all_daily_latest"),
    "adj_factor": (
        "assets",
        "tushare",
        "cn",
        "adj_factor",
        "cn_all_adj_factor_latest",
    ),
    "daily_basic": (
        "assets",
        "tushare",
        "cn",
        "daily_basic",
        "cn_all_daily_basic_latest",
    ),
    "limit_status": (
        "assets",
        "tushare",
        "cn",
        "limit_status",
        "cn_limit_status_latest",
    ),
    "daily_clean": ("assets", "tushare", "cn", "daily", "cn_all_daily_clean_latest"),
    "universe_by_date": ("assets", "universe", "cn_all_full_by_date.csv"),
    "universe_symbols": ("assets", "universe", "cn_all_full_symbols.txt"),
    "universe_meta": ("assets", "universe", "cn_all_full_by_date.meta.yml"),
}

ASSET_PATH_SPECS_BY_MARKET_PROVIDER: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "hk": {"rqdata": HK_ASSET_PATH_SPECS},
    "cn": {
        "rqdata": CN_ASSET_PATH_SPECS,
        "tushare": TUSHARE_CN_ASSET_PATH_SPECS,
    },
}

# Backward-compatible alias for existing HK callers.
ASSET_PATH_SPECS = HK_ASSET_PATH_SPECS


def resolve_artifacts_root(value: str | Path | None = None) -> Path:
    raw = (
        str(value).strip()
        if value is not None
        else os.environ.get(DATA_PLATFORM_ROOT_ENV)
        or os.environ.get(HK_DATA_PLATFORM_ROOT_ENV)
        or os.environ.get(CSTREE_ARTIFACTS_ROOT_ENV)
        or "artifacts"
    )
    return Path(raw).expanduser().resolve()


def current_contract_path(
    artifacts_root: str | Path | None = None,
    *,
    market: str | None = None,
) -> Path:
    return resolve_artifacts_root(artifacts_root) / current_contract_relative_path(market)


def dataset_registry_path(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / DATASET_REGISTRY_RELATIVE_PATH


def candidate_asset_paths(
    artifacts_root: str | Path | None = None,
    *,
    market: str | None = None,
    provider: str | None = None,
) -> dict[str, Path]:
    root = resolve_artifacts_root(artifacts_root)
    market = normalize_market(market)
    provider = normalize_provider(provider, market=market)
    specs = ASSET_PATH_SPECS_BY_MARKET_PROVIDER[market][provider]
    return {key: root.joinpath(*parts) for key, parts in specs.items()}
