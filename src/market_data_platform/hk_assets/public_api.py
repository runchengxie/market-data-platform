from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from market_data_platform.artifacts import (
    RQDATA_ASSETS_DIR as DEFAULT_RQDATA_ASSETS_DIR,
)
from market_data_platform.data_providers import _fetch_daily_rqdata
from . import args as _args
from .asset_io import (
    _audit_record,
    _chunked,
    _daily_audit_record,
    _dated_audit_record,
    _ensure_requested_fields,
    _field_coverage_template,
    _load_existing_daily_entry,
    _load_existing_dated_entry,
    _load_existing_entry,
    _prepare_asset_frame,
    _prepare_daily_asset_frame,
    _prepare_dated_asset_frame,
    _reset_frame_index,
    _update_field_coverage,
    _write_audit_csv,
    _write_daily_audit_csv,
    _write_daily_symbol_frame,
    _write_dated_audit_csv,
    _write_dated_symbol_frame,
    _write_symbol_frame,
)
from .fetch_runtime import (
    _ensure_rqdatac_hk_plugin,
    _extract_invalid_field_name,
    _fetch_hk_dividends_direct,
    _fetch_hk_ex_factors_direct,
    _fetch_hk_shares_direct,
    _retry_fetch,
)
from .industry_ops import (
    DEFAULT_HK_SOUTHBOUND_TRADING_TYPES,
    _build_hk_industry_catalog,
    _prepare_hk_industry_change_frame,
    _prepare_hk_instrument_industry_frame,
    _resolve_hk_industry_change_level,
    _resolve_hk_industry_source,
    _resolve_hk_instrument_industry_level,
    _resolve_hk_rebalance_frequency,
    _resolve_hk_snapshot_dates,
    _resolve_hk_southbound_trading_types,
    _resolve_hk_trading_snapshot_dates,
)
from .manifest_ops import (
    _build_daily_manifest,
    _build_dated_manifest,
    _build_manifest,
    _validate_daily_resume_inputs,
    _validate_dated_resume_inputs,
    _validate_global_daily_resume_inputs,
    _validate_resume_inputs,
)
from .models import (
    DailyMirrorAuditRecord,
    DailyMirrorEntry,
    DatedMirrorAuditRecord,
    DatedMirrorEntry,
    MirrorAuditRecord,
    MirrorEntry,
    MirrorFetchError,
    MirrorQuotaError,
)
from .request_groups import (
    DEFAULT_HK_INSTRUMENTS_DIR,
    DEFAULT_HK_INSTRUMENTS_FILENAME_PREFIX,
    _build_default_dated_request_groups,
    _candidate_hk_instruments_snapshot_paths,
    _default_hk_instruments_out_path,
    _load_cached_hk_instruments_frame,
    _normalize_hk_dated_payload,
    _normalize_hk_valuation_payload,
    _resolve_hk_dated_request_groups,
    _resolve_instrument_symbol_filter,
    _resolve_symbols,
    _resolve_symbols_from_config,
    _uses_hk_unique_ids,
)
from .shared import (
    DATE_TEXT_OUTPUT_COLUMNS,
    DEFAULT_HK_DAILY_FIELDS,
    DEFAULT_HK_EXCHANGE_RATE_FIELDS,
    DEFAULT_HK_INDUSTRY_CHANGE_LEVEL,
    DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX,
    DEFAULT_HK_INDUSTRY_SOURCE,
    DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL,
    DEFAULT_HK_SHARES_FIELDS,
    DEFAULT_HK_VALUATION_FIELDS,
    DEFAULT_PIPELINE_FUNDAMENTALS_NAME,
    DERIVED_PIT_FEATURES,
    HK_INDUSTRY_HIERARCHY_COLUMNS,
    HK_INSTRUMENT_INDUSTRY_FIELDS,
    PIT_METADATA_COLUMNS,
    STARTER_HK_FINANCIAL_FIELDS,
    _coerce_bool,
    _dedupe_preserve_order,
    _drop_conflicting_index_levels,
    _git_metadata,
    _load_existing_text_list,
    _load_hk_financial_fields,
    _load_manifest,
    _load_symbols_from_by_date,
    _load_text_list,
    _normalize_absolute_date,
    _normalize_field_list,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _path_mtime_iso,
    _prepare_daily_output_dir,
    _prepare_output_dir,
    _resolve_daily_fields,
    _resolve_default_plus_explicit_fields,
    _resolve_fields_with_overrides,
    _resolve_optional_explicit_fields,
    _resolve_path,
    _resolve_universe_by_date_columns,
    _split_daily_range_by_year,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)

DEFAULT_OUT_ROOT = DEFAULT_RQDATA_ASSETS_DIR.as_posix()
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIRROR_MAX_ATTEMPTS = 3
DEFAULT_MIRROR_BACKOFF_SECONDS = 1.0
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = 30.0

# Keep this package focused on stable exports for tests and programmatic use.
# New CLI command registration belongs in ``command_registry.py`` instead of
# growing another layer of wrappers here.


def _resolve_fields(args) -> tuple[list[str], dict]:
    return _resolve_fields_with_overrides(
        args,
        load_hk_financial_fields_override=_load_hk_financial_fields,
    )


