from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from market_data_platform.contract import build_current_contract, write_current_contract
from market_data_platform.paths import (
    SUPPORTED_MARKETS,
    candidate_asset_paths,
    current_contract_path,
    dataset_registry_path,
    normalize_provider,
    resolve_artifacts_root,
)
from market_data_platform.registry import (
    render_combined_dataset_registry_csv,
    write_combined_dataset_registry,
)
from market_data_platform.rqdata_cn import export_cn_instruments, mirror_cn_daily
from market_data_platform.tushare_cn import (
    export_cn_instruments as export_tushare_cn_instruments,
)
from market_data_platform.tushare_cn import (
    mirror_cn_adj_factor as mirror_tushare_cn_adj_factor,
)
from market_data_platform.tushare_cn import (
    mirror_cn_daily as mirror_tushare_cn_daily,
)
from market_data_platform.tushare_cn import (
    mirror_cn_daily_basic as mirror_tushare_cn_daily_basic,
)
from market_data_platform.tushare_cn import (
    mirror_cn_limit_status as mirror_tushare_cn_limit_status,
)
from market_data_platform.tushare_cn import (
    mirror_cn_trade_cal as mirror_tushare_cn_trade_cal,
)
from market_data_platform.tushare_cn import verify_tushare_tokens

MARKET_CHOICES = tuple(market for market in ("hk", "cn") if market in SUPPORTED_MARKETS)
REGISTRY_MARKET_CHOICES = ("all", *MARKET_CHOICES)
PROVIDER_CHOICES = ("rqdata", "tushare")


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
            "metadata/current_assets/{hk,cn}_current.json files."
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


def _add_rqdata_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("rqdata", help="RQData mirror/export helpers.")
    rqdata_subparsers = parser.add_subparsers(dest="rqdata_command", required=True)

    instruments = rqdata_subparsers.add_parser(
        "export-cn-instruments",
        help="Export A-share instruments from RQData.",
    )
    instruments.add_argument("--out", required=True)
    instruments.add_argument("--date")
    instruments.add_argument("--instrument-type", default="CS")
    instruments.add_argument("--symbols-out")

    daily = rqdata_subparsers.add_parser(
        "mirror-cn-daily",
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


def _add_token_env_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token-env",
        default="TUSHARE_TOKEN",
        help="Environment variable containing the TuShare token (default: TUSHARE_TOKEN).",
    )


def _add_tushare_date_mirror_parser(
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
    _add_token_env_argument(parser)


def _add_tushare_parser(subparsers: argparse._SubParsersAction) -> None:
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
    _add_token_env_argument(instruments)

    trade_cal = tushare_subparsers.add_parser(
        "mirror-cn-trade-cal",
        help="Mirror the CN trading calendar from trade_cal.",
    )
    trade_cal.add_argument("--out", required=True)
    trade_cal.add_argument("--start-date", required=True)
    trade_cal.add_argument("--end-date", required=True)
    trade_cal.add_argument("--exchange", default="")
    _add_token_env_argument(trade_cal)

    _add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-daily",
        description="Mirror unadjusted A-share daily bars, partitioned by trade date.",
    )
    _add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-adj-factor",
        description="Mirror A-share adjustment factors, partitioned by trade date.",
    )
    _add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-daily-basic",
        description="Mirror A-share daily valuation metrics, partitioned by trade date.",
    )
    _add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-stk-limit",
        description="Mirror A-share daily limit prices from stk_limit.",
    )
    _add_tushare_date_mirror_parser(
        tushare_subparsers,
        command="mirror-cn-limit-status",
        description="Alias for mirror-cn-stk-limit using the limit_status asset key.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketdata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_paths_parser(subparsers)
    _add_contract_parser(subparsers)
    _add_registry_parser(subparsers)
    _add_rqdata_parser(subparsers)
    _add_tushare_parser(subparsers)
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


def _handle_rqdata(args: argparse.Namespace) -> int:
    if args.rqdata_command == "export-cn-instruments":
        summary = export_cn_instruments(
            out=args.out,
            date=args.date,
            instrument_type=args.instrument_type,
            symbols_out=args.symbols_out,
        )
    elif args.rqdata_command == "mirror-cn-daily":
        summary = mirror_cn_daily(
            symbols_file=args.symbols_file,
            out_dir=args.out_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            fields=args.fields,
            adjust_type=args.adjust_type,
            skip_existing=args.skip_existing,
        )
    else:
        raise ValueError(f"Unknown rqdata command: {args.rqdata_command}")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _handle_tushare(args: argparse.Namespace) -> int:
    if args.tushare_command == "verify-token":
        summary = verify_tushare_tokens(env_keys=args.env_keys)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["valid_tokens"] else 1
    if args.tushare_command == "export-cn-instruments":
        summary = export_tushare_cn_instruments(
            out=args.out,
            list_statuses=args.list_statuses,
            fields=args.fields,
            symbols_out=args.symbols_out,
            token_env=args.token_env,
        )
    elif args.tushare_command == "mirror-cn-trade-cal":
        summary = mirror_tushare_cn_trade_cal(
            out=args.out,
            start_date=args.start_date,
            end_date=args.end_date,
            exchange=args.exchange,
            token_env=args.token_env,
        )
    else:
        commands = {
            "mirror-cn-daily": mirror_tushare_cn_daily,
            "mirror-cn-adj-factor": mirror_tushare_cn_adj_factor,
            "mirror-cn-daily-basic": mirror_tushare_cn_daily_basic,
            "mirror-cn-stk-limit": mirror_tushare_cn_limit_status,
            "mirror-cn-limit-status": mirror_tushare_cn_limit_status,
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "paths":
        return _handle_paths(args)
    if args.command == "contract" and args.contract_command == "build":
        return _handle_contract_build(args)
    if args.command == "registry" and args.registry_command == "build":
        return _handle_registry_build(args)
    if args.command == "rqdata":
        return _handle_rqdata(args)
    if args.command == "tushare":
        return _handle_tushare(args)
    parser.error(f"Unknown command: {args.command}")
    return 2
