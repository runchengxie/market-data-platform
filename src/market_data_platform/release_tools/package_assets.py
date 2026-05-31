#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from market_data_platform.current_assets import (
    current_contract_entry,
    default_hk_current_contract_path,
    load_current_contract,
)
from market_data_platform.paths import resolve_artifacts_root
from market_data_platform.repo_paths import find_repo_root
from market_data_platform.repo_paths import resolve_repo_path as resolve_repo_relative_path

REPO_ROOT = find_repo_root(__file__)
ASSETS_ROOT = resolve_artifacts_root() / "assets"
AVAILABLE_PART_CHOICES = (
    "daily",
    "intraday",
    "etf",
    "valuation",
    "instruments",
    "pit",
    "reference",
    "exchange_rate",
    "southbound",
    "financial_details",
    "announcement",
    "industry",
    "universe",
)
DEFAULT_PART_CHOICES = tuple(
    part_name for part_name in AVAILABLE_PART_CHOICES if part_name != "announcement"
)
RELEASE_PRESETS_DIR = REPO_ROOT / "configs" / "presets" / "release"


def _normalize_default_parts(value: object, *, source: Path) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_PART_CHOICES
    if isinstance(value, str) or not isinstance(value, list | tuple):
        raise SystemExit(f"Release preset default_parts must be a list: {source}")
    parts = tuple(str(item).strip() for item in value if str(item).strip())
    invalid = [part for part in parts if part not in AVAILABLE_PART_CHOICES]
    if invalid:
        raise SystemExit(
            f"Release preset {source} has unsupported default_parts: {', '.join(invalid)}"
        )
    return tuple(dict.fromkeys(parts))


def _load_release_preset(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SystemExit(f"Release preset must be a mapping: {path}")
    preset = {str(key): value for key, value in payload.items()}
    preset["default_parts"] = _normalize_default_parts(
        preset.get("default_parts"),
        source=path,
    )
    for key in ("daily_snapshot", "instruments_file"):
        if not str(preset.get(key) or "").strip():
            raise SystemExit(f"Release preset {path} must define {key}.")
    return preset


def load_release_presets(presets_dir: Path = RELEASE_PRESETS_DIR) -> dict[str, dict[str, object]]:
    paths = sorted(presets_dir.glob("*.yml")) + sorted(presets_dir.glob("*.yaml"))
    if not paths:
        raise SystemExit(f"No release preset files found under: {presets_dir}")
    return {path.stem: _load_release_preset(path) for path in paths}


PRESETS = load_release_presets()


def resolve_repo_path(path_text: str | Path) -> Path:
    return resolve_repo_relative_path(path_text, repo_root=REPO_ROOT)


def looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.startswith(".") or value.startswith("~")


def resolve_snapshot_path(base: Path, value: str) -> Path:
    path = resolve_repo_path(value) if looks_like_path(value) else base / value
    return path.resolve() if path.exists() else path


def detect_as_of(text: str) -> str:
    match = re.search(r"(\d{8})", text)
    if match:
        return match.group(1)
    return datetime.now().strftime("%Y%m%d")


def ensure_dest_root(dest: Path, overwrite: bool, *, dry_run: bool) -> None:
    if dest.exists():
        if not overwrite and any(dest.iterdir()):
            raise SystemExit(f"Destination exists and is not empty: {dest}")
        if overwrite and not dry_run:
            shutil.rmtree(dest)
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)


def ensure_exists(path: Path, kind: str) -> None:
    if not path.exists():
        raise SystemExit(f"{kind} not found: {path}")


def create_relative_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    rel_target = os.path.relpath(target, start=link.parent)
    os.symlink(rel_target, link, target_is_directory=target.is_dir())


def copy_dir(src: Path, dest: Path, mode: str, dry_run: bool) -> None:
    if dry_run:
        return
    if mode == "symlink":
        create_relative_symlink(src, dest)
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True)


def copy_file(src: Path, dest: Path, mode: str, dry_run: bool) -> None:
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "symlink":
        create_relative_symlink(src, dest)
    else:
        shutil.copy2(src, dest)


