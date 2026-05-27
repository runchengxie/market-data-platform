from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from market_data_platform.artifacts import RQDATA_ASSETS_DIR as DEFAULT_RQDATA_ASSETS_DIR
from . import args as _args
from .asset_health import inspect_hk_asset_health
from .audit_assets import inspect_hk_data_assets
from .build import build_hk_industry_labels_file, build_hk_pit_fundamentals_file
from .clean_daily import build_hk_daily_clean_layer
from .coverage import inspect_hk_pit_coverage
from .current_health import inspect_hk_current_health
from .industry_ops import (
    DEFAULT_HK_INDUSTRY_CHANGE_LEVEL,
    DEFAULT_HK_INDUSTRY_SOURCE,
    DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL,
)
from .intraday_asset import build_hk_intraday_asset
from .intraday_health import inspect_hk_intraday_health
from .intraday_sync import (
    DEFAULT_INTRADAY_ASSET_ALIAS,
    DEFAULT_INTRADAY_DAILY_ASSET_DIR,
    DEFAULT_INTRADAY_DISTRIBUTION_NAME,
    DEFAULT_PACKAGE_DAILY_SNAPSHOT,
    DEFAULT_PACKAGE_INSTRUMENTS_FILE,
    DEFAULT_PACKAGE_PRESET,
    sync_hk_intraday,
)
from .mirror_daily import mirror_hk_daily
from .mirror_dated import (
    mirror_hk_announcement,
    mirror_hk_dividends,
    mirror_hk_ex_factors,
    mirror_hk_exchange_rate,
    mirror_hk_shares,
    mirror_hk_valuation,
)
from .mirror_financial import (
    export_hk_instruments,
    list_hk_financial_fields,
    mirror_hk_financial_details,
    mirror_hk_pit_financials,
    patch_hk_pit_financials,
)
from .mirror_industry import (
    mirror_hk_industry_changes,
    mirror_hk_instrument_industry,
    mirror_hk_southbound,
)
from .rebase_metadata import rebase_hk_asset_metadata
from .request_groups import DEFAULT_HK_INSTRUMENTS_FILENAME_PREFIX
from .shared import (
    DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX,
    DEFAULT_PIPELINE_FUNDAMENTALS_NAME,
)

DEFAULT_OUT_ROOT = DEFAULT_RQDATA_ASSETS_DIR.as_posix()
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIRROR_MAX_ATTEMPTS = 3
DEFAULT_MIRROR_BACKOFF_SECONDS = 1.0
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class RQDataAssetCommandSpec:
    name: str
    help: str
    add_args: Callable[[argparse.ArgumentParser], None]
    runner: Callable[..., int | None]
    requires_client: bool = False


@dataclass(frozen=True)
class RQDataAssetArgsBuilder:
    func: Callable[..., None]
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __call__(self, parser: argparse.ArgumentParser) -> None:
        self.func(parser, **self.kwargs)


COMMON_MIRROR_KWARGS = {
    "default_out_root": DEFAULT_OUT_ROOT,
    "max_attempts_default": DEFAULT_MIRROR_MAX_ATTEMPTS,
    "backoff_seconds_default": DEFAULT_MIRROR_BACKOFF_SECONDS,
    "max_backoff_seconds_default": DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
}
BATCHED_MIRROR_KWARGS = {
    "default_batch_size": DEFAULT_BATCH_SIZE,
    **COMMON_MIRROR_KWARGS,
}

