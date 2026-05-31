from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from market_data_platform.contract import build_current_contract, write_current_contract
from market_data_platform.deprecations import warn_deprecated_command
from market_data_platform.hk_workflows import (
    HK_INSPECT_ASSET_CHOICES,
    HK_REFRESH_ASSET_CHOICES,
    run_hk_current_health,
    run_hk_current_refresh,
    run_hk_depth_refresh,
    run_hk_fundamentals_refresh,
    run_hk_intraday_refresh,
    sync_hk_transition_links,
)
from market_data_platform.paths import (
    SUPPORTED_MARKETS,
    candidate_asset_paths,
    current_contract_path,
    dataset_registry_path,
    normalize_provider,
    resolve_artifacts_root,
)
from market_data_platform.providers.rqdata_a_share import (
    export_a_share_instruments,
    mirror_a_share_daily,
)
from market_data_platform.providers.tushare_a_share import (
    export_a_share_instruments as export_tushare_a_share_instruments,
)
from market_data_platform.providers.tushare_a_share import (
    mirror_a_share_adj_factor as mirror_tushare_a_share_adj_factor,
)
from market_data_platform.providers.tushare_a_share import (
    mirror_a_share_daily as mirror_tushare_a_share_daily,
)
from market_data_platform.providers.tushare_a_share import (
    mirror_a_share_daily_basic as mirror_tushare_a_share_daily_basic,
)
from market_data_platform.providers.tushare_a_share import (
    mirror_a_share_limit_status as mirror_tushare_a_share_limit_status,
)
from market_data_platform.providers.tushare_a_share import (
    mirror_a_share_trade_cal as mirror_tushare_a_share_trade_cal,
)
from market_data_platform.providers.tushare_a_share import (
    verify_tushare_tokens,
)
from market_data_platform.providers.tushare_a_share_clean import (
    build_a_share_daily_clean as build_tushare_a_share_daily_clean,
)
from market_data_platform.providers.tushare_a_share_clean import (
    validate_a_share_daily_clean as validate_tushare_a_share_daily_clean,
)
from market_data_platform.registry import (
    render_combined_dataset_registry_csv,
    write_combined_dataset_registry,
)
from market_data_platform.transitions import transition_status
from market_data_platform.tushare_cli import add_tushare_parser

MARKET_CHOICES = tuple(market for market in ("hk", "a_share") if market in SUPPORTED_MARKETS)
REGISTRY_MARKET_CHOICES = ("all", *MARKET_CHOICES)
PROVIDER_CHOICES = ("rqdata", "tushare")


def _strip_backend_separator(argv: list[str]) -> list[str]:
    return argv[1:] if argv[:1] == ["--"] else argv


def _run_hk_depth_cli(argv: list[str]) -> int:
    from market_data_platform.hk_depth.cli import main as hk_depth_main

    return hk_depth_main(argv)


def _run_hk_assets_cli(argv: list[str]) -> int:
    from market_data_platform.hk_assets.cli import main as hk_assets_main

    return hk_assets_main(argv)


def _add_paths_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("paths", help="Print shared data platform paths.")
    parser.add_argument("--artifacts-root")
    parser.add_argument("--market", default="hk", choices=MARKET_CHOICES)
    parser.add_argument("--provider", choices=PROVIDER_CHOICES)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def _add_contract_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("contract", help="Current contract helpers.")
    contract_subparsers = parser.add_subparsers(dest="contract_command", required=True)
    build = contract_subparsers.add_parser(
        "build",
        help="Build <market>_current.json from standard paths.",
    )
    build.add_argument("--artifacts-root")
    build.add_argument("--market", default="hk", choices=MARKET_CHOICES)
    build.add_argument("--provider", choices=PROVIDER_CHOICES)
    build.add_argument("--target-date")
    build.add_argument("--generated-by", default="marketdata contract build")
    build.add_argument(
        "--out",
        help="Default: <artifacts-root>/metadata/current_assets/<market>_current.json",
    )
    build.add_argument(
        "--registry-out",
        help="Default: <artifacts-root>/metadata/dataset_registry.csv",
    )
    build.add_argument(
        "--no-registry",
        action="store_true",
        help="Only write <market>_current.json; skip dataset_registry.csv.",
    )
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="Print contract JSON without writing it.",
    )