def _entry_kind(path: Path) -> str:
    return "directory" if path.is_dir() else "file"


def _part_entry(label: str, source: Path, target: str) -> dict:
    return {
        "label": label,
        "source": str(source),
        "target": target,
        "kind": _entry_kind(source),
    }


def _latest_link(link: str, target: str) -> dict:
    return {"link": link, "target": target}


def _hk_current_contract_overrides(args: argparse.Namespace) -> tuple[dict[str, str], Path | None]:
    if args.preset != "hk_current":
        return {}, None
    contract_path = default_hk_current_contract_path(ASSETS_ROOT.parent)
    contract = load_current_contract(contract_path)
    if not isinstance(contract, dict):
        return {}, None
    mapping = {
        "daily_snapshot": ("daily_clean",),
        "intraday_snapshot": ("intraday",),
        "etf_daily_snapshot": ("etf_daily_clean", "etf_daily"),
        "etf_instruments_file": ("etf_instruments",),
        "valuation_snapshot": ("valuation",),
        "instruments_file": ("instruments",),
        "pit_snapshot": ("pit",),
        "ex_factors_snapshot": ("ex_factors",),
        "dividends_snapshot": ("dividends",),
        "shares_snapshot": ("shares",),
        "exchange_rate_snapshot": ("exchange_rate",),
        "southbound_snapshot": ("southbound",),
        "financial_details_snapshot": ("financial_details",),
        "industry_changes_snapshot": ("industry_changes",),
        "universe_by_date": ("universe_by_date",),
        "universe_symbols": ("universe_symbols",),
        "universe_meta": ("universe_meta",),
    }
    overrides: dict[str, str] = {}
    for arg_name, asset_keys in mapping.items():
        if getattr(args, arg_name, None) is not None:
            continue
        for asset_key in asset_keys:
            entry = current_contract_entry(contract, asset_key)
            if not isinstance(entry, dict):
                continue
            if entry.get("exists") is not True:
                continue
            resolved_path = str(entry.get("resolved_path") or "").strip()
            if resolved_path:
                overrides[arg_name] = resolved_path
                break
    return overrides, contract_path


def _validate_resolved_asset_paths(requirements: Mapping[str, Path | None]) -> None:
    for kind, path in requirements.items():
        if path:
            ensure_exists(path, kind)


