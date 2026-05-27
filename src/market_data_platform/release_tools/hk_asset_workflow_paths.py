from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_data_platform.paths import resolve_artifacts_root
from market_data_platform.repo_paths import find_repo_root

REPO_ROOT = find_repo_root(__file__)
ARTIFACTS_ROOT = resolve_artifacts_root()
ASSETS_ROOT = ARTIFACTS_ROOT / "assets"
REPORTS_ROOT = ARTIFACTS_ROOT / "reports"
RELEASES_ROOT = ARTIFACTS_ROOT / "releases"


@dataclass
class SnapshotBundle:
    instruments_file: Path
    etf_instruments_file: Path
    daily_dir: Path
    daily_clean_dir: Path
    etf_daily_dir: Path
    etf_daily_clean_dir: Path
    valuation_dir: Path
    ex_factors_dir: Path
    dividends_dir: Path
    shares_dir: Path
    industry_changes_dir: Path
    southbound_dir: Path
    pit_dir: Path | None
    exchange_rate_dir: Path | None
    financial_details_dir: Path | None
    universe_by_date: Path
    universe_symbols: Path
    universe_meta: Path | None


@dataclass
class Step:
    phase: str
    label: str
    command: list[str]
    summary_path: Path | None = None
    alias_target: Path | None = None
    alias_link: Path | None = None
    asset_name: str | None = None
    report_metadata: dict[str, Any] | None = None
    nonfatal_returncodes: tuple[int, ...] = ()
    depends_on_assets: tuple[str, ...] = ()


def current_snapshot_bundle(*, assets_root: Path = ASSETS_ROOT) -> SnapshotBundle:
    universe_root = assets_root / "universe"
    return SnapshotBundle(
        instruments_file=assets_root
        / "rqdata"
        / "hk"
        / "instruments"
        / "hk_all_instruments_latest.parquet",
        etf_instruments_file=assets_root
        / "rqdata"
        / "hk"
        / "instruments"
        / "hk_etf_instruments_latest.parquet",
        daily_dir=assets_root / "rqdata" / "hk" / "daily" / "hk_all_daily_latest",
        daily_clean_dir=assets_root
        / "rqdata"
        / "hk"
        / "daily"
        / "hk_all_daily_clean_latest",
        etf_daily_dir=assets_root / "rqdata" / "hk" / "daily" / "hk_etf_daily_latest",
        etf_daily_clean_dir=assets_root
        / "rqdata"
        / "hk"
        / "daily"
        / "hk_etf_daily_clean_latest",
        valuation_dir=assets_root / "rqdata" / "hk" / "valuation" / "hk_all_valuation_latest",
        ex_factors_dir=assets_root
        / "rqdata"
        / "hk"
        / "ex_factors"
        / "hk_all_ex_factors_latest",
        dividends_dir=assets_root / "rqdata" / "hk" / "dividends" / "hk_all_dividends_latest",
        shares_dir=assets_root / "rqdata" / "hk" / "shares" / "hk_all_shares_latest",
        industry_changes_dir=assets_root
        / "rqdata"
        / "hk"
        / "industry_changes"
        / "hk_all_industry_changes_latest",
        southbound_dir=assets_root
        / "rqdata"
        / "hk"
        / "southbound"
        / "hk_connect_southbound_latest",
        pit_dir=assets_root
        / "rqdata"
        / "hk"
        / "pit_financials"
        / "hk_all_2000_2025_full_market_latest",
        exchange_rate_dir=assets_root
        / "rqdata"
        / "hk"
        / "exchange_rate"
        / "hk_exchange_rate_latest",
        financial_details_dir=None,
        universe_by_date=universe_root / "hk_all_full_by_date.csv",
        universe_symbols=universe_root / "hk_all_full_symbols.txt",
        universe_meta=universe_root / "hk_all_full_by_date.meta.yml",
    )


def refreshed_snapshot_bundle(
    target_date: str,
    *,
    assets_root: Path = ASSETS_ROOT,
) -> SnapshotBundle:
    universe_root = assets_root / "universe"
    return SnapshotBundle(
        instruments_file=assets_root
        / "rqdata"
        / "hk"
        / "instruments"
        / f"hk_all_instruments_{target_date}.parquet",
        etf_instruments_file=assets_root
        / "rqdata"
        / "hk"
        / "instruments"
        / f"hk_etf_instruments_{target_date}.parquet",
        daily_dir=assets_root
        / "rqdata"
        / "hk"
        / "daily"
        / f"hk_all_2000_{target_date}_daily_final_refetched_latest",
        daily_clean_dir=assets_root
        / "rqdata"
        / "hk"
        / "daily"
        / f"hk_all_2000_{target_date}_daily_clean_refetched_latest",
        etf_daily_dir=assets_root
        / "rqdata"
        / "hk"
        / "daily"
        / f"hk_etf_2000_{target_date}_daily_latest",
        etf_daily_clean_dir=assets_root
        / "rqdata"
        / "hk"
        / "daily"
        / f"hk_etf_2000_{target_date}_daily_clean_latest",
        valuation_dir=assets_root
        / "rqdata"
        / "hk"
        / "valuation"
        / f"hk_all_2000_{target_date}_valuation_full_market_refetched_latest",
        ex_factors_dir=assets_root
        / "rqdata"
        / "hk"
        / "ex_factors"
        / f"hk_all_2000_{target_date}_ex_factors_full_market_latest",
        dividends_dir=assets_root
        / "rqdata"
        / "hk"
        / "dividends"
        / f"hk_all_2000_{target_date}_dividends_full_market_latest",
        shares_dir=assets_root
        / "rqdata"
        / "hk"
        / "shares"
        / f"hk_all_2000_{target_date}_shares_full_market_latest",
        industry_changes_dir=assets_root
        / "rqdata"
        / "hk"
        / "industry_changes"
        / f"hk_all_2000_{target_date}_industry_changes_full_market_latest",
        southbound_dir=assets_root
        / "rqdata"
        / "hk"
        / "southbound"
        / f"hk_connect_southbound_{target_date}",
        pit_dir=assets_root
        / "rqdata"
        / "hk"
        / "pit_financials"
        / "hk_all_2000_2025_full_market_latest",
        exchange_rate_dir=assets_root
        / "rqdata"
        / "hk"
        / "exchange_rate"
        / "hk_exchange_rate_latest",
        financial_details_dir=None,
        universe_by_date=universe_root / "hk_all_full_by_date.csv",
        universe_symbols=universe_root / "hk_all_full_symbols.txt",
        universe_meta=universe_root / "hk_all_full_by_date.meta.yml",
    )


def default_workflow_report_path(
    target_date: str,
    *,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    return reports_root / f"hk_asset_refresh_{target_date}.json"


def default_repair_queue_path(
    target_date: str,
    *,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    return reports_root / f"hk_asset_repair_queue_{target_date}.json"


def default_remaining_repair_candidates_path(
    target_date: str,
    *,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    return reports_root / f"hk_asset_remaining_repair_candidates_{target_date}.json"
