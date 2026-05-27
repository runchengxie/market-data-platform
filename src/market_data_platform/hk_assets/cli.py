from __future__ import annotations

import argparse
import json

from market_data_platform.hk_assets.command_registry import (
    RQDataAssetCommandSpec,
    rqdata_asset_command_specs,
)
from market_data_platform.rqdata_cli_common import (
    augment_quota_payload,
    format_quota_pretty,
    init_rqdatac,
)


def _add_rqdata_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Optional config path to load rqdata.init")
    parser.add_argument("--username", help="Override RQData username")
    parser.add_argument("--password", help="Override RQData password")


def _handle_info(args: argparse.Namespace) -> int:
    rqdatac = init_rqdatac(args)
    print(rqdatac.info())
    return 0


def _handle_quota(args: argparse.Namespace) -> int:
    rqdatac = init_rqdatac(args)
    quota = rqdatac.user.get_quota()
    payload = quota
    if hasattr(quota, "to_dict"):
        try:
            payload = quota.to_dict(orient="records")
        except TypeError:
            payload = quota.to_dict()
    payload = augment_quota_payload(payload)
    if args.pretty:
        print(format_quota_pretty(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_asset_command(args: argparse.Namespace, spec: RQDataAssetCommandSpec) -> int:
    from market_data_platform import hk_assets as hk_assets_tool

    runner = getattr(hk_assets_tool, spec.runner.__name__, spec.runner)
    if spec.requires_client:
        rqdatac = init_rqdatac(args)
        return int(runner(args, rqdatac) or 0)
    return int(runner(args) or 0)


def _make_asset_handler(spec: RQDataAssetCommandSpec):
    def _handler(args: argparse.Namespace) -> int:
        return _run_asset_command(args, spec)

    return _handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketdata rqdata hk-assets")
    subparsers = parser.add_subparsers(dest="command", required=True)

    info = subparsers.add_parser("info", help="Show rqdatac login/info")
    _add_rqdata_auth_args(info)
    info.set_defaults(func=_handle_info)

    quota = subparsers.add_parser("quota", help="Show rqdatac quota usage")
    _add_rqdata_auth_args(quota)
    quota.add_argument(
        "--pretty",
        action="store_true",
        help="Show human-friendly output with percent and progress bar",
    )
    quota.set_defaults(func=_handle_quota)

    for spec in rqdata_asset_command_specs():
        command = subparsers.add_parser(spec.name, help=spec.help)
        spec.add_args(command)
        command.set_defaults(func=_make_asset_handler(spec))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
