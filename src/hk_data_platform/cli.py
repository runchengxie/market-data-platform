from __future__ import annotations

import argparse
import json
from pathlib import Path

from hk_data_platform.contract import build_current_contract, write_current_contract
from hk_data_platform.paths import (
    candidate_asset_paths,
    current_contract_path,
    dataset_registry_path,
    resolve_artifacts_root,
)
from hk_data_platform.registry import render_dataset_registry_csv, write_dataset_registry


def _add_paths_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("paths", help="Print shared HK data platform paths.")
    parser.add_argument("--artifacts-root")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def _add_contract_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("contract", help="Current contract helpers.")
    contract_subparsers = parser.add_subparsers(dest="contract_command", required=True)
    build = contract_subparsers.add_parser(
        "build",
        help="Build hk_current.json from standard paths.",
    )
    build.add_argument("--artifacts-root")
    build.add_argument("--target-date")
    build.add_argument("--generated-by", default="hkdata contract build")
    build.add_argument(
        "--out",
        help="Default: <artifacts-root>/metadata/current_assets/hk_current.json",
    )
    build.add_argument(
        "--registry-out",
        help="Default: <artifacts-root>/metadata/dataset_registry.csv",
    )
    build.add_argument(
        "--no-registry",
        action="store_true",
        help="Only write hk_current.json; skip dataset_registry.csv.",
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
        help="Build dataset_registry.csv from hk_current.json.",
    )
    build.add_argument("--artifacts-root")
    build.add_argument(
        "--contract",
        help="Default: <artifacts-root>/metadata/current_assets/hk_current.json",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hkdata")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_paths_parser(subparsers)
    _add_contract_parser(subparsers)
    _add_registry_parser(subparsers)
    return parser


def _handle_paths(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    payload = {
        "artifacts_root": str(root),
        "current_contract": str(current_contract_path(root)),
        "dataset_registry": str(dataset_registry_path(root)),
        "assets": {key: str(path) for key, path in candidate_asset_paths(root).items()},
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"artifacts_root: {payload['artifacts_root']}")
        print(f"current_contract: {payload['current_contract']}")
        print(f"dataset_registry: {payload['dataset_registry']}")
        for key, path in payload["assets"].items():
            print(f"{key}: {path}")
    return 0


def _handle_contract_build(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    output = current_contract_path(root)
    if args.out is not None:
        output = Path(args.out).expanduser().resolve()
    registry_output = dataset_registry_path(root)
    if args.registry_out is not None:
        registry_output = Path(args.registry_out).expanduser().resolve()
    payload = build_current_contract(
        root,
        generated_by=args.generated_by,
        target_date=args.target_date,
    )
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    write_current_contract(output, payload)
    print(f"current_contract: {output}")
    if not args.no_registry:
        write_dataset_registry(registry_output, payload)
        print(f"dataset_registry: {registry_output}")
    return 0


def _load_contract(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Contract is not a JSON object: {path}")
    return payload


def _handle_registry_build(args: argparse.Namespace) -> int:
    root = resolve_artifacts_root(args.artifacts_root)
    contract_path = current_contract_path(root)
    if args.contract is not None:
        contract_path = Path(args.contract).expanduser().resolve()
    output = dataset_registry_path(root)
    if args.out is not None:
        output = Path(args.out).expanduser().resolve()
    payload = _load_contract(contract_path)
    if args.dry_run:
        print(render_dataset_registry_csv(payload), end="")
        return 0
    write_dataset_registry(output, payload)
    print(str(output))
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
    parser.error(f"Unknown command: {args.command}")
    return 2
