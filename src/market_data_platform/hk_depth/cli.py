"""Command line interface for RQData HK ten-level depth snapshot tooling."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from market_data_platform.deprecations import warn_if_legacy_console_script
from market_data_platform.hk_depth.aggregate import write_daily_aggregate
from market_data_platform.hk_depth.assets import emit_daily_asset, emit_raw_asset
from market_data_platform.hk_depth.compact import (
    COMPACT_GROUPINGS,
    DUPLICATE_POLICIES,
    compact_raw_cache,
)
from market_data_platform.hk_depth.downloader import (
    download_tick_depth,
    probe_tick_depth,
    provider_error_to_exit,
)
from market_data_platform.hk_depth.fields import parse_fields
from market_data_platform.hk_depth.health import format_health_summary, write_health_report
from market_data_platform.hk_depth.quota import augment_quota_payload, format_quota_pretty
from market_data_platform.hk_depth.recompress import recompress_raw_cache
from market_data_platform.hk_depth.reconcile import (
    REFERENCE_POLICIES,
    ReconcileConfig,
    write_reconciliation_report,
)
from market_data_platform.hk_depth.release_assets import (
    ARCHIVE_FORMATS,
    DEFAULT_ARCHIVE_FORMAT,
    DEFAULT_MAX_TAR_BYTES,
    PART_NAMES,
    RAW_DEDUPE_MODES,
    package_tick_assets,
    upload_release_assets,
)
from market_data_platform.hk_depth.rq_client import RQDataClient, TickDataProvider
from market_data_platform.hk_depth.storage import DEFAULT_PARQUET_COMPRESSION
from market_data_platform.hk_depth.symbols import parse_symbols


def _provider(fake: bool) -> TickDataProvider:
    if fake:
        from market_data_platform.hk_depth.testing import FakeProvider

        return FakeProvider()
    return RQDataClient()


def _print_json(data: dict[str, object]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _add_probe_parser(subparsers: argparse._SubParsersAction) -> None:
    probe = subparsers.add_parser("probe", help="Run a one-symbol one-day provider probe.")
    probe.add_argument("--symbol", required=True)
    probe.add_argument("--date", required=True)
    probe.add_argument("--fields")
    probe.add_argument("--adjust-type", default="none")
    probe.add_argument("--time-slice")
    probe.add_argument("--out", default="artifacts/cache/rqdata/hk_tick_depth/probe")
    probe.add_argument("--fake-provider", action="store_true")


def _add_download_parser(subparsers: argparse._SubParsersAction) -> None:
    download = subparsers.add_parser("download", help="Download HK depth snapshot parquet parts.")
    download.add_argument("--symbols")
    download.add_argument("--symbols-file")
    download.add_argument("--start-date", required=True)
    download.add_argument("--end-date", required=True)
    download.add_argument("--fields")
    download.add_argument("--adjust-type", default="none")
    download.add_argument("--time-slice")
    download.add_argument("--out", required=True)
    download.add_argument("--batch-size", type=int, default=5)
    download.add_argument("--raw-layout", choices=["symbol-date", "batch"], default="symbol-date")
    download.add_argument("--calendar", choices=["provider", "calendar"], default="provider")
    download.add_argument("--parquet-engine", default="pyarrow")
    download.add_argument(
        "--compression",
        dest="parquet_compression",
        default=DEFAULT_PARQUET_COMPRESSION,
    )
    download.add_argument("--compression-level", dest="parquet_compression_level", type=int)
    download.add_argument("--resume", dest="resume", action="store_true", default=True)
    download.add_argument("--no-resume", dest="resume", action="store_false")
    download.add_argument("--continue-on-error", action="store_true")
    download.add_argument("--dry-run", action="store_true")
    download.add_argument("--fake-provider", action="store_true")
    download.add_argument("--retry-max-attempts", type=int, default=1)
    download.add_argument("--retry-backoff-seconds", type=float, default=0.0)
    download.add_argument("--retry-max-backoff-seconds", type=float, default=60.0)
    download.add_argument("--quota-guard", dest="quota_guard", action="store_true", default=True)
    download.add_argument("--no-quota-guard", dest="quota_guard", action="store_false")
    download.add_argument("--quota-stop-ratio", type=float, default=0.95)
    download.add_argument("--quota-safety-multiplier", type=float, default=1.2)
    download.add_argument("--audit-output")
    download.add_argument("--metadata-detail-limit", type=int, default=1000)


def _add_quality_parser(subparsers: argparse._SubParsersAction) -> None:
    health = subparsers.add_parser("health", help="Inspect depth snapshot parquet cache health.")
    health.add_argument("--input", required=True)
    health.add_argument("--out-json")
    health.add_argument("--out-units")
    health.add_argument("--unit-sample-limit", type=int, default=20)
    health.add_argument(
        "--fail-on-severity",
        choices=["none", "info", "warning", "error"],
        default="error",
    )


def _add_aggregate_parser(subparsers: argparse._SubParsersAction) -> None:
    aggregate = subparsers.add_parser(
        "aggregate-daily",
        help="Aggregate raw depth snapshots to daily data.",
    )
    aggregate.add_argument("--input", required=True)
    aggregate.add_argument("--output", required=True)
    aggregate.add_argument("--meta-output")


def _add_storage_parsers(subparsers: argparse._SubParsersAction) -> None:
    recompress = subparsers.add_parser(
        "recompress-raw",
        help="Rewrite depth snapshot parquet parts with a different compression codec.",
    )
    recompress.add_argument("--input", required=True)
    recompress.add_argument("--output", required=True)
    recompress.add_argument(
        "--compression",
        dest="parquet_compression",
        default=DEFAULT_PARQUET_COMPRESSION,
    )
    recompress.add_argument("--compression-level", dest="parquet_compression_level", type=int)
    recompress.add_argument("--min-rewrite-bytes", type=int, default=0)
    recompress.add_argument("--resume", dest="resume", action="store_true", default=True)
    recompress.add_argument("--no-resume", dest="resume", action="store_false")
    recompress.add_argument("--continue-on-error", action="store_true")
    recompress.add_argument("--meta-output")
    recompress.add_argument("--out-units")
    recompress.add_argument("--progress", action="store_true")

    compact = subparsers.add_parser(
        "compact-raw",
        help="Merge symbol-date depth snapshot parts into cold-storage parquet files.",
    )
    compact.add_argument("--input", required=True)
    compact.add_argument("--output", required=True)
    compact.add_argument("--grouping", choices=COMPACT_GROUPINGS, default="symbol-quarter")
    compact.add_argument(
        "--compression",
        dest="parquet_compression",
        default=DEFAULT_PARQUET_COMPRESSION,
    )
    compact.add_argument("--compression-level", dest="parquet_compression_level", type=int)
    compact.add_argument("--row-group-days", type=int, default=1)
    compact.add_argument(
        "--duplicate-policy",
        choices=DUPLICATE_POLICIES,
        default="error",
    )
    compact.add_argument("--resume", dest="resume", action="store_true", default=True)
    compact.add_argument("--no-resume", dest="resume", action="store_false")
    compact.add_argument("--continue-on-error", action="store_true")
    compact.add_argument("--meta-output")
    compact.add_argument("--out-units")
    compact.add_argument("--progress", action="store_true")


def _add_delivery_parsers(subparsers: argparse._SubParsersAction) -> None:
    asset = subparsers.add_parser("emit-asset", help="Emit a deliverable data directory.")
    asset.add_argument("--kind", required=True, choices=["raw", "daily"])
    asset.add_argument("--source", required=True)
    asset.add_argument("--output", required=True)

    package_assets = subparsers.add_parser(
        "package-assets",
        help="Package local HK depth snapshot assets into release tarballs.",
    )
    package_assets.add_argument(
        "--preset",
        choices=["explicit", "current-cache"],
        default="explicit",
    )
    package_assets.add_argument("--name", default="hk-depth-snapshots")
    package_assets.add_argument("--as-of")
    package_assets.add_argument("--tar-dir")
    package_assets.add_argument("--overwrite", action="store_true")
    package_assets.add_argument("--dry-run", action="store_true")
    package_assets.add_argument("--part", action="append", choices=PART_NAMES, default=[])
    package_assets.add_argument("--raw-source", action="append", default=[])
    package_assets.add_argument("--daily-source", action="append", default=[])
    package_assets.add_argument("--metadata-source", action="append", default=[])
    package_assets.add_argument("--report-source", action="append", default=[])
    package_assets.add_argument("--config-source", action="append", default=[])
    package_assets.add_argument("--max-tar-bytes", type=int, default=DEFAULT_MAX_TAR_BYTES)
    package_assets.add_argument(
        "--archive-format",
        choices=ARCHIVE_FORMATS,
        default=DEFAULT_ARCHIVE_FORMAT,
    )
    package_assets.add_argument("--archive-compression-level", type=int)
    package_assets.add_argument("--raw-dedupe", choices=RAW_DEDUPE_MODES, default="none")
    package_assets.add_argument("--progress", action="store_true")

    release_assets = subparsers.add_parser(
        "release-assets",
        help="Upload packaged HK depth snapshot tarballs to a GitHub Release.",
    )
    release_assets.add_argument("--tar-dir", required=True)
    release_assets.add_argument("--tag", required=True)
    release_assets.add_argument("--repo")
    release_assets.add_argument("--title")
    release_assets.add_argument("--notes-file")
    release_assets.add_argument("--draft", action="store_true")
    release_assets.add_argument("--prerelease", action="store_true")
    release_assets.add_argument("--latest", action="store_true")
    release_assets.add_argument("--clobber", action="store_true")
    release_assets.add_argument("--dry-run", action="store_true")


def _add_provider_parser(subparsers: argparse._SubParsersAction) -> None:
    quota = subparsers.add_parser("quota", help="Show RQData quota usage.")
    quota.add_argument("--pretty", action="store_true")
    quota.add_argument("--fake-provider", action="store_true")


def _add_reconcile_parser(subparsers: argparse._SubParsersAction) -> None:
    reconcile = subparsers.add_parser(
        "reconcile-daily",
        help="Reconcile raw depth snapshots with external daily benchmark data.",
    )
    reconcile.add_argument("--tick-input", required=True)
    reconcile.add_argument("--daily-asset-dir", required=True)
    reconcile.add_argument("--out", required=True)
    reconcile.add_argument(
        "--reference-policy",
        choices=REFERENCE_POLICIES,
        default="raw-daily",
        help=(
            "raw-daily gates against a same-basis raw daily reference; cross-clean records "
            "numeric basis mismatches as info for research clean assets."
        ),
    )
    reconcile.add_argument(
        "--fail-on-severity",
        choices=["none", "info", "warning", "error"],
        default="error",
    )
    reconcile.add_argument("--price-rtol", type=float, default=1e-4)
    reconcile.add_argument("--price-atol", type=float, default=1e-4)
    reconcile.add_argument("--volume-rtol", type=float, default=1e-4)
    reconcile.add_argument("--volume-atol", type=float, default=1.0)
    reconcile.add_argument("--turnover-rtol", type=float, default=1e-4)
    reconcile.add_argument("--turnover-atol", type=float, default=1.0)
    reconcile.add_argument("--session-start", default="09:00")
    reconcile.add_argument("--session-end", default="16:30")
    reconcile.add_argument("--sample-limit", type=int, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rqdata-hk-depth")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_probe_parser(subparsers)
    _add_download_parser(subparsers)
    _add_quality_parser(subparsers)
    _add_aggregate_parser(subparsers)
    _add_storage_parsers(subparsers)
    _add_delivery_parsers(subparsers)
    _add_provider_parser(subparsers)
    _add_reconcile_parser(subparsers)
    return parser


def _handle_probe(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    selected_provider = provider or _provider(args.fake_provider)
    result = probe_tick_depth(
        provider=selected_provider,
        symbol=args.symbol,
        trade_date=args.date,
        fields=parse_fields(args.fields),
        output_root=Path(args.out),
        adjust_type=args.adjust_type,
        time_slice=args.time_slice,
    )
    _print_json(result)
    return 0


def _handle_download(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    symbols = parse_symbols(args.symbols, args.symbols_file)
    selected_provider = None
    if not args.dry_run or args.fake_provider:
        selected_provider = provider or _provider(args.fake_provider)
    result = download_tick_depth(
        provider=selected_provider,
        symbols=symbols,
        start_date=args.start_date,
        end_date=args.end_date,
        output_root=Path(args.out),
        fields=parse_fields(args.fields),
        batch_size=args.batch_size,
        adjust_type=args.adjust_type,
        time_slice=args.time_slice,
        calendar=args.calendar,
        resume=args.resume,
        continue_on_error=args.continue_on_error,
        dry_run=args.dry_run,
        raw_layout=args.raw_layout,
        parquet_engine=args.parquet_engine,
        parquet_compression=args.parquet_compression,
        parquet_compression_level=args.parquet_compression_level,
        retry_max_attempts=args.retry_max_attempts,
        retry_backoff_seconds=args.retry_backoff_seconds,
        retry_max_backoff_seconds=args.retry_max_backoff_seconds,
        quota_guard=args.quota_guard,
        quota_stop_ratio=args.quota_stop_ratio,
        quota_safety_multiplier=args.quota_safety_multiplier,
        audit_output=args.audit_output,
        metadata_detail_limit=args.metadata_detail_limit,
    )
    _print_json(result)
    return 0


def _handle_health(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    report = write_health_report(
        args.input,
        args.out_json,
        fail_on_severity=args.fail_on_severity,
        units_output=args.out_units,
        unit_sample_limit=args.unit_sample_limit,
    )
    print(format_health_summary(report))
    print(f"report_path={report['report_path']}")
    if report.get("unit_diagnostics_path"):
        print(f"unit_diagnostics_path={report['unit_diagnostics_path']}")
    verdict = report.get("quality_verdict")
    if isinstance(verdict, dict) and verdict.get("gate_triggered"):
        return 2
    return 0 if report["status"] == "pass" else 1


def _handle_aggregate_daily(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    metadata = write_daily_aggregate(args.input, args.output, args.meta_output)
    _print_json(metadata)
    return 0


def _handle_recompress_raw(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    metadata = recompress_raw_cache(
        args.input,
        args.output,
        parquet_compression=args.parquet_compression,
        parquet_compression_level=args.parquet_compression_level,
        min_rewrite_bytes=args.min_rewrite_bytes,
        resume=args.resume,
        continue_on_error=args.continue_on_error,
        meta_output=args.meta_output,
        units_output=args.out_units,
        progress=args.progress,
    )
    _print_json(metadata)
    return 0 if metadata["status"] == "pass" else 1


def _handle_emit_asset(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    if args.kind == "raw":
        metadata = emit_raw_asset(args.source, args.output)
    else:
        metadata = emit_daily_asset(args.source, args.output)
    _print_json(metadata)
    return 0


def _handle_compact_raw(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    metadata = compact_raw_cache(
        args.input,
        args.output,
        grouping=args.grouping,
        parquet_compression=args.parquet_compression,
        parquet_compression_level=args.parquet_compression_level,
        row_group_days=args.row_group_days,
        duplicate_policy=args.duplicate_policy,
        resume=args.resume,
        continue_on_error=args.continue_on_error,
        meta_output=args.meta_output,
        units_output=args.out_units,
        progress=args.progress,
    )
    _print_json(metadata)
    return 0 if metadata["status"] == "pass" else 1


def _handle_package_assets(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    metadata = package_tick_assets(
        preset=args.preset,
        name=args.name,
        as_of=args.as_of,
        tar_dir=args.tar_dir,
        raw_sources=args.raw_source,
        daily_sources=args.daily_source,
        metadata_sources=args.metadata_source,
        report_sources=args.report_source,
        config_sources=args.config_source,
        parts=args.part,
        max_tar_bytes=args.max_tar_bytes,
        archive_format=args.archive_format,
        archive_compression_level=args.archive_compression_level,
        raw_dedupe=args.raw_dedupe,
        progress=args.progress,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    _print_json(metadata)
    return 0


def _handle_release_assets(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    metadata = upload_release_assets(
        tar_dir=args.tar_dir,
        tag=args.tag,
        repo=args.repo,
        title=args.title,
        notes_file=args.notes_file,
        draft=args.draft,
        prerelease=args.prerelease,
        latest=args.latest,
        clobber=args.clobber,
        dry_run=args.dry_run,
    )
    _print_json(metadata)
    return int(metadata.get("returncode") or 0)


def _handle_quota(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    selected_provider = provider or _provider(args.fake_provider)
    payload = augment_quota_payload(selected_provider.quota_snapshot())
    if args.pretty:
        print(format_quota_pretty(payload))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _handle_reconcile_daily(args: argparse.Namespace, provider: TickDataProvider | None) -> int:
    del provider
    config = ReconcileConfig(
        price_rtol=args.price_rtol,
        price_atol=args.price_atol,
        volume_rtol=args.volume_rtol,
        volume_atol=args.volume_atol,
        turnover_rtol=args.turnover_rtol,
        turnover_atol=args.turnover_atol,
        session_start=args.session_start,
        session_end=args.session_end,
        sample_limit=args.sample_limit,
        fail_on_severity=args.fail_on_severity,
        reference_policy=args.reference_policy,
    )
    report = write_reconciliation_report(
        args.tick_input,
        args.daily_asset_dir,
        args.out,
        config=config,
    )
    _print_json(
        {
            "report_path": report["report_path"],
            "reference_policy": report["reference_policy"],
            "summary": report["summary"],
            "quality_verdict": report["quality_verdict"],
            "status": report["status"],
        }
    )
    verdict = report.get("quality_verdict")
    if isinstance(verdict, dict) and verdict.get("gate_triggered"):
        return 2
    return 0 if report["status"] == "pass" else 1


COMMAND_HANDLERS = {
    "probe": _handle_probe,
    "download": _handle_download,
    "health": _handle_health,
    "aggregate-daily": _handle_aggregate_daily,
    "recompress-raw": _handle_recompress_raw,
    "compact-raw": _handle_compact_raw,
    "emit-asset": _handle_emit_asset,
    "package-assets": _handle_package_assets,
    "release-assets": _handle_release_assets,
    "quota": _handle_quota,
    "reconcile-daily": _handle_reconcile_daily,
}


def main(argv: list[str] | None = None, provider: TickDataProvider | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = COMMAND_HANDLERS.get(args.command)
        if handler is not None:
            return handler(args, provider)
    except Exception as exc:
        code, message = provider_error_to_exit(exc)
        print(message, file=sys.stderr)
        return code

    parser.error(f"Unhandled command {args.command!r}")
    return 2


def main_entry() -> None:
    warn_if_legacy_console_script(
        ("rqdata-hk-depth", "rqdata-tick"),
        "marketdata rqdata hk-depth -- <args>",
    )
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
