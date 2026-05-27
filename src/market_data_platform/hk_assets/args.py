from __future__ import annotations

import argparse

from .args_mirror import (
    add_hk_announcement_mirror_args,
    add_hk_daily_mirror_args,
    add_hk_dated_mirror_args,
    add_hk_dividends_mirror_args,
    add_hk_ex_factors_mirror_args,
    add_hk_exchange_rate_mirror_args,
    add_hk_financial_mirror_args,
    add_hk_industry_changes_mirror_args,
    add_hk_instrument_industry_mirror_args,
    add_hk_pit_patch_mirror_args,
    add_hk_shares_mirror_args,
    add_hk_southbound_mirror_args,
    add_hk_valuation_mirror_args,
)
from .args_shared import (
    _add_hk_symbol_selection_args,
    _add_quality_gate_arg,
    _add_rqdata_credentials_args,
)


def add_list_hk_financial_fields_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--contains",
        action="append",
        default=[],
        help="Keep only field names containing this token. Repeatable.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional cap on the number of printed field names.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path. Default: print to stdout.",
    )


def add_hk_instruments_export_args(
    parser: argparse.ArgumentParser,
    *,
    default_out_root: str,
    default_instruments_filename_prefix: str,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init.",
    )
    parser.add_argument(
        "--use-config-universe",
        action="store_true",
        help="Filter to the universe resolved from --config instead of exporting the full HK instrument list.",
    )
    parser.add_argument(
        "--instrument-type",
        default="CS",
        help=(
            "RQData instrument_type passed to all_instruments, for example CS or ETF. Default: CS."
        ),
    )
    _add_hk_symbol_selection_args(
        parser,
        symbol_help="HK symbol to keep, for example 00005.HK. Repeatable.",
        symbols_file_help="Text file with one HK symbol per line.",
        by_date_file_help="Universe-by-date CSV used to derive the HK symbol set.",
    )
    parser.add_argument(
        "--out",
        help=(
            "Output file path. Default: "
            + default_out_root
            + "/hk/instruments/"
            + default_instruments_filename_prefix
            + "_<timestamp>.parquet"
        ),
    )
    parser.add_argument(
        "--symbols-out",
        help="Optional text output with one exported HK symbol per line.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )


def add_hk_pit_fundamentals_build_args(
    parser: argparse.ArgumentParser,
    *,
    default_pipeline_fundamentals_name: str,
) -> None:
    parser.add_argument(
        "--asset-dir",
        required=True,
        help="Path to a mirror-hk-pit-financials output directory.",
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
        help="Value field to keep in the output fundamentals file. Repeatable. Default: use asset manifest fields.",
    )
    parser.add_argument(
        "--fields-file",
        action="append",
        default=[],
        help="Text file with one financial field per line. Repeatable.",
    )
    parser.add_argument(
        "--out",
        help=(
            "Output file path. Default: <asset-dir>/"
            + default_pipeline_fundamentals_name
            + ". Use .csv to write CSV, otherwise Parquet."
        ),
    )
    parser.add_argument(
        "--source-universe-by-date",
        help="Optional source universe-by-date CSV. Use with --universe-by-date-out to derive a research-ready PIT universe.",
    )
    parser.add_argument(
        "--universe-by-date-out",
        help="Optional filtered universe-by-date CSV output. Requires --source-universe-by-date.",
    )
    parser.add_argument(
        "--max-latest-report-age-days",
        type=int,
        help=(
            "Optional max age, in calendar days, between each universe trade_date and the latest "
            "available PIT row for that symbol. Only applies when deriving --universe-by-date-out."
        ),
    )
    parser.add_argument(
        "--feature-age-config",
        help=(
            "Optional pipeline config whose PIT-backed features should be used for config-aware "
            "universe filtering. Use with --max-selected-feature-age-days."
        ),
    )
    parser.add_argument(
        "--max-selected-feature-age-days",
        type=int,
        help=(
            "Optional max age, in calendar days, between each universe trade_date and the latest "
            "non-null value of every selected PIT-backed feature resolved from --feature-age-config. "
            "Only applies when deriving --universe-by-date-out."
        ),
    )
    parser.add_argument(
        "--symbols-out",
        help=(
            "Optional text file output with one canonical symbol per line for names present "
            "in the derived fundamentals file. Legacy ts_code inputs remain compatible."
        ),
    )
    parser.add_argument(
        "--keep-meta",
        action="store_true",
        help="Keep PIT metadata columns such as quarter, info_date, fiscal_year and rice_create_tm.",
    )
    parser.add_argument(
        "--duplicate-policy",
        choices=["keep-last", "error"],
        default="keep-last",
        help="How to handle duplicate trade_date + symbol rows after mapping trade_date=info_date.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )


def add_hk_industry_labels_build_args(
    parser: argparse.ArgumentParser,
    *,
    default_industry_labels_filename_prefix: str,
) -> None:
    parser.add_argument(
        "--asset-dir",
        required=True,
        help="Path to a mirror-hk-industry-changes output directory.",
    )
    parser.add_argument(
        "--source-universe-by-date",
        help="Optional universe-by-date CSV used as the exact date + symbol grid. Best for M/Q label files.",
    )
    parser.add_argument(
        "--daily-asset-dir",
        help="Optional local daily asset snapshot used to derive a daily trade_date + symbol grid.",
    )
    parser.add_argument(
        "--start-date",
        help="Optional lower date bound in YYYYMMDD. Applies to the selected source grid.",
    )
    parser.add_argument(
        "--end-date",
        help="Optional upper date bound in YYYYMMDD. Applies to the selected source grid.",
    )
    parser.add_argument(
        "--frequency",
        default="D",
        choices=["D", "M", "Q"],
        help="Output sampling frequency over the source grid. D keeps all dates, M/Q keep each symbol's last trade date per period. Default: D.",
    )
    parser.add_argument(
        "--out",
        help=(
            "Output file path. Default: <asset-dir>/"
            + default_industry_labels_filename_prefix
            + "_<freq>.parquet. Use .csv to write CSV, otherwise Parquet."
        ),
    )
    parser.add_argument(
        "--symbols-out",
        help=(
            "Optional text file output with one canonical symbol per line for names present "
            "in the derived label file. Legacy ts_code inputs remain compatible."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )


def add_hk_pit_coverage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help=(
            "Optional pipeline config path or alias. "
            "When provided, the command defaults to config fundamentals.file "
            "and uses fundamentals.features as the inspection feature set."
        ),
    )
    parser.add_argument(
        "--asset-dir",
        help="Optional PIT asset directory. Defaults to the parent of pipeline_fundamentals.parquet when possible.",
    )
    parser.add_argument(
        "--fundamentals-file",
        help="Optional pipeline fundamentals file path. Defaults to <asset-dir>/pipeline_fundamentals.parquet.",
    )
    parser.add_argument(
        "--field-profile",
        action="append",
        choices=["starter", "full"],
        default=[],
        help="Optional bundled field set. Useful when you want to inspect raw PIT columns instead of config features.",
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Feature or raw PIT field to inspect. Repeatable.",
    )
    parser.add_argument(
        "--fields-file",
        action="append",
        default=[],
        help="Text file with one feature or raw PIT field per line. Repeatable.",
    )
    parser.add_argument(
        "--mode",
        default="strict",
        choices=["strict", "trainable", "both"],
        help=(
            "Coverage mode. strict keeps the current source-level complete-case check. "
            "trainable estimates PIT trainability after quarterly ffill + features.missing. "
            "both includes the trainable estimate alongside strict coverage. Default: strict."
        ),
    )
    parser.add_argument(
        "--include-health",
        action="store_true",
        help=(
            "Also inspect target-date PIT freshness / staleness. "
            "Useful to tell whether sparse PIT rows can still be safely ffilled to a rebalance date."
        ),
    )
    parser.add_argument(
        "--target-date",
        help=(
            "Optional target date in YYYYMMDD for PIT health. "
            "Default: latest date from --by-date-file or config research_universe.by_date_file when available, "
            "else max trade_date in pipeline_fundamentals.parquet."
        ),
    )
    parser.add_argument(
        "--symbols-file",
        help=(
            "Optional text file with one HK symbol per line. "
            "Limits PIT health inspection to those symbols."
        ),
    )
    parser.add_argument(
        "--by-date-file",
        help=(
            "Optional universe-by-date CSV for PIT health. "
            "When omitted, defaults to config research_universe.by_date_file if available."
        ),
    )
    parser.add_argument(
        "--health-sample-limit",
        type=int,
        default=5,
        help="Number of sample stale or missing symbols shown in PIT health output. Default: 5.",
    )
    parser.add_argument(
        "--min-symbols",
        type=int,
        help="Quarter viability threshold. Defaults to universe.min_symbols_per_date from --config, else 5.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Number of worst-coverage features shown in text output. Default: 10.",
    )
    parser.add_argument(
        "--quarter-limit",
        type=int,
        default=12,
        help="Number of recent quarters shown in text output. Default: 12.",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path. Default: print to stdout.",
    )
    _add_quality_gate_arg(parser)


def add_hk_asset_health_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--asset-dir",
        required=True,
        help="Path to a local HK asset snapshot directory containing data/.",
    )
    parser.add_argument(
        "--symbols-file",
        help="Optional text file with one HK symbol per line. Limits inspection to those symbols.",
    )
    parser.add_argument(
        "--by-date-file",
        help=(
            "Optional universe-by-date CSV. Limits inspection to symbols selected on --target-date "
            "(or the resolved target date when omitted)."
        ),
    )
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help=(
            "Value column to audit on the target date. Repeatable. "
            "Default: dataset-aware fields such as daily OHLCV or valuation ratios."
        ),
    )
    parser.add_argument(
        "--date-column",
        help="Override the date column name. Default: auto-detect trade_date/date/info_date.",
    )
    parser.add_argument(
        "--target-date",
        help=(
            "Optional target date in YYYYMMDD. Default: latest date from audit.csv when available, "
            "else manifest query date, else parquet scan max date."
        ),
    )
    parser.add_argument(
        "--daily-asset-dir",
        help=(
            "Optional HK daily asset snapshot used to de-noise valuation stale-run history. "
            "When provided together with --include-history on valuation assets, stale runs are only "
            "flagged when the corresponding daily close changed during the run."
        ),
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="Number of sample stale or missing symbols shown. Default: 5.",
    )
    parser.add_argument(
        "--top-latest-dates",
        type=int,
        default=5,
        help="Number of latest-date buckets shown in the summary. Default: 5.",
    )
    parser.add_argument(
        "--include-history",
        action="store_true",
        help=(
            "Also scan the full available history for row-level anomalies such as impossible "
            "daily price bounds, negative volume, or negative turnover."
        ),
    )
    parser.add_argument(
        "--history-sample-limit",
        type=int,
        default=5,
        help="Number of sample historical issue rows shown when --include-history is enabled. Default: 5.",
    )
    parser.add_argument(
        "--history-start-date",
        help=(
            "Optional lower date bound for --include-history scans in YYYYMMDD. "
            "Useful for incremental/tail health checks on large assets."
        ),
    )
    parser.add_argument(
        "--history-end-date",
        help="Optional upper date bound for --include-history scans in YYYYMMDD.",
    )
    parser.add_argument(
        "--history-tail-days",
        type=int,
        help=(
            "Limit --include-history to the trailing N calendar days ending at --target-date. "
            "Ignored when --history-start-date is also provided."
        ),
    )
    parser.add_argument(
        "--history-timeout-seconds",
        type=float,
        help=(
            "Stop adding full-history work after this many seconds and mark the report as truncated; "
            "target-date health checks still complete."
        ),
    )
    parser.add_argument(
        "--history-max-symbols",
        type=int,
        help=(
            "Maximum number of symbols to include in --include-history scans. "
            "Target-date health checks still scan all selected symbols."
        ),
    )
    parser.add_argument(
        "--history-progress-every-symbols",
        type=int,
        default=0,
        help="Print history scan progress to stderr every N symbols. Default: disabled.",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path. Default: print to stdout.",
    )
    _add_quality_gate_arg(parser)