def _resolve_assets(args: argparse.Namespace) -> dict[str, object]:
    preset = PRESETS[args.preset]
    current_overrides, current_contract_path = _hk_current_contract_overrides(args)
    daily_snapshot = (
        args.daily_snapshot or current_overrides.get("daily_snapshot") or preset["daily_snapshot"]
    )
    intraday_snapshot = (
        None
        if args.no_intraday
        else (
            args.intraday_snapshot
            or current_overrides.get("intraday_snapshot")
            or preset.get("intraday_snapshot")
        )
    )
    etf_daily_snapshot = (
        None
        if args.no_etf
        else (
            args.etf_daily_snapshot
            or current_overrides.get("etf_daily_snapshot")
            or preset.get("etf_daily_snapshot")
        )
    )
    if (
        etf_daily_snapshot == "hk_etf_daily_clean_latest"
        and not args.etf_daily_snapshot
        and not resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "daily",
            etf_daily_snapshot,
        ).exists()
        and resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "daily",
            "hk_etf_daily_latest",
        ).exists()
    ):
        etf_daily_snapshot = "hk_etf_daily_latest"
    etf_instruments_file = (
        None
        if args.no_etf
        else (
            args.etf_instruments_file
            or current_overrides.get("etf_instruments_file")
            or preset.get("etf_instruments_file")
        )
    )
    valuation_snapshot = (
        None
        if args.no_valuation
        else (
            args.valuation_snapshot
            or current_overrides.get("valuation_snapshot")
            or preset.get("valuation_snapshot")
        )
    )
    instruments_file = (
        args.instruments_file
        or current_overrides.get("instruments_file")
        or preset["instruments_file"]
    )
    pit_snapshot = (
        None
        if args.no_pit
        else (args.pit_snapshot or current_overrides.get("pit_snapshot") or preset["pit_snapshot"])
    )
    if args.no_reference:
        ex_factors_snapshot = None
        dividends_snapshot = None
        shares_snapshot = None
    else:
        ex_factors_snapshot = (
            args.ex_factors_snapshot
            or current_overrides.get("ex_factors_snapshot")
            or preset.get("ex_factors_snapshot")
        )
        dividends_snapshot = (
            args.dividends_snapshot
            or current_overrides.get("dividends_snapshot")
            or preset.get("dividends_snapshot")
        )
        shares_snapshot = (
            args.shares_snapshot
            or current_overrides.get("shares_snapshot")
            or preset.get("shares_snapshot")
        )
    exchange_rate_snapshot = (
        None
        if args.no_exchange_rate
        else (
            args.exchange_rate_snapshot
            or current_overrides.get("exchange_rate_snapshot")
            or preset.get("exchange_rate_snapshot")
        )
    )
    southbound_snapshot = (
        None
        if args.no_southbound
        else (
            args.southbound_snapshot
            or current_overrides.get("southbound_snapshot")
            or preset.get("southbound_snapshot")
        )
    )
    financial_details_snapshot = (
        None
        if args.no_financial_details
        else (
            args.financial_details_snapshot
            or current_overrides.get("financial_details_snapshot")
            or preset.get("financial_details_snapshot")
        )
    )
    announcement_snapshot = (
        None
        if args.no_announcement
        else (args.announcement_snapshot or preset.get("announcement_snapshot"))
    )
    industry_changes_snapshot = (
        None
        if args.no_industry
        else (
            args.industry_changes_snapshot
            or current_overrides.get("industry_changes_snapshot")
            or preset.get("industry_changes_snapshot")
        )
    )
    universe_by_date = (
        args.universe_by_date
        if args.universe_by_date is not None
        else current_overrides.get("universe_by_date") or preset.get("universe_by_date")
    )
    universe_symbols = (
        args.universe_symbols
        if args.universe_symbols is not None
        else current_overrides.get("universe_symbols") or preset.get("universe_symbols")
    )
    universe_meta = (
        args.universe_meta
        if args.universe_meta is not None
        else current_overrides.get("universe_meta") or preset.get("universe_meta")
    )

    daily_dir = resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "daily", daily_snapshot)
    valuation_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "valuation", valuation_snapshot)
        if valuation_snapshot
        else None
    )
    instruments_path = resolve_snapshot_path(
        ASSETS_ROOT / "rqdata" / "hk" / "instruments",
        instruments_file,
    )
    pit_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "pit_financials", pit_snapshot)
        if pit_snapshot
        else None
    )
    ex_factors_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "ex_factors", ex_factors_snapshot)
        if ex_factors_snapshot
        else None
    )
    dividends_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "dividends", dividends_snapshot)
        if dividends_snapshot
        else None
    )
    shares_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "shares", shares_snapshot)
        if shares_snapshot
        else None
    )
    exchange_rate_dir = (
        resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "exchange_rate",
            exchange_rate_snapshot,
        )
        if exchange_rate_snapshot
        else None
    )
    southbound_dir = (
        resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "southbound",
            southbound_snapshot,
        )
        if southbound_snapshot
        else None
    )
    financial_details_dir = (
        resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "financial_details",
            financial_details_snapshot,
        )
        if financial_details_snapshot
        else None
    )
    announcement_dir = (
        resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "announcement",
            announcement_snapshot,
        )
        if announcement_snapshot
        else None
    )
    industry_changes_dir = (
        resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "industry_changes",
            industry_changes_snapshot,
        )
        if industry_changes_snapshot
        else None
    )
    universe_root = ASSETS_ROOT / "universe"
    universe_by_date_path = (
        resolve_snapshot_path(universe_root, universe_by_date) if universe_by_date else None
    )
    universe_symbols_path = (
        resolve_snapshot_path(universe_root, universe_symbols) if universe_symbols else None
    )
    universe_meta_path = (
        resolve_snapshot_path(universe_root, universe_meta) if universe_meta else None
    )
    intraday_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "intraday", intraday_snapshot)
        if intraday_snapshot
        else None
    )
    etf_daily_dir = (
        resolve_snapshot_path(ASSETS_ROOT / "rqdata" / "hk" / "daily", etf_daily_snapshot)
        if etf_daily_snapshot
        else None
    )
    etf_instruments_path = (
        resolve_snapshot_path(
            ASSETS_ROOT / "rqdata" / "hk" / "instruments",
            etf_instruments_file,
        )
        if etf_instruments_file
        else None
    )

    if bool(universe_by_date_path) != bool(universe_symbols_path):
        raise SystemExit(
            "Universe part requires both --universe-by-date and --universe-symbols. "
            "Provide both, or leave both unset."
        )
    if bool(etf_daily_dir) != bool(etf_instruments_path):
        raise SystemExit(
            "ETF part requires both --etf-daily-snapshot and --etf-instruments-file. "
            "Provide both, or leave both unset."
        )

    _validate_resolved_asset_paths(
        {
            "Daily snapshot directory": daily_dir,
            "Intraday snapshot directory": intraday_dir,
            "ETF daily snapshot directory": etf_daily_dir,
            "ETF instruments file": etf_instruments_path,
            "Valuation snapshot directory": valuation_dir,
            "Instruments file": instruments_path,
            "PIT snapshot directory": pit_dir,
            "Ex-factors snapshot directory": ex_factors_dir,
            "Dividends snapshot directory": dividends_dir,
            "Shares snapshot directory": shares_dir,
            "Exchange-rate snapshot directory": exchange_rate_dir,
            "Southbound snapshot directory": southbound_dir,
            "Financial-details snapshot directory": financial_details_dir,
            "Announcement snapshot directory": announcement_dir,
            "Industry changes snapshot directory": industry_changes_dir,
            "Universe by-date file": universe_by_date_path,
            "Universe symbols file": universe_symbols_path,
        }
    )
    if universe_meta_path and not universe_meta_path.exists():
        universe_meta_path = None

    return {
        "daily_dir": daily_dir,
        "intraday_dir": intraday_dir,
        "etf_daily_dir": etf_daily_dir,
        "etf_instruments_path": etf_instruments_path,
        "valuation_dir": valuation_dir,
        "instruments_path": instruments_path,
        "pit_dir": pit_dir,
        "ex_factors_dir": ex_factors_dir,
        "dividends_dir": dividends_dir,
        "shares_dir": shares_dir,
        "exchange_rate_dir": exchange_rate_dir,
        "southbound_dir": southbound_dir,
        "financial_details_dir": financial_details_dir,
        "announcement_dir": announcement_dir,
        "industry_changes_dir": industry_changes_dir,
        "universe_by_date_path": universe_by_date_path,
        "universe_symbols_path": universe_symbols_path,
        "universe_meta_path": universe_meta_path,
        "current_contract_path": current_contract_path,
    }