def _add_registry_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("registry", help="Dataset registry helpers.")
    registry_subparsers = parser.add_subparsers(dest="registry_command", required=True)
    build = registry_subparsers.add_parser(
        "build",
        help="Build dataset_registry.csv from current contracts.",
    )
    build.add_argument("--artifacts-root")
    build.add_argument("--market", default="all", choices=REGISTRY_MARKET_CHOICES)
    build.add_argument(
        "--contract",
        help=(
            "Use one explicit current contract. Default: combine existing "
            "metadata/current_assets/{hk,a_share}_current.json files."
        ),
    )
    build.add_argument(
        "--out",
        help="Default: <artifacts-root>/metadata/dataset_registry.csv",
    )
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="Print registry CSV without writing it.",
    )


def _add_data_parser(subparsers: argparse._SubParsersAction) -> None:
    from market_data_platform import data_warehouse

    parser = subparsers.add_parser(
        "data",
        help="Catalog, materialize, and query manifest-backed data assets.",
    )
    data_subparsers = parser.add_subparsers(dest="data_command", required=True)

    catalog = data_subparsers.add_parser(
        "catalog",
        help="Refresh the local manifest-backed artifact catalog.",
    )
    data_warehouse.add_catalog_args(catalog)

    materialize = data_subparsers.add_parser(
        "materialize",
        help="Materialize an input asset or file into the standardized layer.",
    )
    data_warehouse.add_materialize_args(materialize)

    query = data_subparsers.add_parser(
        "query",
        help="Register standardized views in DuckDB and run a query.",
    )
    data_warehouse.add_query_args(query)


def _add_backup_parser(subparsers: argparse._SubParsersAction) -> None:
    from market_data_platform.backup_data import add_backup_data_args

    parser = subparsers.add_parser(
        "backup-data",
        help=(
            "Create a private local snapshot of caches, universe files, configs, "
            "or HK current assets."
        ),
    )
    add_backup_data_args(parser)