def add_hk_current_health_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        default="artifacts",
        help=(
            "Artifacts root used to resolve the default current contract and fallback alias paths. "
            "Default: artifacts."
        ),
    )
    parser.add_argument(
        "--current-contract",
        help=(
            "Optional HK current contract path. Default: "
            "<artifacts-root>/metadata/current_assets/hk_current.json."
        ),
    )
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        help=(
            "Optional HK current asset key to inspect. Repeatable. "
            "Default: inspect all known hk_current assets."
        ),
    )
    parser.add_argument(
        "--target-date",
        help=(
            "Optional target date in YYYYMMDD. Default: current_contract.target_date when available, "
            "else max asset as_of."
        ),
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path. Default: print to stdout.",
    )
    _add_quality_gate_arg(parser)


def add_hk_asset_metadata_rebase_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        default="artifacts",
        help="Artifacts root containing live HK asset metadata. Default: artifacts.",
    )
    parser.add_argument(
        "--from-prefix",
        required=True,
        help="Old absolute repository prefix embedded in manifests/contracts.",
    )
    parser.add_argument(
        "--to-prefix",
        help="New absolute repository prefix. Default: parent directory of --artifacts-root.",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=5_000_000,
        help="Skip text metadata files larger than this limit. Default: 5000000.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write metadata replacements and rebuild hk_current/registry. Default: dry-run.",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
        help="Output format. Default: text.",
    )
    parser.add_argument("--out", help="Optional report path. Default: print to stdout.")


