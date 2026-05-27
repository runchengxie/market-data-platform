from __future__ import annotations

import argparse

from .args_shared import (
    _add_hk_symbol_selection_args,
    _add_mirror_output_args,
    _add_resume_args,
    _add_retry_args,
    _add_rqdata_credentials_args,
)


def add_hk_daily_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init and default research universe.",
    )
    parser.add_argument(
        "--start-date", required=True, help="Date range start, for example 20000101."
    )
    parser.add_argument("--end-date", required=True, help="Date range end, for example 20260311.")
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Extra daily field name. Repeatable. The default OHLCV + total_turnover fields are always included.",
    )
    parser.add_argument(
        "--fields-file",
        action="append",
        default=[],
        help="Text file with one extra daily field per line. Repeatable.",
    )
    _add_hk_symbol_selection_args(
        parser,
        symbol_help="HK symbol to mirror, for example 00005.HK. Repeatable.",
        symbols_file_help="Text file with one HK symbol per line. If provided, this takes precedence over config research_universe symbols.",
        by_date_file_help="Universe-by-date CSV. If provided, this takes precedence over config research_universe symbols.",
    )
    parser.add_argument(
        "--adjust-type",
        help="Optional RQData adjust_type passed to get_price, for example none or pre.",
    )
    parser.add_argument(
        "--skip-suspended",
        dest="skip_suspended",
        action="store_true",
        help="Skip suspended data in RQData get_price. Default for HK is enabled.",
    )
    parser.add_argument(
        "--include-suspended",
        dest="skip_suspended",
        action="store_false",
        help="Include suspended rows instead of using the HK default skip behavior.",
    )
    parser.set_defaults(skip_suspended=None)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=default_batch_size,
        help=f"Number of order_book_ids per RQData request. Default: {default_batch_size}.",
    )
    parser.add_argument(
        "--provider-permission-preflight",
        action="store_true",
        help=(
            "Run a one-symbol daily get_price permission check before bulk requests. "
            "Useful for ETF daily mirrors where account-level permission gaps should fail fast."
        ),
    )
    parser.add_argument(
        "--preflight-symbol",
        help=(
            "Optional HK symbol to use for --provider-permission-preflight. "
            "Defaults to the first pending symbol."
        ),
    )
    _add_mirror_output_args(parser, default_out_root=default_out_root)
    _add_resume_args(
        parser,
        resume_help="Resume into an existing snapshot directory. Requires matching fields, symbols, and query settings.",
    )
    _add_retry_args(
        parser,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        attempts_help="Retry attempts per request batch.",
    )


def add_hk_valuation_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        supports_fields=True,
        field_help=(
            "Extra HK valuation factor name. The default archive fields "
            "hk_total_market_val/pe_ratio_ttm/pb_ratio_ttm are always included."
        ),
        fields_file_help="Text file with one extra HK valuation factor name per line. Repeatable.",
    )


def add_hk_dated_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
    supports_fields: bool = False,
    field_help: str | None = None,
    fields_file_help: str | None = None,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init and default research universe.",
    )
    parser.add_argument(
        "--start-date", required=True, help="Date range start, for example 20000101."
    )
    parser.add_argument("--end-date", required=True, help="Date range end, for example 20260317.")
    if supports_fields:
        parser.add_argument(
            "--field",
            action="append",
            default=[],
            help=field_help or "Extra field name. Repeatable.",
        )
        parser.add_argument(
            "--fields-file",
            action="append",
            default=[],
            help=fields_file_help or "Text file with one extra field per line. Repeatable.",
        )
    _add_hk_symbol_selection_args(
        parser,
        symbol_help="HK symbol to mirror, for example 00005.HK. Repeatable.",
        symbols_file_help="Text file with one HK symbol per line. If provided, this takes precedence over config research_universe symbols.",
        by_date_file_help="Universe-by-date CSV. If provided, this takes precedence over config research_universe symbols.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=default_batch_size,
        help=f"Number of order_book_ids per RQData request. Default: {default_batch_size}.",
    )
    _add_mirror_output_args(parser, default_out_root=default_out_root)
    _add_resume_args(
        parser,
        resume_help="Resume into an existing snapshot directory. Requires matching fields, symbols, and query settings.",
    )
    _add_retry_args(
        parser,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        attempts_help="Retry attempts per request batch.",
    )


