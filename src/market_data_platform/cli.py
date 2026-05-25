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
    resolve_artifacts_root,
)
from market_data_platform.registry import (
    render_combined_dataset_registry_csv,
    write_combined_dataset_registry,
)
from market_data_platform.rqdata_cn import export_cn_instruments, mirror_cn_daily

MARKET_CHOICES = tuple(market for market in ("hk", "cn") if market in SUPPORTED_MARKETS)
REGISTRY_MARKET_CHOICES = ("all", *MARKET_CHOICES)


def _add_paths_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("paths", help="Print shared data platform paths.")
    parser.add_argument("--artifacts-root")
    parser.add_argument("--market", default="hk", choices=MARKET_CHOICES)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketdata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_paths_parser(subparsers)
    _add_contract_parser(subparsers)
    _add_registry_parser(subparsers)
    _add_rqdata_parser(subparsers)
    return parser


def _handle_paths(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    payload = {
        "artifacts_root": str(root),
        "market": args.market,
        "current_contract": str(current_contract_path(root, market=args.market)),
        "dataset_registry": str(dataset_registry_path(root)),
        "assets": {
            key: str(path)
            for key, path in candidate_asset_paths(root, market=args.market).items()
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"artifacts_root: {payload['artifacts_root']}")
        print(f"market: {payload['market']}")
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
    parser.error(f"Unknown command: {args.command}")
    return 2