def add_hk_data_asset_audit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        default="artifacts",
        help="Artifacts root used to resolve current contracts, reports, and local snapshots. Default: artifacts.",
    )
    parser.add_argument(
        "--current-contract",
        help="Optional HK current contract path. Default: <artifacts-root>/metadata/current_assets/hk_current.json.",
    )
    parser.add_argument(
        "--reports-dir",
        help="Directory containing health/workflow JSON reports. Default: <artifacts-root>/reports.",
    )
    parser.add_argument(
        "--target-date",
        help="Target date in YYYYMMDD. Default: hk_current target_date when available, else today.",
    )
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        help="Optional hk_current asset key to include from the current contract. Repeatable. Default: all known.",
    )
    parser.add_argument(
        "--scan-family",
        action="append",
        default=[],
        help="Optional HK asset family directory to scan under assets/rqdata/hk. Repeatable. Default: all known families.",
    )
    parser.add_argument(
        "--metadata-only-etf-daily",
        action="store_true",
        help="Use manifest metadata for ETF daily verification instead of scanning ETF parquet files.",
    )
    parser.add_argument(
        "--intraday-mode",
        choices=["metadata", "scan", "health"],
        default="metadata",
        help=(
            "Intraday freshness evidence mode. metadata reads manifest/as_of only; scan reads parquet dates; "
            "health runs inspect-hk-intraday-health. Default: metadata."
        ),
    )
    parser.add_argument(
        "--health-report",
        action="append",
        default=[],
        help="Additional health/workflow JSON report to aggregate. Repeatable.",
    )
    parser.add_argument(
        "--run-refresh",
        action="store_true",
        help="Run the existing HK asset workflow refresh+inspect phases before final freshness verdicts.",
    )
    parser.add_argument(
        "--refresh-mode",
        choices=["full", "patch"],
        default="patch",
        help="Refresh mode forwarded to run_hk_asset_workflow.py when --run-refresh is set. Default: patch.",
    )
    parser.add_argument(
        "--refresh-asset",
        action="append",
        default=[],
        help="Asset forwarded as --refresh-asset/--inspect-asset when --run-refresh is set. Repeatable.",
    )
    parser.add_argument(
        "--refresh-dry-run",
        action="store_true",
        help="Forward --dry-run to the refresh workflow when --run-refresh is set.",
    )
    parser.add_argument("--config", help="Optional config path or alias forwarded to refresh workflow.")
    parser.add_argument(
        "--execute-repair",
        action="store_true",
        help="Execute approved automatic repair commands. Default: report candidates only.",
    )
    parser.add_argument(
        "--approved-repair-action",
        action="append",
        choices=["repoint", "patch-refresh", "targeted-rebuild", "manual-review", "provider-boundary"],
        default=[],
        help="Repair action class approved for --execute-repair. Repeatable.",
    )
    parser.add_argument(
        "--delete-prune-candidates",
        action="store_true",
        help="Delete explicitly approved prune candidate paths. Default: dry-run only.",
    )
    parser.add_argument(
        "--approved-prune-path",
        action="append",
        default=[],
        help="Exact prune candidate path approved for deletion. Repeatable and only used with --delete-prune-candidates.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="Number of sample rows/symbols retained in audit sections. Default: 5.",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path. Default: print to stdout.",
    )
    _add_quality_gate_arg(parser)