LIST_HK_FINANCIAL_FIELDS_ARGS = RQDataAssetArgsBuilder(_args.add_list_hk_financial_fields_args)
HK_INSTRUMENTS_EXPORT_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_instruments_export_args,
    {
        "default_out_root": DEFAULT_OUT_ROOT,
        "default_instruments_filename_prefix": DEFAULT_HK_INSTRUMENTS_FILENAME_PREFIX,
    },
)
HK_DAILY_MIRROR_ARGS = RQDataAssetArgsBuilder(_args.add_hk_daily_mirror_args, BATCHED_MIRROR_KWARGS)
HK_VALUATION_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_valuation_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_EX_FACTORS_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_ex_factors_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_DIVIDENDS_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_dividends_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_SHARES_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_shares_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_EXCHANGE_RATE_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_exchange_rate_mirror_args,
    COMMON_MIRROR_KWARGS,
)
HK_ANNOUNCEMENT_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_announcement_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_SOUTHBOUND_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_southbound_mirror_args,
    COMMON_MIRROR_KWARGS,
)
HK_INSTRUMENT_INDUSTRY_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_instrument_industry_mirror_args,
    {
        **BATCHED_MIRROR_KWARGS,
        "default_industry_source": DEFAULT_HK_INDUSTRY_SOURCE,
        "default_industry_level": DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL,
    },
)
HK_INDUSTRY_CHANGES_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_industry_changes_mirror_args,
    {
        **BATCHED_MIRROR_KWARGS,
        "default_industry_source": DEFAULT_HK_INDUSTRY_SOURCE,
        "default_change_level": DEFAULT_HK_INDUSTRY_CHANGE_LEVEL,
    },
)
HK_PIT_FINANCIAL_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_financial_mirror_args,
    {
        **BATCHED_MIRROR_KWARGS,
        "supports_quarter_chunk": True,
    },
)
HK_FINANCIAL_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_financial_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_PIT_PATCH_MIRROR_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_pit_patch_mirror_args,
    BATCHED_MIRROR_KWARGS,
)
HK_PIT_FUNDAMENTALS_BUILD_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_pit_fundamentals_build_args,
    {"default_pipeline_fundamentals_name": DEFAULT_PIPELINE_FUNDAMENTALS_NAME},
)
HK_INDUSTRY_LABELS_BUILD_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_industry_labels_build_args,
    {"default_industry_labels_filename_prefix": DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX},
)
HK_PIT_COVERAGE_ARGS = RQDataAssetArgsBuilder(_args.add_hk_pit_coverage_args)
HK_ASSET_HEALTH_ARGS = RQDataAssetArgsBuilder(_args.add_hk_asset_health_args)
HK_CURRENT_HEALTH_ARGS = RQDataAssetArgsBuilder(_args.add_hk_current_health_args)
HK_ASSET_METADATA_REBASE_ARGS = RQDataAssetArgsBuilder(_args.add_hk_asset_metadata_rebase_args)
HK_DATA_ASSET_AUDIT_ARGS = RQDataAssetArgsBuilder(_args.add_hk_data_asset_audit_args)
HK_INTRADAY_HEALTH_ARGS = RQDataAssetArgsBuilder(_args.add_hk_intraday_health_args)
HK_INTRADAY_ASSET_BUILD_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_intraday_asset_build_args,
    {"default_out_root": DEFAULT_OUT_ROOT},
)
HK_INTRADAY_SYNC_ARGS = RQDataAssetArgsBuilder(
    _args.add_hk_intraday_sync_args,
    {
        "default_out_root": DEFAULT_OUT_ROOT,
        "default_daily_asset_dir": DEFAULT_INTRADAY_DAILY_ASSET_DIR,
        "default_asset_alias": DEFAULT_INTRADAY_ASSET_ALIAS,
        "default_package_preset": DEFAULT_PACKAGE_PRESET,
        "default_package_daily_snapshot": DEFAULT_PACKAGE_DAILY_SNAPSHOT,
        "default_package_instruments_file": DEFAULT_PACKAGE_INSTRUMENTS_FILE,
        "default_distribution_name": DEFAULT_INTRADAY_DISTRIBUTION_NAME,
    },
)
HK_DAILY_CLEAN_LAYER_ARGS = RQDataAssetArgsBuilder(_args.add_hk_daily_clean_layer_args)


