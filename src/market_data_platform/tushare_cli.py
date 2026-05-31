from __future__ import annotations

import argparse

from market_data_platform.tushare_backfill import BACKFILL_DATASETS, BACKFILL_SEGMENTS


def add_token_env_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token-env",
        default="TUSHARE_TOKEN",
        help="Environment variable containing the TuShare token (default: TUSHARE_TOKEN).",
    )


def add_tushare_date_mirror_parser(
    subparsers: argparse._SubParsersAction,
    *,
    command: str,
    description: str,
) -> None:
    parser = subparsers.add_parser(command, help=description)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--fields", nargs="+")
    parser.add_argument("--skip-existing", action="store_true")
    add_token_env_argument(parser)


def add_tushare_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tushare", help="TuShare A-share mirror/export helpers.")
    tushare_subparsers = parser.add_subparsers(dest="tushare_command", required=True)

    verify = tushare_subparsers.add_parser(
        "verify-token",
        help="Verify one or more tokens without printing tokens or account quota data.",
    )
    verify.add_argument(
        "--env",
        dest="env_keys",
        action="append",
        help="Token environment variable; repeat for multiple tokens.",
    )

    instruments = tushare_subparsers.add_parser(
        "export-a-share-instruments",
        help="Export A-share instrument master from stock_basic.",
    )
    instruments.add_argument("--out", required=True)
    instruments.add_argument("--symbols-out")
    instruments.add_argument("--list-status", dest="list_statuses", nargs="+")
    instruments.add_argument("--fields", nargs="+")
    add_token_env_argument(instruments)

    trade_cal = tushare_subparsers.add_parser(
        "mirror-a-share-trade-cal",
        help="Mirror the A-share trading calendar from trade_cal.",
    )
    trade_cal.add_argument("--out", required=True)
    trade_cal.add_argument("--start-date", required=True)
    trade_cal.add_argument("--end-date", required=True)
    trade_cal.add_argument("--exchange", default="")
    add_token_env_argument(trade_cal)

    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-a-share-daily",
        description="Mirror unadjusted A-share daily bars, partitioned by trade date.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-a-share-adj-factor",
        description="Mirror A-share adjustment factors, partitioned by trade date.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-a-share-daily-basic",
        description="Mirror A-share daily valuation metrics, partitioned by trade date.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-a-share-limit-status",
        description="Mirror A-share daily limit prices into the limit_status asset.",
    )

    backfill = tushare_subparsers.add_parser(
        "backfill-a-share-history",
        help="Plan or run segmented TuShare A 股 raw history backfill.",
    )
    backfill.add_argument("--artifacts-root")
    backfill.add_argument("--start-date", required=True)
    backfill.add_argument("--end-date", required=True)
    backfill.add_argument(
        "--dataset",
        dest="datasets",
        action="append",
        choices=BACKFILL_DATASETS,
        help=(
            "Dataset to backfill; repeat for multiple datasets. "
            "Defaults to all raw daily datasets."
        ),
    )
    backfill.add_argument(
        "--segment",
        default="month",
        choices=BACKFILL_SEGMENTS,
        help="Backfill request segment size (default: month).",
    )
    backfill.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Refetch partitions even when trade_date parquet files already exist.",
    )
    backfill.add_argument(
        "--sync-latest",
        action="store_true",
        help="Point canonical latest aliases at completed backfill snapshots.",
    )
    backfill.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue remaining segments/datasets after a provider or write error.",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the backfill plan without provider calls or writes.",
    )
    add_token_env_argument(backfill)

    clean = tushare_subparsers.add_parser(
        "build-a-share-daily-clean",
        help="Build the TuShare A 股 daily_clean asset from raw daily and optional overlays.",
    )
    clean.add_argument("--daily-dir", required=True)
    clean.add_argument("--out-dir", required=True)
    clean.add_argument("--adj-factor-dir")
    clean.add_argument("--daily-basic-dir")
    clean.add_argument("--limit-status-dir")
    clean.add_argument("--suspend-dir")
    clean.add_argument("--instruments-file")
    clean.add_argument("--min-rows", type=int, default=1)
    clean.add_argument("--min-symbols", type=int, default=1)

    validate = tushare_subparsers.add_parser(
        "validate-a-share-daily-clean",
        help="Run quality gates for a TuShare A 股 daily_clean asset.",
    )
    validate.add_argument("--daily-clean-dir", required=True)
    validate.add_argument("--min-rows", type=int, default=1)
    validate.add_argument("--min-symbols", type=int, default=1)
    validate.add_argument("--require-valuation", action="store_true")
    validate.add_argument("--require-limit-status", action="store_true")