def add_hk_intraday_health_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help=(
            "Intraday parquet path, .parts directory, cache directory, or formal intraday asset directory. "
            "Repeatable. When a matching .parts directory exists, the command scans its part files automatically."
        ),
    )
    parser.add_argument(
        "--daily-asset-dir",
        help=(
            "Optional HK daily asset snapshot for 5m-vs-daily reconciliation. "
            "When provided, the command compares intraday aggregated OHLCV/amount against daily parquet rows."
        ),
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="Number of sample rows or symbol-days shown per issue. Default: 5.",
    )
    parser.add_argument(
        "--expected-bars-per-day",
        type=int,
        default=66,
        help="Expected HK 5m bars per full session. Default: 66.",
    )
    parser.add_argument(
        "--numeric-rtol",
        type=float,
        default=1e-6,
        help="Relative tolerance used for daily reconciliation. Default: 1e-6.",
    )
    parser.add_argument(
        "--numeric-atol",
        type=float,
        default=1e-8,
        help="Absolute tolerance used for daily reconciliation. Default: 1e-8.",
    )
    parser.add_argument(
        "--intraday-adjust-type",
        choices=["none", "pre", "post", "pre_volume", "post_volume"],
        help=(
            "Adjustment basis for the intraday input. When it differs from the daily asset basis, "
            "OHLC reconciliation mismatches are reported as adjustment-basis info instead of warning."
        ),
    )
    parser.add_argument(
        "--daily-adjust-type",
        choices=["none", "pre", "post", "pre_volume", "post_volume"],
        help=(
            "Adjustment basis for the daily reference asset. Default: infer from manifest query.adjust_type when present."
        ),
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json"],
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path. Default: print to stdout.",
    )
    _add_quality_gate_arg(parser)