def _add_universe_meta_part(parts: dict[str, dict], universe_meta_path: Path | None) -> None:
    universe_part = parts.get("universe")
    if not universe_meta_path or not universe_part:
        return
    universe_part["entries"].append(
        _part_entry(
            "meta",
            universe_meta_path,
            f"universe/{universe_meta_path.name}",
        )
    )
    universe_part["latest_links"].append(
        _latest_link(
            "universe/latest_meta.yml",
            f"universe/{universe_meta_path.name}",
        )
    )
    universe_part["summary"]["meta"] = universe_meta_path.name


def _build_reference_part(
    *,
    ex_factors_dir: Path | None,
    dividends_dir: Path | None,
    shares_dir: Path | None,
) -> dict | None:
    reference_entries: list[dict] = []
    reference_links: list[dict] = []
    reference_summary: dict[str, str] = {}
    if ex_factors_dir:
        reference_entries.append(
            _part_entry(
                "ex_factors",
                ex_factors_dir,
                f"rqdata/hk/ex_factors/{ex_factors_dir.name}",
            )
        )
        reference_links.append(
            _latest_link(
                "rqdata/hk/ex_factors/latest",
                f"rqdata/hk/ex_factors/{ex_factors_dir.name}",
            )
        )
        reference_summary["ex_factors_snapshot"] = ex_factors_dir.name
    if dividends_dir:
        reference_entries.append(
            _part_entry(
                "dividends",
                dividends_dir,
                f"rqdata/hk/dividends/{dividends_dir.name}",
            )
        )
        reference_links.append(
            _latest_link(
                "rqdata/hk/dividends/latest",
                f"rqdata/hk/dividends/{dividends_dir.name}",
            )
        )
        reference_summary["dividends_snapshot"] = dividends_dir.name
    if shares_dir:
        reference_entries.append(
            _part_entry(
                "shares",
                shares_dir,
                f"rqdata/hk/shares/{shares_dir.name}",
            )
        )
        reference_links.append(
            _latest_link(
                "rqdata/hk/shares/latest",
                f"rqdata/hk/shares/{shares_dir.name}",
            )
        )
        reference_summary["shares_snapshot"] = shares_dir.name
    if not reference_entries:
        return None
    return {
        "description": "Reference snapshots: ex-factors, dividends, shares.",
        "entries": reference_entries,
        "latest_links": reference_links,
        "summary": reference_summary,
    }