from .asset_health import inspect_hk_asset_health
from .audit_assets import inspect_hk_data_assets
from .build import (
    _build_filtered_universe_by_date,
    _default_hk_industry_labels_path,
    _default_pipeline_fundamentals_path,
    _derive_hk_industry_labels,
    _industry_labels_manifest_path,
    _load_industry_changes_frame,
    _load_trade_date_grid_from_daily_asset_dir,
    _load_universe_by_date_frame,
    _pipeline_fundamentals_manifest_path,
    _resolve_build_fields,
    _resolve_hk_industry_label_grid,
    _resolve_hk_industry_labels_out_path,
    _resolve_hk_label_frequency,
    _resolve_industry_changes_asset_dir,
    _resolve_optional_absolute_date,
    _resolve_pipeline_fundamentals_out_path,
    _resolve_pit_asset_dir,
    _sample_trade_date_grid,
    _write_symbol_list,
    build_hk_industry_labels_file,
    build_hk_pit_fundamentals_file,
)
from .clean_daily import build_hk_daily_clean_layer
from .coverage import (
    _assess_trainable_fill_dependence,
    _build_trainable_period_grid,
    _compute_pit_coverage_series,
    _estimate_trainable_pit_coverage,
    _is_supported_pit_coverage_feature,
    _render_hk_pit_coverage_text,
    _resolve_pit_coverage_features,
    _resolve_trainable_pit_features,
    _resolve_trainable_pit_settings,
    inspect_hk_pit_coverage,
)
from .current_health import inspect_hk_current_health
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
from .mirror_workflow import (
    _collect_pending_mirror_items,
    _mirror_dataset,
    _mirror_dated_dataset,
    _run_partitioned_mirror_batches,
)
from .rebase_metadata import rebase_hk_asset_metadata


def add_list_hk_financial_fields_args(parser: argparse.ArgumentParser) -> None:
    _args.add_list_hk_financial_fields_args(parser)


def add_hk_instruments_export_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_instruments_export_args(
        parser,
        default_out_root=DEFAULT_OUT_ROOT,
        default_instruments_filename_prefix=DEFAULT_HK_INSTRUMENTS_FILENAME_PREFIX,
    )


def add_hk_daily_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_daily_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_valuation_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_valuation_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_dated_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    supports_fields: bool = False,
    field_help: str | None = None,
    fields_file_help: str | None = None,
) -> None:
    _args.add_hk_dated_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
        supports_fields=supports_fields,
        field_help=field_help,
        fields_file_help=fields_file_help,
    )


def add_hk_ex_factors_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_ex_factors_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_dividends_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_dividends_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_shares_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_shares_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_exchange_rate_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_exchange_rate_mirror_args(
        parser,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_announcement_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_announcement_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_southbound_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_southbound_mirror_args(
        parser,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_instrument_industry_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_instrument_industry_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
        default_industry_source=DEFAULT_HK_INDUSTRY_SOURCE,
        default_industry_level=DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL,
    )


def add_hk_industry_changes_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_industry_changes_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
        default_industry_source=DEFAULT_HK_INDUSTRY_SOURCE,
        default_change_level=DEFAULT_HK_INDUSTRY_CHANGE_LEVEL,
    )


def add_hk_financial_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_financial_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_pit_patch_mirror_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_pit_patch_mirror_args(
        parser,
        default_batch_size=DEFAULT_BATCH_SIZE,
        default_out_root=DEFAULT_OUT_ROOT,
        max_attempts_default=DEFAULT_MIRROR_MAX_ATTEMPTS,
        backoff_seconds_default=DEFAULT_MIRROR_BACKOFF_SECONDS,
        max_backoff_seconds_default=DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    )


def add_hk_pit_fundamentals_build_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_pit_fundamentals_build_args(
        parser,
        default_pipeline_fundamentals_name=DEFAULT_PIPELINE_FUNDAMENTALS_NAME,
    )


def add_hk_industry_labels_build_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_industry_labels_build_args(
        parser,
        default_industry_labels_filename_prefix=DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX,
    )


def add_hk_pit_coverage_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_pit_coverage_args(parser)


def add_hk_asset_health_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_asset_health_args(parser)


def add_hk_current_health_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_current_health_args(parser)


def add_hk_asset_metadata_rebase_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_asset_metadata_rebase_args(parser)


def add_hk_intraday_health_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_intraday_health_args(parser)


def add_hk_intraday_asset_build_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_intraday_asset_build_args(
        parser,
        default_out_root=DEFAULT_OUT_ROOT,
    )


def add_hk_intraday_sync_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_intraday_sync_args(
        parser,
        default_out_root=DEFAULT_OUT_ROOT,
        default_daily_asset_dir=DEFAULT_INTRADAY_DAILY_ASSET_DIR,
        default_asset_alias=DEFAULT_INTRADAY_ASSET_ALIAS,
        default_package_preset=DEFAULT_PACKAGE_PRESET,
        default_package_daily_snapshot=DEFAULT_PACKAGE_DAILY_SNAPSHOT,
        default_package_instruments_file=DEFAULT_PACKAGE_INSTRUMENTS_FILE,
        default_distribution_name=DEFAULT_INTRADAY_DISTRIBUTION_NAME,
    )


def add_hk_daily_clean_layer_args(parser: argparse.ArgumentParser) -> None:
    _args.add_hk_daily_clean_layer_args(parser)


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
