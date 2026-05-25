from __future__ import annotations

import os
from pathlib import Path

HK_DATA_PLATFORM_ROOT_ENV = "HK_DATA_PLATFORM_ROOT"
CSTREE_ARTIFACTS_ROOT_ENV = "CSTREE_ARTIFACTS_ROOT"

CURRENT_CONTRACT_RELATIVE_PATH = Path("metadata") / "current_assets" / "hk_current.json"
DATASET_REGISTRY_RELATIVE_PATH = Path("metadata") / "dataset_registry.csv"

ASSET_PATH_SPECS: dict[str, tuple[str, ...]] = {
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


def resolve_artifacts_root(value: str | Path | None = None) -> Path:
    raw = (
        str(value).strip()
        if value is not None
        else os.environ.get(HK_DATA_PLATFORM_ROOT_ENV)
        or os.environ.get(CSTREE_ARTIFACTS_ROOT_ENV)
        or "artifacts"
    )
    return Path(raw).expanduser().resolve()


def current_contract_path(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / CURRENT_CONTRACT_RELATIVE_PATH


def dataset_registry_path(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / DATASET_REGISTRY_RELATIVE_PATH


def candidate_asset_paths(artifacts_root: str | Path | None = None) -> dict[str, Path]:
    root = resolve_artifacts_root(artifacts_root)
    return {key: root.joinpath(*parts) for key, parts in ASSET_PATH_SPECS.items()}
