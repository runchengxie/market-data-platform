from __future__ import annotations

import argparse


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
    parser = subparsers.add_parser("tushare", help="TuShare CN mirror/export helpers.")
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
        "export-cn-instruments",
        help="Export A-share instrument master from stock_basic.",
    )
    instruments.add_argument("--out", required=True)
    instruments.add_argument("--symbols-out")
    instruments.add_argument("--list-status", dest="list_statuses", nargs="+")
    instruments.add_argument("--fields", nargs="+")
    add_token_env_argument(instruments)

    trade_cal = tushare_subparsers.add_parser(
        "mirror-cn-trade-cal",
        help="Mirror the CN trading calendar from trade_cal.",
    )
    trade_cal.add_argument("--out", required=True)
    trade_cal.add_argument("--start-date", required=True)
    trade_cal.add_argument("--end-date", required=True)
    trade_cal.add_argument("--exchange", default="")
    add_token_env_argument(trade_cal)

    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-daily",
        description="Mirror unadjusted A-share daily bars, partitioned by trade date.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-adj-factor",
        description="Mirror A-share adjustment factors, partitioned by trade date.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-daily-basic",
        description="Mirror A-share daily valuation metrics, partitioned by trade date.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-stk-limit",
        description="Mirror A-share daily limit prices from stk_limit.",
    )
    add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-limit-status",
        description="Alias for mirror-cn-stk-limit using the limit_status asset key.",
    )