def _add_rqdata_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("rqdata", help="RQData mirror/export helpers.")
    rqdata_subparsers = parser.add_subparsers(dest="rqdata_command", required=True)

    instruments = rqdata_subparsers.add_parser(
        "export-a-share-instruments",
        help="Export A-share instruments from RQData.",
    )
    instruments.add_argument("--out", required=True)
    instruments.add_argument("--date")
    instruments.add_argument("--instrument-type", default="CS")
    instruments.add_argument("--symbols-out")

    daily = rqdata_subparsers.add_parser(
        "mirror-a-share-daily",
        help="Mirror A-share daily bars from RQData into a parquet asset directory.",
    )
    daily.add_argument("--symbols-file", required=True)
    daily.add_argument("--out-dir", required=True)
    daily.add_argument("--start-date", required=True)
    daily.add_argument("--end-date", required=True)
    daily.add_argument(
        "--fields",
        nargs="+",
        help="Default: open high low close volume total_turnover.",
    )
    daily.add_argument("--adjust-type", default="pre")
    daily.add_argument("--skip-existing", action="store_true")

    refresh_hk = rqdata_subparsers.add_parser(
        "refresh-hk-current",
        help="Run the transitional HK current refresh workflow from the platform entrypoint.",
    )
    refresh_hk.add_argument("--artifacts-root")
    refresh_hk.add_argument("--target-date", required=True)
    refresh_hk.add_argument("--config")
    refresh_hk.add_argument("--refresh-mode", choices=("full", "patch"), default="patch")
    refresh_hk.add_argument(
        "--refresh-asset",
        action="append",
        choices=HK_REFRESH_ASSET_CHOICES,
        default=[],
        help="Only refresh selected HK asset(s). Repeatable.",
    )
    refresh_hk.add_argument(
        "--inspect-asset",
        action="append",
        choices=HK_INSPECT_ASSET_CHOICES,
        default=[],
        help="Only inspect selected HK asset(s). Repeatable.",
    )
    refresh_hk.add_argument("--daily-patch-lookback-days", type=int, default=20)
    refresh_hk.add_argument("--dated-patch-lookback-days", type=int, default=40)
    refresh_hk.add_argument(
        "--gate-on-severity",
        choices=("none", "info", "warning", "error"),
        default="warning",
    )
    refresh_hk.add_argument(
        "--inspect-fail-on-severity",
        choices=("none", "info", "warning", "error"),
        default="none",
    )
    refresh_hk.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not pass --resume to the transition workflow.",
    )
    refresh_hk.set_defaults(resume=True)
    refresh_hk.add_argument("--skip-history", action="store_true")
    refresh_hk.add_argument("--no-refresh-universe", action="store_true")
    refresh_hk.add_argument("--workflow-report")
    refresh_hk.add_argument("--dry-run", action="store_true")
    refresh_hk.add_argument(
        "--no-sync-transition-links",
        action="store_false",
        dest="sync_transition_links",
        help="Do not repoint cross-sectional-trees transition artifact symlinks first.",
    )
    refresh_hk.set_defaults(sync_transition_links=True)
    refresh_hk.add_argument(
        "--no-rebuild-contract",
        action="store_false",
        dest="rebuild_contract",
        help="Skip rebuilding hk_current.json and dataset_registry.csv after success.",
    )
    refresh_hk.set_defaults(rebuild_contract=True)

    current_health_hk = rqdata_subparsers.add_parser(
        "inspect-hk-current",
        help="Inspect the HK current contract through the platform entrypoint.",
    )
    current_health_hk.add_argument("--artifacts-root")
    current_health_hk.add_argument("--target-date")
    current_health_hk.add_argument(
        "--asset",
        action="append",
        default=[],
        help="Only inspect selected HK current asset key(s). Repeatable.",
    )
    current_health_hk.add_argument(
        "--fail-on-severity",
        choices=("none", "info", "warning", "error"),
        default="none",
    )
    current_health_hk.add_argument("--out")
    current_health_hk.add_argument("--dry-run", action="store_true")
    current_health_hk.add_argument(
        "--no-sync-transition-links",
        action="store_false",
        dest="sync_transition_links",
        help="Do not repoint cross-sectional-trees transition artifact links first.",
    )
    current_health_hk.set_defaults(sync_transition_links=True)

    intraday_hk = rqdata_subparsers.add_parser(
        "refresh-hk-intraday",
        help="Download and publish HK intraday 5m data through the platform entrypoint.",
    )
    intraday_hk.add_argument("--artifacts-root")
    intraday_hk.add_argument("--start-date", required=True)
    intraday_hk.add_argument("--end-date", required=True)
    intraday_hk.add_argument("--frequency", default="5m")
    intraday_hk.add_argument("--symbols-file")
    intraday_hk.add_argument("--batch-size", type=int, default=50)
    intraday_hk.add_argument("--config")
    intraday_hk.add_argument(
        "--inspect-fail-on-severity",
        choices=("none", "info", "warning", "error"),
        default="error",
    )
    intraday_hk.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not pass --resume to the transition workflow.",
    )
    intraday_hk.set_defaults(resume=True)
    intraday_hk.add_argument("--verify-sampled-segments", type=int, default=0)
    intraday_hk.add_argument("--verify-full-asset", action="store_true")
    intraday_hk.add_argument("--dry-run", action="store_true")
    intraday_hk.add_argument(
        "--no-sync-transition-links",
        action="store_false",
        dest="sync_transition_links",
        help="Do not repoint cross-sectional-trees transition artifact symlinks first.",
    )
    intraday_hk.set_defaults(sync_transition_links=True)
    intraday_hk.add_argument(
        "--no-rebuild-contract",
        action="store_false",
        dest="rebuild_contract",
        help="Skip rebuilding hk_current.json and dataset_registry.csv after success.",
    )
    intraday_hk.set_defaults(rebuild_contract=True)

    depth_hk = rqdata_subparsers.add_parser(
        "refresh-hk-depth",
        help="Download, validate, aggregate, and publish HK tick-depth data.",
    )
    depth_hk.add_argument("--artifacts-root")
    depth_hk.add_argument("--start-date", required=True)
    depth_hk.add_argument("--end-date", required=True)
    depth_hk.add_argument("--symbols")
    depth_hk.add_argument("--symbols-file")
    depth_hk.add_argument("--name")
    depth_hk.add_argument("--fields")
    depth_hk.add_argument("--batch-size", type=int, default=5)
    depth_hk.add_argument("--raw-layout", choices=("symbol-date", "batch"), default="symbol-date")
    depth_hk.add_argument("--calendar", choices=("provider", "calendar"), default="provider")
    depth_hk.add_argument(
        "--fail-on-severity",
        choices=("none", "info", "warning", "error"),
        default="error",
    )
    depth_hk.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not pass --resume to the depth downloader.",
    )
    depth_hk.set_defaults(resume=True)
    depth_hk.add_argument("--continue-on-error", action="store_true")
    depth_hk.add_argument(
        "--no-publish-assets",
        action="store_false",
        dest="publish_assets",
        help="Run download/health/aggregate but skip emit-asset and latest aliases.",
    )
    depth_hk.set_defaults(publish_assets=True)
    depth_hk.add_argument("--dry-run", action="store_true")
    depth_hk.add_argument(
        "--no-rebuild-contract",
        action="store_false",
        dest="rebuild_contract",
        help="Skip rebuilding hk_current.json and dataset_registry.csv after success.",
    )
    depth_hk.set_defaults(rebuild_contract=True)

    fundamentals_hk = rqdata_subparsers.add_parser(
        "refresh-hk-fundamentals",
        help="Refresh HK PIT and financial-details assets through the platform entrypoint.",
    )
    fundamentals_hk.add_argument("--artifacts-root")
    fundamentals_hk.add_argument("--target-date", required=True)
    fundamentals_hk.add_argument("--config")
    fundamentals_hk.add_argument("--symbols-file")
    fundamentals_hk.add_argument("--financial-fields-file")
    fundamentals_hk.add_argument("--pit-patch-start-quarter", default="2024q4")
    fundamentals_hk.add_argument("--pit-patch-end-quarter", default="2026q1")
    fundamentals_hk.add_argument("--financial-start-quarter", default="2000q1")
    fundamentals_hk.add_argument("--financial-end-quarter", default="2026q1")
    fundamentals_hk.add_argument(
        "--no-pit-inspect",
        action="store_false",
        dest="inspect_pit",
        help="Skip PIT coverage/health inspection after the PIT patch.",
    )
    fundamentals_hk.set_defaults(inspect_pit=True)
    fundamentals_hk.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Do not pass --resume to transition mirror commands.",
    )
    fundamentals_hk.set_defaults(resume=True)
    fundamentals_hk.add_argument("--dry-run", action="store_true")
    fundamentals_hk.add_argument(
        "--no-sync-transition-links",
        action="store_false",
        dest="sync_transition_links",
        help="Do not repoint cross-sectional-trees transition artifact symlinks first.",
    )
    fundamentals_hk.set_defaults(sync_transition_links=True)
    fundamentals_hk.add_argument(
        "--no-rebuild-contract",
        action="store_false",
        dest="rebuild_contract",
        help="Skip rebuilding hk_current.json and dataset_registry.csv after success.",
    )
    fundamentals_hk.set_defaults(rebuild_contract=True)

    for name, help_text, backend_description in (
        (
            "hk-depth",
            "Run native HK tick-depth workflows through the platform entrypoint.",
            "native HK depth CLI",
        ),
        (
            "hk-assets",
            "Run native HK RQData asset workflows through the platform entrypoint.",
            "native HK assets CLI",
        ),
    ):
        transition = rqdata_subparsers.add_parser(
            name,
            help=help_text,
            description=(
                f"{help_text} Pass backend options after `--`, for example: "
                f"`marketdata rqdata {name} -- --help`."
            ),
        )
        transition.add_argument(
            "backend_args",
            nargs=argparse.REMAINDER,
            help=f"Arguments forwarded unchanged to the {backend_description}.",
        )