def add_hk_intraday_asset_build_args(
    parser: argparse.ArgumentParser,
    *,
    default_out_root: str,
) -> None:
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help=(
            "Local intraday parquet path, .parts directory, cache directory, or intraday asset directory. "
            "Repeatable. All matching parquet/meta/parts files are copied into a formal asset snapshot."
        ),
    )
    parser.add_argument(
        "--out-root",
        default=default_out_root,
        help=f"Asset root directory. Default: {default_out_root}",
    )
    parser.add_argument(
        "--name",
        help="Optional snapshot folder name. Default: intraday_<start>_<end>_<timestamp>.",
    )
    parser.add_argument(
        "--alias",
        help="Optional alias/symlink path to repoint at the new intraday asset snapshot after build.",
    )


def add_hk_intraday_sync_args(
    parser: argparse.ArgumentParser,
    *,
    default_out_root: str,
    default_daily_asset_dir: str,
    default_asset_alias: str,
    default_package_preset: str,
    default_package_daily_snapshot: str,
    default_package_instruments_file: str,
    default_distribution_name: str,
) -> None:
    _add_rqdata_credentials_args(
        parser,
        config_help="Optional config path or alias for rqdata.init.",
    )
    parser.add_argument(
        "--symbols-file", required=True, help="TXT/CSV/Parquet file containing HK symbols."
    )
    parser.add_argument("--start-date", required=True, help="Start date, e.g. 20260402.")
    parser.add_argument("--end-date", required=True, help="End date, e.g. 20260409.")
    parser.add_argument("--frequency", default="5m", help="Intraday frequency. Default: 5m.")
    parser.add_argument(
        "--adjust-type",
        default="pre",
        choices=["none", "pre", "post", "pre_volume", "post_volume"],
        help="RQData adjust_type for intraday bars. Default: pre.",
    )
    parser.add_argument(
        "--fields",
        nargs="+",
        default=["open", "high", "low", "close", "volume", "total_turnover"],
        help="RQData fields. Default: open high low close volume total_turnover.",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Symbols per get_price call.")
    parser.add_argument(
        "--output",
        help=(
            "Optional intraday cache parquet path. "
            "Defaults to artifacts/cache/intraday/hk_intraday_<frequency>_<start>_<end>.parquet."
        ),
    )
    parser.add_argument(
        "--meta-output",
        help="Optional metadata JSON path. Defaults to <output>.meta.json beside the parquet.",
    )
    parser.add_argument(
        "--parts-dir",
        help="Optional batch checkpoint directory. Defaults to <output_stem>.parts beside the output parquet.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip batch files that already exist under --parts-dir and only download missing batches.",
    )
    parser.add_argument(
        "--skip-inspect",
        action="store_true",
        help="Skip intraday health inspection before publishing the new asset alias.",
    )
    parser.add_argument(
        "--daily-asset-dir",
        default=default_daily_asset_dir,
        help=f"HK daily asset snapshot used for 5m-vs-daily reconciliation. Default: {default_daily_asset_dir}",
    )
    parser.add_argument(
        "--health-out",
        help="Optional health report JSON path. Defaults to artifacts/reports/<output_stem>_health.json.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="Number of sample rows or symbol-days shown per issue. Default: 5.",
    )
    parser.add_argument(
        "--expected-bars-per-day",
        type=int,
        default=66,
        help="Expected HK 5m bars per full session. Default: 66.",
    )
    parser.add_argument(
        "--numeric-rtol",
        type=float,
        default=1e-6,
        help="Relative tolerance used for daily reconciliation. Default: 1e-6.",
    )
    parser.add_argument(
        "--numeric-atol",
        type=float,
        default=1e-8,
        help="Absolute tolerance used for daily reconciliation. Default: 1e-8.",
    )
    parser.add_argument(
        "--daily-adjust-type",
        choices=["none", "pre", "post", "pre_volume", "post_volume"],
        help=(
            "Adjustment basis for the daily reference asset. Default: infer from manifest query.adjust_type when present."
        ),
    )
    parser.add_argument(
        "--inspect-fail-on-severity",
        default="error",
        choices=["none", "info", "warning", "error"],
        help=(
            "Quality gate threshold for the default patch health inspection step. "
            "When triggered, the command stops before repointing the intraday latest alias. "
            "Default: error."
        ),
    )
    parser.add_argument(
        "--verify-full-asset",
        action="store_true",
        help=(
            "After the new asset alias is repointed, also scan the full formal intraday asset. "
            "This is much heavier than the default patch-only inspection."
        ),
    )
    parser.add_argument(
        "--verify-sampled-segments",
        type=int,
        default=0,
        help=(
            "After publishing the formal asset, inspect N evenly distributed stored input segments; "
            "N=1 inspects the latest segment only. Default: 0 (disabled)."
        ),
    )
    parser.add_argument(
        "--sampled-health-out",
        help=(
            "Optional JSON path for the post-publish sampled-segment health report. "
            "Only used with --verify-sampled-segments."
        ),
    )
    parser.add_argument(
        "--sampled-inspect-fail-on-severity",
        default="warning",
        choices=["none", "info", "warning", "error"],
        help=(
            "Quality gate threshold for the optional sampled-segment inspection step. "
            "Only used with --verify-sampled-segments. Default: warning."
        ),
    )
    parser.add_argument(
        "--full-health-out",
        help=(
            "Optional JSON path for the explicit full-asset inspection report. "
            "Only used with --verify-full-asset."
        ),
    )
    parser.add_argument(
        "--full-inspect-fail-on-severity",
        default="warning",
        choices=["none", "info", "warning", "error"],
        help=(
            "Quality gate threshold for the optional full-asset inspection step. "
            "Only used with --verify-full-asset. Default: warning."
        ),
    )
    parser.add_argument(
        "--out-root",
        default=default_out_root,
        help=f"Formal intraday asset root directory. Default: {default_out_root}",
    )
    parser.add_argument(
        "--asset-name",
        help="Optional snapshot folder name for the formal intraday asset.",
    )
    parser.add_argument(
        "--asset-alias",
        default=default_asset_alias,
        help=f"Alias/symlink path to repoint at the new intraday asset snapshot. Default: {default_asset_alias}",
    )
    parser.add_argument(
        "--package",
        action="store_true",
        help="Stage the refreshed intraday asset into release tarballs under artifacts/releases/.",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Create or update a GitHub Release for the refreshed intraday asset. Implies --package.",
    )
    parser.add_argument(
        "--preset",
        default=default_package_preset,
        help=f"Preset forwarded to package_assets when --package/--release is used. Default: {default_package_preset}",
    )
    parser.add_argument(
        "--distribution-name",
        default=default_distribution_name,
        help="Distribution name used in staged manifests, tarballs, and release notes.",
    )
    parser.add_argument(
        "--package-dest", help="Optional staged package root for the intraday release part."
    )
    parser.add_argument("--tar-dir", help="Optional tarball output directory.")
    parser.add_argument(
        "--package-daily-snapshot",
        default=default_package_daily_snapshot,
        help=(
            f"Daily snapshot forwarded to package_assets. Default: {default_package_daily_snapshot}"
        ),
    )
    parser.add_argument(
        "--package-instruments-file",
        default=default_package_instruments_file,
        help=(
            "Instruments file forwarded to package_assets. "
            f"Default: {default_package_instruments_file}"
        ),
    )
    parser.add_argument(
        "--repo", help="Target GitHub repo in owner/name format when --release is used."
    )
    parser.add_argument("--tag", help="Optional GitHub release tag override.")
    parser.add_argument("--title", help="Optional GitHub release title override.")
    parser.add_argument("--draft", action="store_true", help="Create the GitHub release as draft.")
    parser.add_argument(
        "--prerelease", action="store_true", help="Mark the GitHub release as prerelease."
    )
    parser.add_argument("--latest", action="store_true", help="Mark the GitHub release as latest.")
    parser.add_argument(
        "--clobber", action="store_true", help="Overwrite existing release assets if needed."
    )


def add_hk_daily_clean_layer_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--asset-dir",
        required=True,
        help="Path to a local HK daily asset snapshot directory containing data/.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Destination directory for the cleaned daily snapshot.",
    )
    parser.add_argument(
        "--alias",
        help="Optional alias/symlink path to point at the cleaned snapshot after it is built.",
    )
    parser.add_argument(
        "--symbols-file",
        help="Optional text file with one HK symbol per line. Defaults to source symbols.txt when present.",
    )
    parser.add_argument(
        "--instruments-file",
        help=(
            "Optional HK instruments parquet. "
            "When the snapshot is ETF-oriented, this enables ETF second-pass rules and product-profile reporting."
        ),
    )
    parser.add_argument(
        "--zero-price-min-run",
        type=int,
        default=5,
        help="Minimum consecutive all-zero OHLC run length to null out. Default: 5.",
    )
    parser.add_argument(
        "--etf-short-zero-max-run",
        type=int,
        default=4,
        help=(
            "When ETF product metadata is available, null out vanilla ETF all-zero OHLC runs up to this length. "
            "Default: 4."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite --out-dir if it already exists.",
    )