def rqdata_asset_command_specs() -> Sequence[RQDataAssetCommandSpec]:
    return (
        RQDataAssetCommandSpec(
            name="list-hk-financial-fields",
            help="List supported HK financial field names for PIT/details APIs",
            add_args=LIST_HK_FINANCIAL_FIELDS_ARGS,
            runner=list_hk_financial_fields,
        ),
        RQDataAssetCommandSpec(
            name="export-hk-instruments",
            help="Export HK instrument metadata such as order_book_id, listed_date, and round_lot",
            add_args=HK_INSTRUMENTS_EXPORT_ARGS,
            runner=export_hk_instruments,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-daily",
            help="Mirror HK daily OHLCV + turnover data into parquet + manifest assets",
            add_args=HK_DAILY_MIRROR_ARGS,
            runner=mirror_hk_daily,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-valuation",
            help="Mirror HK daily valuation factors into parquet + manifest assets",
            add_args=HK_VALUATION_MIRROR_ARGS,
            runner=mirror_hk_valuation,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-pit-financials",
            help="Mirror HK PIT financial statements into parquet + manifest assets",
            add_args=HK_PIT_FINANCIAL_MIRROR_ARGS,
            runner=mirror_hk_pit_financials,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="patch-hk-pit-financials",
            help="Build a HK PIT base + recent-quarter patch snapshot",
            add_args=HK_PIT_PATCH_MIRROR_ARGS,
            runner=patch_hk_pit_financials,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-financial-details",
            help="Mirror HK raw financial detail items into parquet + manifest assets",
            add_args=HK_FINANCIAL_MIRROR_ARGS,
            runner=mirror_hk_financial_details,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-ex-factors",
            help="Mirror HK ex-factor history into parquet + manifest assets",
            add_args=HK_EX_FACTORS_MIRROR_ARGS,
            runner=mirror_hk_ex_factors,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-dividends",
            help="Mirror HK dividend history into parquet + manifest assets",
            add_args=HK_DIVIDENDS_MIRROR_ARGS,
            runner=mirror_hk_dividends,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-shares",
            help="Mirror HK share-capital history into parquet + manifest assets",
            add_args=HK_SHARES_MIRROR_ARGS,
            runner=mirror_hk_shares,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-exchange-rate",
            help="Mirror HK exchange-rate history into parquet + manifest assets",
            add_args=HK_EXCHANGE_RATE_MIRROR_ARGS,
            runner=mirror_hk_exchange_rate,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-announcement",
            help="Mirror HK company announcements into parquet + manifest assets",
            add_args=HK_ANNOUNCEMENT_MIRROR_ARGS,
            runner=mirror_hk_announcement,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-southbound",
            help="Mirror HK southbound eligibility history into parquet + manifest assets",
            add_args=HK_SOUTHBOUND_MIRROR_ARGS,
            runner=mirror_hk_southbound,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-instrument-industry",
            help="Mirror HK instrument-industry snapshots into parquet + manifest assets",
            add_args=HK_INSTRUMENT_INDUSTRY_MIRROR_ARGS,
            runner=mirror_hk_instrument_industry,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="mirror-hk-industry-changes",
            help="Mirror HK industry membership intervals into parquet + manifest assets",
            add_args=HK_INDUSTRY_CHANGES_MIRROR_ARGS,
            runner=mirror_hk_industry_changes,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="build-hk-pit-fundamentals",
            help="Build a pipeline-readable fundamentals file from an HK PIT mirror asset",
            add_args=HK_PIT_FUNDAMENTALS_BUILD_ARGS,
            runner=build_hk_pit_fundamentals_file,
        ),
        RQDataAssetCommandSpec(
            name="build-hk-industry-labels",
            help="Build local HK industry label files from an industry_changes asset",
            add_args=HK_INDUSTRY_LABELS_BUILD_ARGS,
            runner=build_hk_industry_labels_file,
        ),
        RQDataAssetCommandSpec(
            name="inspect-hk-pit-coverage",
            help="Inspect HK PIT fundamentals coverage for selected raw or derived features",
            add_args=HK_PIT_COVERAGE_ARGS,
            runner=inspect_hk_pit_coverage,
        ),
        RQDataAssetCommandSpec(
            name="inspect-hk-asset-health",
            help="Inspect local HK asset snapshots for latest-date coverage and field-level gaps",
            add_args=HK_ASSET_HEALTH_ARGS,
            runner=inspect_hk_asset_health,
        ),
        RQDataAssetCommandSpec(
            name="inspect-hk-current-health",
            help=(
                "Inspect hk_current contract and alias alignment without scanning "
                "large parquet assets"
            ),
            add_args=HK_CURRENT_HEALTH_ARGS,
            runner=inspect_hk_current_health,
        ),
        RQDataAssetCommandSpec(
            name="rebase-hk-asset-metadata",
            help="Rebase embedded repository prefixes in live HK asset metadata after moving the checkout",
            add_args=HK_ASSET_METADATA_REBASE_ARGS,
            runner=rebase_hk_asset_metadata,
        ),
        RQDataAssetCommandSpec(
            name="inspect-hk-data-assets",
            help="Audit HK data assets, freshness, repair candidates, and conservative prune plans",
            add_args=HK_DATA_ASSET_AUDIT_ARGS,
            runner=inspect_hk_data_assets,
        ),
        RQDataAssetCommandSpec(
            name="inspect-hk-intraday-health",
            help=(
                "Inspect local HK 5m parquet files for duplicate bars, missing bars, "
                "and daily reconciliation"
            ),
            add_args=HK_INTRADAY_HEALTH_ARGS,
            runner=inspect_hk_intraday_health,
        ),
        RQDataAssetCommandSpec(
            name="build-hk-intraday-asset",
            help="Package local HK 5m parquet/cache files into a formal reusable asset snapshot",
            add_args=HK_INTRADAY_ASSET_BUILD_ARGS,
            runner=build_hk_intraday_asset,
        ),
        RQDataAssetCommandSpec(
            name="sync-hk-intraday",
            help=(
                "Download HK intraday cache, inspect it, repoint the formal asset "
                "alias, and optionally package/release it"
            ),
            add_args=HK_INTRADAY_SYNC_ARGS,
            runner=sync_hk_intraday,
            requires_client=True,
        ),
        RQDataAssetCommandSpec(
            name="build-hk-daily-clean-layer",
            help="Build a conservative cleaned HK daily snapshot without mutating the source asset",
            add_args=HK_DAILY_CLEAN_LAYER_ARGS,
            runner=build_hk_daily_clean_layer,
        ),
    )
