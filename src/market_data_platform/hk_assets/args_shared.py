from __future__ import annotations

import argparse


def _add_rqdata_credentials_args(
    parser: argparse.ArgumentParser,
    *,
    config_help: str,
) -> None:
    parser.add_argument("--config", help=config_help)
    parser.add_argument("--username", help="Override RQData username")
    parser.add_argument("--password", help="Override RQData password")


def _add_hk_symbol_selection_args(
    parser: argparse.ArgumentParser,
    *,
    symbol_help: str,
    symbols_file_help: str,
    by_date_file_help: str,
) -> None:
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help=symbol_help,
    )
    parser.add_argument(
        "--symbols-file",
        help=symbols_file_help,
    )
    parser.add_argument(
        "--by-date-file",
        help=by_date_file_help,
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional cap on the resolved symbol count after dedupe.",
    )


def _add_quality_gate_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fail-on-severity",
        default="none",
        choices=["none", "info", "warning", "error"],
        help=(
            "Optional quality gate threshold. "
            "The command exits non-zero when a quality issue at or above this severity is found. "
            "Default: none."
        ),
    )


def _add_mirror_output_args(
    parser: argparse.ArgumentParser,
    *,
    default_out_root: str,
) -> None:
    parser.add_argument(
        "--out-root",
        default=default_out_root,
        help=f"Mirror root directory. Default: {default_out_root}",
    )
    parser.add_argument(
        "--name",
        help="Optional snapshot folder name. Default: auto-generated from range + timestamp.",
    )


def _add_resume_args(
    parser: argparse.ArgumentParser,
    *,
    resume_help: str,
    include_skip_existing: bool = True,
) -> None:
    parser.add_argument(
        "--resume",
        action="store_true",
        help=resume_help,
    )
    if include_skip_existing:
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            help="Skip symbols whose parquet files already exist under data/. Implied by --resume.",
        )


def _add_retry_args(
    parser: argparse.ArgumentParser,
    *,
    max_attempts_default: int,
    backoff_seconds_default: float,
    max_backoff_seconds_default: float,
    attempts_help: str,
) -> None:
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=max_attempts_default,
        help=f"{attempts_help} Default: {max_attempts_default}.",
    )
    parser.add_argument(
        "--backoff-seconds",
        type=float,
        default=backoff_seconds_default,
        help=f"Initial retry backoff in seconds. Default: {backoff_seconds_default}.",
    )
    parser.add_argument(
        "--max-backoff-seconds",
        type=float,
        default=max_backoff_seconds_default,
        help=f"Maximum retry backoff in seconds. Default: {max_backoff_seconds_default}.",
    )