def _build_part_specs(resolved: dict[str, object]) -> dict[str, dict]:
    daily_dir = resolved["daily_dir"]
    intraday_dir = resolved["intraday_dir"]
    etf_daily_dir = resolved["etf_daily_dir"]
    etf_instruments_path = resolved["etf_instruments_path"]
    valuation_dir = resolved["valuation_dir"]
    instruments_path = resolved["instruments_path"]
    pit_dir = resolved["pit_dir"]
    ex_factors_dir = resolved["ex_factors_dir"]
    dividends_dir = resolved["dividends_dir"]
    shares_dir = resolved["shares_dir"]
    exchange_rate_dir = resolved["exchange_rate_dir"]
    southbound_dir = resolved["southbound_dir"]
    financial_details_dir = resolved["financial_details_dir"]
    announcement_dir = resolved["announcement_dir"]
    industry_changes_dir = resolved["industry_changes_dir"]
    universe_by_date_path = resolved["universe_by_date_path"]
    universe_symbols_path = resolved["universe_symbols_path"]
    universe_meta_path = resolved["universe_meta_path"]

    parts = {
        "daily": {
            "description": "HK daily snapshot directory.",
            "entries": [
                _part_entry(
                    "daily",
                    daily_dir,
                    f"rqdata/hk/daily/{daily_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/daily/latest",
                    f"rqdata/hk/daily/{daily_dir.name}",
                )
            ],
            "summary": {"snapshot": daily_dir.name},
        },
        "intraday": {
            "description": "HK intraday 5m snapshot directory.",
            "entries": [
                _part_entry(
                    "intraday",
                    intraday_dir,
                    f"rqdata/hk/intraday/{intraday_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/intraday/hk_intraday_latest",
                    f"rqdata/hk/intraday/{intraday_dir.name}",
                )
            ],
            "summary": {"snapshot": intraday_dir.name},
        }
        if intraday_dir
        else None,
        "etf": {
            "description": "HK ETF daily snapshot plus ETF instruments parquet.",
            "entries": [
                _part_entry(
                    "etf_daily",
                    etf_daily_dir,
                    f"rqdata/hk/daily/{etf_daily_dir.name}",
                ),
                _part_entry(
                    "etf_instruments",
                    etf_instruments_path,
                    f"rqdata/hk/instruments/{etf_instruments_path.name}",
                ),
            ],
            "latest_links": [
                *(
                    [
                        _latest_link(
                            "rqdata/hk/daily/hk_etf_daily_clean_latest",
                            f"rqdata/hk/daily/{etf_daily_dir.name}",
                        )
                    ]
                    if "clean" in etf_daily_dir.name
                    else []
                ),
                _latest_link(
                    "rqdata/hk/daily/hk_etf_daily_latest",
                    f"rqdata/hk/daily/{etf_daily_dir.name}",
                ),
                _latest_link(
                    "rqdata/hk/instruments/hk_etf_instruments_latest.parquet",
                    f"rqdata/hk/instruments/{etf_instruments_path.name}",
                ),
            ],
            "summary": {
                "daily_snapshot": etf_daily_dir.name,
                "instruments_file": etf_instruments_path.name,
            },
        }
        if etf_daily_dir and etf_instruments_path
        else None,
        "valuation": {
            "description": "HK valuation snapshot directory.",
            "entries": [
                _part_entry(
                    "valuation",
                    valuation_dir,
                    f"rqdata/hk/valuation/{valuation_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/valuation/latest",
                    f"rqdata/hk/valuation/{valuation_dir.name}",
                )
            ],
            "summary": {"snapshot": valuation_dir.name},
        }
        if valuation_dir
        else None,
        "instruments": {
            "description": "HK instruments parquet.",
            "entries": [
                _part_entry(
                    "instruments",
                    instruments_path,
                    f"rqdata/hk/instruments/{instruments_path.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/instruments/latest.parquet",
                    f"rqdata/hk/instruments/{instruments_path.name}",
                )
            ],
            "summary": {"file": instruments_path.name},
        },
        "universe": (
            {
                "description": "Universe membership and symbol files.",
                "entries": [
                    _part_entry(
                        "by_date",
                        universe_by_date_path,
                        f"universe/{universe_by_date_path.name}",
                    ),
                    _part_entry(
                        "symbols",
                        universe_symbols_path,
                        f"universe/{universe_symbols_path.name}",
                    ),
                ],
                "latest_links": [
                    _latest_link(
                        "universe/latest_by_date.csv",
                        f"universe/{universe_by_date_path.name}",
                    ),
                    _latest_link(
                        "universe/latest_symbols.txt",
                        f"universe/{universe_symbols_path.name}",
                    ),
                ],
                "summary": {
                    "by_date": universe_by_date_path.name,
                    "symbols": universe_symbols_path.name,
                },
            }
            if universe_by_date_path and universe_symbols_path
            else None
        ),
    }
    _add_universe_meta_part(parts, universe_meta_path)

    parts = {name: spec for name, spec in parts.items() if spec is not None}

    if pit_dir:
        parts["pit"] = {
            "description": "PIT fundamentals snapshot directory.",
            "entries": [
                _part_entry(
                    "pit_financials",
                    pit_dir,
                    f"rqdata/hk/pit_financials/{pit_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/pit_financials/latest",
                    f"rqdata/hk/pit_financials/{pit_dir.name}",
                )
            ],
            "summary": {"snapshot": pit_dir.name},
        }

    reference_part = _build_reference_part(
        ex_factors_dir=ex_factors_dir,
        dividends_dir=dividends_dir,
        shares_dir=shares_dir,
    )
    if reference_part is not None:
        parts["reference"] = reference_part

    if exchange_rate_dir:
        parts["exchange_rate"] = {
            "description": "Exchange-rate snapshot directory.",
            "entries": [
                _part_entry(
                    "exchange_rate",
                    exchange_rate_dir,
                    f"rqdata/hk/exchange_rate/{exchange_rate_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/exchange_rate/latest",
                    f"rqdata/hk/exchange_rate/{exchange_rate_dir.name}",
                )
            ],
            "summary": {"snapshot": exchange_rate_dir.name},
        }

    if southbound_dir:
        parts["southbound"] = {
            "description": "Southbound eligibility snapshot directory.",
            "entries": [
                _part_entry(
                    "southbound",
                    southbound_dir,
                    f"rqdata/hk/southbound/{southbound_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/southbound/latest",
                    f"rqdata/hk/southbound/{southbound_dir.name}",
                )
            ],
            "summary": {"snapshot": southbound_dir.name},
        }

    if financial_details_dir:
        parts["financial_details"] = {
            "description": "Financial-details raw snapshot directory.",
            "entries": [
                _part_entry(
                    "financial_details",
                    financial_details_dir,
                    f"rqdata/hk/financial_details/{financial_details_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/financial_details/latest",
                    f"rqdata/hk/financial_details/{financial_details_dir.name}",
                )
            ],
            "summary": {"snapshot": financial_details_dir.name},
        }

    if announcement_dir:
        parts["announcement"] = {
            "description": "Announcement raw snapshot directory.",
            "entries": [
                _part_entry(
                    "announcement",
                    announcement_dir,
                    f"rqdata/hk/announcement/{announcement_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/announcement/latest",
                    f"rqdata/hk/announcement/{announcement_dir.name}",
                )
            ],
            "summary": {"snapshot": announcement_dir.name},
        }

    if industry_changes_dir:
        parts["industry"] = {
            "description": "Industry changes snapshot directory.",
            "entries": [
                _part_entry(
                    "industry_changes",
                    industry_changes_dir,
                    f"rqdata/hk/industry_changes/{industry_changes_dir.name}",
                )
            ],
            "latest_links": [
                _latest_link(
                    "rqdata/hk/industry_changes/latest",
                    f"rqdata/hk/industry_changes/{industry_changes_dir.name}",
                )
            ],
            "summary": {"snapshot": industry_changes_dir.name},
        }

    return parts