def add_hk_ex_factors_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
    )


def add_hk_dividends_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
    )


def add_hk_shares_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        supports_fields=True,
        field_help=(
            "Extra shares field name. The documented total/circulation/HK share fields "
            "are included by default. Repeatable."
        ),
        fields_file_help="Text file with one extra shares field per line. Repeatable.",
    )


def add_hk_exchange_rate_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init.",
    )
    parser.add_argument(
        "--start-date", required=True, help="Date range start, for example 20000101."
    )
    parser.add_argument("--end-date", required=True, help="Date range end, for example 20260319.")
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help=(
            "Extra exchange-rate field name. currency_pair + middle_referrence_rate are included "
            "by default. Repeatable."
        ),
    )
    parser.add_argument(
        "--fields-file",
        action="append",
        default=[],
        help="Text file with one extra exchange-rate field per line. Repeatable.",
    )
    _add_mirror_output_args(parser, default_out_root=default_out_root)
    _add_resume_args(
        parser,
        resume_help="Resume into an existing snapshot directory. Requires matching dates and fields.",
        include_skip_existing=False,
    )
    _add_retry_args(
        parser,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        attempts_help="Retry attempts per exchange-rate request.",
    )


def add_hk_announcement_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        supports_fields=True,
        field_help="Announcement field name passed to rqdatac.hk.get_announcement. Repeatable.",
        fields_file_help="Text file with one announcement field name per line. Repeatable.",
    )


def add_hk_southbound_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init and default research universe.",
    )
    parser.add_argument(
        "--start-date", required=True, help="Date range start, for example 20141117."
    )
    parser.add_argument("--end-date", required=True, help="Date range end, for example 20260318.")
    _add_hk_symbol_selection_args(
        parser,
        symbol_help="HK symbol to keep, for example 00005.HK. Repeatable.",
        symbols_file_help="Text file with one HK symbol per line. If provided, this takes precedence over config research_universe symbols.",
        by_date_file_help="Universe-by-date CSV. If provided, both symbols and query dates are resolved from this file.",
    )
    parser.add_argument(
        "--trading-type",
        action="append",
        default=[],
        choices=["sh", "sz", "both"],
        help="Southbound channel to mirror. Repeatable. Default: both.",
    )
    parser.add_argument(
        "--rebalance-frequency",
        default="D",
        help="Snapshot frequency applied to resolved trading dates. Default: D. Use M/Q to sample fewer dates.",
    )
    _add_mirror_output_args(parser, default_out_root=default_out_root)
    _add_resume_args(
        parser,
        resume_help="Resume into an existing snapshot directory. Requires matching symbols, dates, and trading types.",
    )
    _add_retry_args(
        parser,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        attempts_help="Retry attempts per southbound request.",
    )


def add_hk_instrument_industry_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
    default_industry_source: str,
    default_industry_level: int,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
    )
    parser.add_argument(
        "--source",
        default=default_industry_source,
        help=f"Industry taxonomy source passed to rqdatac.get_instrument_industry. Default: {default_industry_source}.",
    )
    parser.add_argument(
        "--level",
        default=str(default_industry_level),
        choices=["0", "1", "2", "3"],
        help="Industry hierarchy depth. 0 keeps first/second/third industry columns. Default: 0.",
    )
    parser.add_argument(
        "--rebalance-frequency",
        default="M",
        help="Snapshot frequency applied to resolved dates. Default: M. Use D to keep every date.",
    )


def add_hk_industry_changes_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
    default_industry_source: str,
    default_change_level: int,
) -> None:
    add_hk_dated_mirror_args(
        parser,
        default_batch_size=default_batch_size,
        default_out_root=default_out_root,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
    )
    parser.add_argument(
        "--source",
        default=default_industry_source,
        help=f"Industry taxonomy source passed to rqdatac.get_industry_change. Default: {default_industry_source}.",
    )
    parser.add_argument(
        "--level",
        default=str(default_change_level),
        choices=["1", "2", "3"],
        help="Industry hierarchy level used to enumerate mapping codes. Default: 1.",
    )
    parser.add_argument(
        "--mapping-date",
        help="Optional mapping as-of date used for get_industry_mapping. Default: --end-date.",
    )