def _add_migration_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("migration", help="Data workflow migration status helpers.")
    migration_subparsers = parser.add_subparsers(dest="migration_command", required=True)
    status = migration_subparsers.add_parser(
        "status",
        help="Show native and transition-backed data workflow ownership.",
    )
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    sync = migration_subparsers.add_parser(
        "sync-hk-links",
        help="Point transitional cross-sectional-trees HK artifact links at DATA_PLATFORM_ROOT.",
    )
    sync.add_argument("--artifacts-root")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    import_cross = migration_subparsers.add_parser(
        "import-cross-artifacts",
        help="Copy platform-owned historical artifacts out of cross-sectional-trees.",
    )
    import_cross.add_argument("--artifacts-root")
    import_cross.add_argument("--cross-artifacts-root")
    import_cross.add_argument("--workspace-root")
    import_cross.add_argument(
        "--apply",
        action="store_true",
        help="Copy files. Omit this flag for a dry-run plan.",
    )
    import_cross.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite differing target files. Symlink targets are never overwritten.",
    )
    import_cross.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketdata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_paths_parser(subparsers)
    _add_contract_parser(subparsers)
    _add_registry_parser(subparsers)
    _add_data_parser(subparsers)
    _add_backup_parser(subparsers)
    _add_rqdata_parser(subparsers)
    add_tushare_parser(subparsers)
    _add_migration_parser(subparsers)
    return parser