def _selected_parts(
    requested_parts: list[str],
    available_parts: dict[str, dict],
    *,
    default_parts: tuple[str, ...],
) -> list[str]:
    if requested_parts:
        selected = list(dict.fromkeys(requested_parts))
    else:
        selected = [part for part in default_parts if part in available_parts]
    missing = [part for part in selected if part not in available_parts]
    if missing:
        raise SystemExit(
            f"Requested parts are not available under the current preset/settings: {missing}"
        )
    return selected


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _copy_entry_to_part(entry: dict, part_dir: Path, mode: str, dry_run: bool) -> None:
    src = Path(entry["source"])
    out = part_dir / str(entry["target"])
    if src.is_dir():
        copy_dir(src, out, mode, dry_run)
    else:
        copy_file(src, out, mode, dry_run)


def _build_root_manifest(
    *,
    name: str,
    as_of: str,
    mode: str,
    preset: str,
    generated_at: str,
    selected_parts: list[str],
    part_specs: dict[str, dict],
    current_contract_path: Path | None,
) -> dict:
    payload = {
        "distribution": {
            "name": name,
            "as_of": as_of,
            "generated_at": generated_at,
            "source_repo": str(REPO_ROOT),
            "mode": mode,
            "preset": preset,
            "current_contract_path": (
                str(current_contract_path) if current_contract_path is not None else None
            ),
        },
        "parts": {},
    }
    for part_name in selected_parts:
        spec = part_specs[part_name]
        payload["parts"][part_name] = {
            "path": part_name,
            "description": spec["description"],
            "entries": spec["entries"],
            "latest_links": spec["latest_links"],
            "summary": spec["summary"],
        }
    return payload