def add_hk_financial_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
    supports_quarter_chunk: bool = False,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init and default research universe.",
    )
    parser.add_argument(
        "--start-quarter", required=True, help="Quarter range start, for example 2011q1."
    )
    parser.add_argument(
        "--end-quarter", required=True, help="Quarter range end, for example 2025q4."
    )
    parser.add_argument(
        "--date",
        help="Optional PIT as-of date. Use an absolute date such as 20260310 for reproducible mirrors.",
    )
    parser.add_argument(
        "--statements",
        default="latest",
        choices=["latest", "all"],
        help="Return latest or all statements for each quarter. Default: latest.",
    )
    parser.add_argument(
        "--field-profile",
        action="append",
        choices=["starter", "full"],
        default=[],
        help="Bundled HK financial field set. starter=repo baseline, full=all fields exposed by local rqdatac metadata.",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Financial field name. Repeatable.",
    )
    parser.add_argument(
        "--fields-file",
        action="append",
        default=[],
        help="Text file with one financial field per line. Repeatable.",
    )
    _add_hk_symbol_selection_args(
        parser,
        symbol_help="HK symbol to mirror, for example 00005.HK. Repeatable.",
        symbols_file_help="Text file with one HK symbol per line. If provided, this takes precedence over config research_universe symbols.",
        by_date_file_help="Universe-by-date CSV. If provided, this takes precedence over config research_universe symbols.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=default_batch_size,
        help=f"Number of order_book_ids per RQData request. Default: {default_batch_size}.",
    )
    if supports_quarter_chunk:
        parser.add_argument(
            "--quarter-chunk-size",
            type=int,
            help=(
                "Optional number of quarters per PIT provider request partition. "
                "Useful for strict full PIT snapshots because it keeps each request window small "
                "and resumable. Default: disabled."
            ),
        )
    _add_mirror_output_args(parser, default_out_root=default_out_root)
    _add_resume_args(
        parser,
        resume_help="Resume into an existing snapshot directory. Requires matching fields, symbols, and query settings.",
    )
    _add_retry_args(
        parser,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        attempts_help="Retry attempts per request batch.",
    )


def add_hk_pit_patch_mirror_args(
    parser: argparse.ArgumentParser,
    *,
    default_batch_size: int,
    default_out_root: str,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init.",
    )
    parser.add_argument(
        "--base-asset-dir",
        required=True,
        help="Existing mirror-hk-pit-financials output directory used as the full base snapshot.",
    )
    parser.add_argument(
        "--target-date",
        required=True,
        help="Target PIT as-of date for the patch, for example 20260430.",
    )
    parser.add_argument(
        "--patch-start-quarter",
        required=True,
        help="First quarter to refresh from RQData, for example 2024q4.",
    )
    parser.add_argument(
        "--patch-end-quarter",
        required=True,
        help="Last quarter to refresh from RQData, for example 2025q4.",
    )
    parser.add_argument(
        "--statements",
        default="latest",
        choices=["latest", "all"],
        help="Return latest or all statements for patched quarters. Default: latest.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Optional HK symbol subset to patch, for example 00005.HK. Repeatable. Default: all base symbols.",
    )
    parser.add_argument(
        "--symbols-file",
        help="Optional text file with one HK symbol per line. Default: use base symbols.txt.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional cap on the selected symbol count after dedupe.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=default_batch_size,
        help=f"Number of order_book_ids per RQData patch request. Default: {default_batch_size}.",
    )
    _add_mirror_output_args(parser, default_out_root=default_out_root)
    _add_resume_args(
        parser,
        resume_help=(
            "Resume into an existing PIT patch snapshot directory. Existing data/*.parquet files "
            "are treated as completed symbols."
        ),
    )
    _add_retry_args(
        parser,
        max_attempts_default=max_attempts_default,
        backoff_seconds_default=backoff_seconds_default,
        max_backoff_seconds_default=max_backoff_seconds_default,
        attempts_help="Retry attempts per PIT patch request batch.",
    )