def _handle_paths(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    provider = normalize_provider(args.provider, market=args.market)
    payload = {
        "artifacts_root": str(root),
        "market": args.market,
        "provider": provider,
        "current_contract": str(current_contract_path(root, market=args.market)),
        "dataset_registry": str(dataset_registry_path(root)),
        "assets": {
            key: str(path)
            for key, path in candidate_asset_paths(
                root,
                market=args.market,
                provider=provider,
            ).items()
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"artifacts_root: {payload['artifacts_root']}")
        print(f"market: {payload['market']}")
        print(f"provider: {payload['provider']}")
        print(f"current_contract: {payload['current_contract']}")
        print(f"dataset_registry: {payload['dataset_registry']}")
        for key, path in payload["assets"].items():
            print(f"{key}: {path}")
    return 0


def _load_contract(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Contract is not a JSON object: {path}")
    return payload


def _contract_market(contract: dict[str, object]) -> str:
    meta = contract.get("contract")
    if not isinstance(meta, dict):
        return "hk"
    return str(meta.get("market") or "hk").strip().lower() or "hk"


def _with_contract_path(payload: dict[str, Any], output: Path) -> dict[str, Any]:
    normalized = dict(payload)
    meta = dict(normalized.get("contract") or {})
    meta["contract_path"] = str(output)
    normalized["contract"] = meta
    return normalized


def _load_registry_contracts(
    root: Path,
    *,
    market: str,
    explicit_contract: str | None = None,
    override_payload: dict[str, Any] | None = None,
) -> list[dict[str, object]]:
    if explicit_contract is not None:
        return [_load_contract(Path(explicit_contract).expanduser().resolve())]

    markets = MARKET_CHOICES if market == "all" else (market,)
    contracts: list[dict[str, object]] = []
    override_market = _contract_market(override_payload) if override_payload is not None else None
    for candidate_market in markets:
        if override_payload is not None and candidate_market == override_market:
            contracts.append(override_payload)
            continue
        path = current_contract_path(root, market=candidate_market)
        if path.exists():
            contracts.append(_load_contract(path))
    if not contracts:
        raise FileNotFoundError(
            f"No current contracts found under {root / 'metadata' / 'current_assets'}."
        )
    return contracts


def _handle_contract_build(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    output = current_contract_path(root, market=args.market)
    if args.out is not None:
        output = Path(args.out).expanduser().resolve()
    registry_output = dataset_registry_path(root)
    if args.registry_out is not None:
        registry_output = Path(args.registry_out).expanduser().resolve()
    payload = build_current_contract(
        root,
        market=args.market,
        provider=args.provider,
        generated_by=args.generated_by,
        target_date=args.target_date,
    )
    payload = _with_contract_path(payload, output)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    write_current_contract(output, payload)
    print(f"current_contract: {output}")
    if not args.no_registry:
        contracts = _load_registry_contracts(
            root,
            market="all",
            override_payload=payload,
        )
        write_combined_dataset_registry(registry_output, contracts)
        print(f"dataset_registry: {registry_output}")
    return 0


def _handle_registry_build(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    output = dataset_registry_path(root)
    if args.out is not None:
        output = Path(args.out).expanduser().resolve()
    contracts = _load_registry_contracts(
        root,
        market=args.market,
        explicit_contract=args.contract,
    )
    if args.dry_run:
        print(render_combined_dataset_registry_csv(contracts), end="")
        return 0
    write_combined_dataset_registry(output, contracts)
    print(str(output))
    return 0


def _handle_data(args: argparse.Namespace) -> int:
    from market_data_platform import data_warehouse

    if args.data_command == "catalog":
        return int(data_warehouse.refresh_catalog(args) or 0)
    if args.data_command == "materialize":
        return int(data_warehouse.materialize_standardized(args) or 0)
    if args.data_command == "query":
        return int(data_warehouse.query_standardized(args) or 0)
    raise ValueError(f"Unknown data command: {args.data_command}")


def _handle_backup_data(args: argparse.Namespace) -> int:
    from market_data_platform.backup_data import run_backup

    return run_backup(args)


def _handle_rqdata(args: argparse.Namespace) -> int:
    if args.rqdata_command == "export-a-share-instruments":
        summary = export_a_share_instruments(
            out=args.out,
            date=args.date,
            instrument_type=args.instrument_type,
            symbols_out=args.symbols_out,
        )
    elif args.rqdata_command == "mirror-a-share-daily":
        summary = mirror_a_share_daily(
            symbols_file=args.symbols_file,
            out_dir=args.out_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            fields=args.fields,
            adjust_type=args.adjust_type,
            skip_existing=args.skip_existing,
        )
    elif args.rqdata_command == "hk-depth":
        return _run_hk_depth_cli(_strip_backend_separator(list(args.backend_args)))
    elif args.rqdata_command == "hk-assets":
        return _run_hk_assets_cli(_strip_backend_separator(list(args.backend_args)))
    elif args.rqdata_command == "refresh-hk-current":
        summary = run_hk_current_refresh(
            artifacts_root=args.artifacts_root,
            target_date=args.target_date,
            refresh_assets=args.refresh_asset,
            inspect_assets=args.inspect_asset,
            refresh_mode=args.refresh_mode,
            daily_patch_lookback_days=args.daily_patch_lookback_days,
            dated_patch_lookback_days=args.dated_patch_lookback_days,
            gate_on_severity=args.gate_on_severity,
            inspect_fail_on_severity=args.inspect_fail_on_severity,
            resume=args.resume,
            skip_history=args.skip_history,
            no_refresh_universe=args.no_refresh_universe,
            config=args.config,
            workflow_report=args.workflow_report,
            dry_run=args.dry_run,
            sync_transition_links=args.sync_transition_links,
            rebuild_contract=args.rebuild_contract,
        )
    elif args.rqdata_command == "inspect-hk-current":
        summary = run_hk_current_health(
            artifacts_root=args.artifacts_root,
            target_date=args.target_date,
            assets=args.asset,
            fail_on_severity=args.fail_on_severity,
            output=args.out,
            dry_run=args.dry_run,
            sync_transition_links=args.sync_transition_links,
        )
    elif args.rqdata_command == "refresh-hk-intraday":
        summary = run_hk_intraday_refresh(
            artifacts_root=args.artifacts_root,
            start_date=args.start_date,
            end_date=args.end_date,
            frequency=args.frequency,
            symbols_file=args.symbols_file,
            batch_size=args.batch_size,
            inspect_fail_on_severity=args.inspect_fail_on_severity,
            resume=args.resume,
            verify_sampled_segments=args.verify_sampled_segments,
            verify_full_asset=args.verify_full_asset,
            config=args.config,
            dry_run=args.dry_run,
            sync_transition_links=args.sync_transition_links,
            rebuild_contract=args.rebuild_contract,
        )
    elif args.rqdata_command == "refresh-hk-depth":
        summary = run_hk_depth_refresh(
            artifacts_root=args.artifacts_root,
            start_date=args.start_date,
            end_date=args.end_date,
            symbols=args.symbols,
            symbols_file=args.symbols_file,
            name=args.name,
            fields=args.fields,
            batch_size=args.batch_size,
            raw_layout=args.raw_layout,
            calendar=args.calendar,
            fail_on_severity=args.fail_on_severity,
            resume=args.resume,
            continue_on_error=args.continue_on_error,
            publish_assets=args.publish_assets,
            dry_run=args.dry_run,
            rebuild_contract=args.rebuild_contract,
        )
    elif args.rqdata_command == "refresh-hk-fundamentals":
        summary = run_hk_fundamentals_refresh(
            artifacts_root=args.artifacts_root,
            target_date=args.target_date,
            pit_patch_start_quarter=args.pit_patch_start_quarter,
            pit_patch_end_quarter=args.pit_patch_end_quarter,
            financial_start_quarter=args.financial_start_quarter,
            financial_end_quarter=args.financial_end_quarter,
            symbols_file=args.symbols_file,
            financial_fields_file=args.financial_fields_file,
            inspect_pit=args.inspect_pit,
            config=args.config,
            resume=args.resume,
            dry_run=args.dry_run,
            sync_transition_links=args.sync_transition_links,
            rebuild_contract=args.rebuild_contract,
        )
    else:
        raise ValueError(f"Unknown rqdata command: {args.rqdata_command}")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return int(summary.get("returncode", 0)) if isinstance(summary, dict) else 0


def _handle_tushare(args: argparse.Namespace) -> int:
    if args.tushare_command == "verify-token":
        summary = verify_tushare_tokens(env_keys=args.env_keys)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["valid_tokens"] else 1
    if args.tushare_command == "export-a-share-instruments":
        summary = export_tushare_a_share_instruments(
            out=args.out,
            list_statuses=args.list_statuses,
            fields=args.fields,
            symbols_out=args.symbols_out,
            token_env=args.token_env,
        )
    elif args.tushare_command == "mirror-a-share-trade-cal":
        summary = mirror_tushare_a_share_trade_cal(
            out=args.out,
            start_date=args.start_date,
            end_date=args.end_date,
            exchange=args.exchange,
            token_env=args.token_env,
        )
    elif args.tushare_command == "build-a-share-daily-clean":
        summary = build_tushare_a_share_daily_clean(
            daily_dir=args.daily_dir,
            adj_factor_dir=args.adj_factor_dir,
            daily_basic_dir=args.daily_basic_dir,
            limit_status_dir=args.limit_status_dir,
            suspend_dir=args.suspend_dir,
            instruments_file=args.instruments_file,
            out_dir=args.out_dir,
            min_rows=args.min_rows,
            min_symbols=args.min_symbols,
        )
    elif args.tushare_command == "validate-a-share-daily-clean":
        summary = validate_tushare_a_share_daily_clean(
            daily_clean_dir=args.daily_clean_dir,
            min_rows=args.min_rows,
            min_symbols=args.min_symbols,
            require_valuation=args.require_valuation,
            require_limit_status=args.require_limit_status,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["status"] == "passed" else 1
    else:
        commands = {
            "mirror-a-share-daily": mirror_tushare_a_share_daily,
            "mirror-a-share-adj-factor": mirror_tushare_a_share_adj_factor,
            "mirror-a-share-daily-basic": mirror_tushare_a_share_daily_basic,
            "mirror-a-share-limit-status": mirror_tushare_a_share_limit_status,
        }
        handler = commands.get(args.tushare_command)
        if handler is None:
            raise ValueError(f"Unknown tushare command: {args.tushare_command}")
        summary = handler(
            out_dir=args.out_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            fields=args.fields,
            skip_existing=args.skip_existing,
            token_env=args.token_env,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _handle_migration_status(args: argparse.Namespace) -> int:
    warn_deprecated_command(
        "marketdata migration status",
        "docs/migration-plan.md and native marketdata rqdata commands",
    )
    payload = {
        "native": [
            {
                "name": "a-share-tushare",
                "status": "native",
                "capability": (
                    "A-share instruments, trade calendar, daily, adj-factor, daily-basic, "
                    "and stk-limit mirrors"
                ),
            },
            {
                "name": "hk-depth",
                "status": "native",
                "capability": (
                    "HK tick-depth download, health, aggregate, reconcile, "
                    "and package workflows"
                ),
            },
            {
                "name": "hk-assets",
                "status": "native",
                "capability": (
                    "HK RQData daily, PIT, valuation, clean, health, intraday, "
                    "and release workflows"
                ),
            },
            {
                "name": "a-share-rqdata",
                "status": "native",
                "capability": "A-share instruments and daily mirror MVP",
            },
        ],
        "transition_backends": transition_status(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    for item in payload["native"]:
        print(f"{item['name']}: native - {item['capability']}")
    for item in payload["transition_backends"]:
        availability = "available" if item["available"] else "unavailable"
        print(f"{item['name']}: transition_backend ({availability}) - {item['backend_repo']}")
        print(f"  command: {item['platform_command']}")
    return 0


def _handle_migration_sync_hk_links(args: argparse.Namespace) -> int:
    warn_deprecated_command(
        "marketdata migration sync-hk-links",
        "current contract consumers with explicit artifacts roots",
    )
    payload = sync_hk_transition_links(
        args.artifacts_root,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for row in payload:
            source = row.get("target") or row.get("source")
            destination = row.get("link") or row.get("file")
            print(f"{row['status']}: {destination} -> {source}")
    return 0


def _handle_migration_import_cross_artifacts(args: argparse.Namespace) -> int:
    from market_data_platform.hk_workflows import import_cross_platform_artifacts

    warn_deprecated_command(
        "marketdata migration import-cross-artifacts",
        "scripts/internal/import_cross_artifacts.py or archived migration documentation",
    )
    payload = import_cross_platform_artifacts(
        args.artifacts_root,
        cross_artifacts_root=args.cross_artifacts_root,
        workspace_root=args.workspace_root,
        dry_run=not args.apply,
        overwrite=args.overwrite,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"source_artifacts_root: {payload['source_artifacts_root']}")
    print(f"target_artifacts_root: {payload['target_artifacts_root']}")
    print(f"dry_run: {payload['dry_run']}")
    print(f"summary: {payload['summary']}")
    if "manifest" in payload:
        print(f"manifest: {payload['manifest']}")
    items = cast(list[dict[str, object]], payload["items"])
    for row in items:
        print(f"{row['status']}: {row['relative_path']} -> {row['target']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "paths":
        return _handle_paths(args)
    if args.command == "contract" and args.contract_command == "build":
        return _handle_contract_build(args)
    if args.command == "registry" and args.registry_command == "build":
        return _handle_registry_build(args)
    if args.command == "data":
        return _handle_data(args)
    if args.command == "backup-data":
        return _handle_backup_data(args)
    if args.command == "rqdata":
        return _handle_rqdata(args)
    if args.command == "tushare":
        return _handle_tushare(args)
    if args.command == "migration" and args.migration_command == "status":
        return _handle_migration_status(args)
    if args.command == "migration" and args.migration_command == "sync-hk-links":
        return _handle_migration_sync_hk_links(args)
    if args.command == "migration" and args.migration_command == "import-cross-artifacts":
        return _handle_migration_import_cross_artifacts(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