def _build_part_manifest(
    *,
    name: str,
    as_of: str,
    mode: str,
    preset: str,
    generated_at: str,
    part_name: str,
    spec: dict,
    current_contract_path: Path | None,
) -> dict:
    return {
        "distribution": {
            "name": name,
            "as_of": as_of,
            "generated_at": generated_at,
            "source_repo": str(REPO_ROOT),
            "mode": mode,
            "preset": preset,
            "current_contract_path": (
                str(current_contract_path) if current_contract_path is not None else None
            ),
        },
        "part": {
            "name": part_name,
            "description": spec["description"],
            "entries": spec["entries"],
            "latest_links": spec["latest_links"],
            "summary": spec["summary"],
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage HK assets into multiple release parts.",
    )
    parser.add_argument("--preset", choices=sorted(PRESETS.keys()), default="hk_full")
    parser.add_argument(
        "--name",
        default=None,
        help="Distribution name used in manifests and tarballs.",
    )
    parser.add_argument("--dest", default=None, help="Destination staging root.")
    parser.add_argument("--mode", choices=["copy", "symlink"], default="copy")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite destination.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--part",
        action="append",
        choices=AVAILABLE_PART_CHOICES,
        default=[],
        help="Only stage selected part(s). Repeatable.",
    )
    parser.add_argument("--no-pit", action="store_true", help="Skip PIT assets.")
    parser.add_argument(
        "--no-reference",
        action="store_true",
        help="Skip reference assets (ex_factors/dividends/shares).",
    )
    parser.add_argument("--no-valuation", action="store_true", help="Skip valuation assets.")
    parser.add_argument("--as-of", dest="as_of", default=None)
    parser.add_argument("--daily-snapshot", default=None)
    parser.add_argument("--intraday-snapshot", default=None)
    parser.add_argument("--etf-daily-snapshot", default=None)
    parser.add_argument("--etf-instruments-file", default=None)
    parser.add_argument("--valuation-snapshot", default=None)
    parser.add_argument("--instruments-file", default=None)
    parser.add_argument("--pit-snapshot", default=None)
    parser.add_argument("--ex-factors-snapshot", default=None)
    parser.add_argument("--dividends-snapshot", default=None)
    parser.add_argument("--shares-snapshot", default=None)
    parser.add_argument("--exchange-rate-snapshot", default=None)
    parser.add_argument("--southbound-snapshot", default=None)
    parser.add_argument("--financial-details-snapshot", default=None)
    parser.add_argument("--announcement-snapshot", default=None)
    parser.add_argument("--industry-changes-snapshot", default=None)
    parser.add_argument("--universe-by-date", default=None)
    parser.add_argument("--universe-symbols", default=None)
    parser.add_argument("--universe-meta", default=None)
    parser.add_argument(
        "--no-exchange-rate",
        action="store_true",
        help="Skip exchange_rate assets.",
    )
    parser.add_argument("--no-southbound", action="store_true", help="Skip southbound assets.")
    parser.add_argument("--no-intraday", action="store_true", help="Skip intraday assets.")
    parser.add_argument(
        "--no-etf",
        action="store_true",
        help="Skip ETF daily + ETF instruments assets.",
    )
    parser.add_argument(
        "--no-financial-details",
        action="store_true",
        help="Skip financial_details assets.",
    )
    parser.add_argument("--no-announcement", action="store_true", help="Skip announcement assets.")
    parser.add_argument("--no-industry", action="store_true", help="Skip industry_changes assets.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    preset = PRESETS[args.preset]
    default_parts = tuple(preset.get("default_parts") or DEFAULT_PART_CHOICES)
    resolved = _resolve_assets(args)
    daily_dir = resolved["daily_dir"]
    as_of = args.as_of or detect_as_of(daily_dir.name)
    distribution_name = args.name or args.preset
    dest = resolve_repo_path(
        args.dest or (REPO_ROOT.parent / "marketdata_asset_parts" / f"{distribution_name}_{as_of}")
    )
    ensure_dest_root(dest, args.overwrite, dry_run=args.dry_run)

    part_specs = _build_part_specs(resolved)
    selected_parts = _selected_parts(args.part, part_specs, default_parts=default_parts)
    generated_at = datetime.now(UTC).astimezone().isoformat(timespec="seconds")

    for part_name in selected_parts:
        spec = part_specs[part_name]
        part_dir = dest / part_name
        if not args.dry_run:
            part_dir.mkdir(parents=True, exist_ok=True)
        for entry in spec["entries"]:
            _copy_entry_to_part(entry, part_dir, args.mode, args.dry_run)
        if not args.dry_run:
            for link_spec in spec["latest_links"]:
                create_relative_symlink(
                    part_dir / str(link_spec["target"]),
                    part_dir / str(link_spec["link"]),
                )
            _write_yaml(
                part_dir / "manifest.yml",
                _build_part_manifest(
                    name=distribution_name,
                    as_of=as_of,
                    mode=args.mode,
                    preset=args.preset,
                    generated_at=generated_at,
                    part_name=part_name,
                    spec=spec,
                    current_contract_path=resolved.get("current_contract_path"),
                ),
            )

    if not args.dry_run:
        _write_yaml(
            dest / "manifest.yml",
            _build_root_manifest(
                name=distribution_name,
                as_of=as_of,
                mode=args.mode,
                preset=args.preset,
                generated_at=generated_at,
                selected_parts=selected_parts,
                part_specs=part_specs,
                current_contract_path=resolved.get("current_contract_path"),
            ),
        )

    print(f"Staged asset parts at: {dest}")
    for part_name in selected_parts:
        print(f"Part {part_name}: {dest / part_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
