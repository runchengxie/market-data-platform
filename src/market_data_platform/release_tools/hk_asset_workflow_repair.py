from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .hk_asset_workflow_paths import SnapshotBundle, Step
from .hk_asset_workflow_report import REPAIR_SEVERITY_RANK


def normalize_repair_min_severity(value: str) -> str:
    text = str(value or "").strip().lower() or "warning"
    if text not in REPAIR_SEVERITY_RANK:
        raise SystemExit("--repair-min-severity must be one of: info, warning, error.")
    return text


def repair_candidate_passes_threshold(
    candidate: Mapping[str, Any],
    *,
    min_severity: str,
) -> bool:
    candidate_severity = str(candidate.get("max_severity") or "info").strip().lower() or "info"
    return REPAIR_SEVERITY_RANK.get(candidate_severity, -1) >= REPAIR_SEVERITY_RANK[min_severity]


def load_repair_source_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Repair source report not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Repair source report must be a JSON object: {path}")
    return payload


def repair_source_kind(*, only_unresolved: bool) -> str:
    return "remaining_repair_candidates" if only_unresolved else "repair_candidates"


def clone_candidate_list(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in items]


def repair_source_candidates(
    source_report: Mapping[str, Any],
    *,
    asset_name: str,
    only_unresolved: bool,
) -> tuple[list[dict[str, Any]], str]:
    inspect_assets = source_report.get("inspect", {}).get("assets")
    if not isinstance(inspect_assets, Mapping):
        raise SystemExit("Repair source report does not contain inspect.assets.")
    asset_payload = inspect_assets.get(asset_name)
    if not isinstance(asset_payload, Mapping):
        return [], repair_source_kind(only_unresolved=only_unresolved)

    if only_unresolved:
        remaining_assets = (
            source_report.get("repair", {}).get("remaining_candidates", {}).get("assets")
        )
        if isinstance(remaining_assets, Mapping):
            remaining_payload = remaining_assets.get(asset_name)
            if isinstance(remaining_payload, Mapping):
                candidates = remaining_payload.get("repair_candidates")
                if isinstance(candidates, list):
                    return clone_candidate_list(
                        [item for item in candidates if isinstance(item, Mapping)]
                    ), "repair.remaining_candidates"
        candidates = asset_payload.get("post_repair_repair_candidates")
        if isinstance(candidates, list):
            return clone_candidate_list(
                [item for item in candidates if isinstance(item, Mapping)]
            ), "inspect.post_repair_repair_candidates"
        return [], repair_source_kind(only_unresolved=True)

    candidates = asset_payload.get("repair_candidates")
    if not isinstance(candidates, list):
        return [], "inspect.repair_candidates"
    return clone_candidate_list(
        [item for item in candidates if isinstance(item, Mapping)]
    ), "inspect.repair_candidates"


def repair_unresolved_source_available(source_report: Mapping[str, Any]) -> bool:
    remaining_assets = source_report.get("repair", {}).get("remaining_candidates", {}).get("assets")
    if isinstance(remaining_assets, Mapping):
        return True
    inspect_assets = source_report.get("inspect", {}).get("assets")
    if not isinstance(inspect_assets, Mapping):
        return False
    return any(
        isinstance(asset_payload, Mapping)
        and isinstance(asset_payload.get("post_repair_repair_candidates"), list)
        for asset_payload in inspect_assets.values()
    )


def asset_path_from_bundle(bundle: SnapshotBundle, asset_name: str) -> Path:
    mapping = {
        "daily": bundle.daily_dir,
        "valuation": bundle.valuation_dir,
        "ex_factors": bundle.ex_factors_dir,
        "dividends": bundle.dividends_dir,
        "shares": bundle.shares_dir,
    }
    if asset_name not in mapping:
        raise SystemExit(f"Repair is not supported for asset: {asset_name}")
    return mapping[asset_name]


def repair_command_name(asset_name: str) -> str:
    mapping = {
        "daily": "mirror-hk-daily",
        "valuation": "mirror-hk-valuation",
        "ex_factors": "mirror-hk-ex-factors",
        "dividends": "mirror-hk-dividends",
        "shares": "mirror-hk-shares",
    }
    if asset_name not in mapping:
        raise SystemExit(f"Repair is not supported for asset: {asset_name}")
    return mapping[asset_name]


def repair_symbols_file_path(args: argparse.Namespace, *, asset_name: str) -> Path:
    return (
        args.reports_dir / "repair_inputs" / f"{asset_name}_{args.target_date}_repair_symbols.txt"
    )


def repair_patch_snapshot_path(refreshed_path: Path) -> Path:
    return refreshed_path.parent / f"{refreshed_path.name}__repair"


def candidate_window_bounds(candidate: Mapping[str, Any]) -> tuple[str | None, str | None]:
    trade_date = str(candidate.get("trade_date") or "").strip() or None
    start_date = str(candidate.get("start_date") or "").strip() or None
    end_date = str(candidate.get("end_date") or "").strip() or None
    return trade_date or start_date or end_date, trade_date or end_date or start_date


def write_repair_symbols_file(path: Path, *, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{symbol}\n" for symbol in symbols), encoding="utf-8")


def repair_candidates_from_steps(steps: list[Step]) -> dict[str, list[dict[str, Any]]]:
    assets: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        if step.phase != "repair" or not step.asset_name:
            continue
        metadata = step.report_metadata or {}
        if str(metadata.get("action") or "") != "patch_fetch":
            continue
        candidates = metadata.get("candidates")
        if not isinstance(candidates, list):
            continue
        assets[step.asset_name] = clone_candidate_list(
            [item for item in candidates if isinstance(item, Mapping)]
        )
    return assets


def build_repair_candidate_payload(
    *,
    args: argparse.Namespace,
    source_report: Path,
    source_kind: str,
    candidates_by_asset: Mapping[str, list[dict[str, Any]]],
    report_path: Path,
) -> dict[str, Any]:
    assets_payload: dict[str, Any] = {}
    total = 0
    for asset_name, candidates in candidates_by_asset.items():
        symbols = sorted(
            {
                str(item.get("symbol") or "").strip()
                for item in candidates
                if str(item.get("symbol") or "").strip()
            }
        )
        assets_payload[asset_name] = {
            "asset_name": asset_name,
            "candidate_count": len(candidates),
            "symbols": symbols,
            "repair_candidates": clone_candidate_list(candidates),
        }
        total += len(candidates)
    return {
        "target_date": args.target_date,
        "source_report": str(source_report),
        "source_kind": source_kind,
        "min_severity": args.repair_min_severity,
        "candidate_count": total,
        "assets": assets_payload,
        "report_path": str(report_path),
    }


def build_remaining_repair_candidates_payload(
    *,
    args: argparse.Namespace,
    workflow_report: Mapping[str, Any],
    report_path: Path,
) -> dict[str, Any] | None:
    inspect_assets = workflow_report.get("inspect", {}).get("assets")
    if not isinstance(inspect_assets, Mapping):
        return None
    assets_payload: dict[str, Any] = {}
    total = 0
    for asset_name, asset_payload in inspect_assets.items():
        if not isinstance(asset_payload, Mapping):
            continue
        latest_stage = str(asset_payload.get("latest_stage") or "").strip()
        if latest_stage != "post_repair":
            continue
        candidates = asset_payload.get("post_repair_repair_candidates")
        if not isinstance(candidates, list):
            continue
        cloned = clone_candidate_list([item for item in candidates if isinstance(item, Mapping)])
        symbols = sorted(
            {
                str(item.get("symbol") or "").strip()
                for item in cloned
                if str(item.get("symbol") or "").strip()
            }
        )
        assets_payload[str(asset_name)] = {
            "asset_name": str(asset_name),
            "candidate_count": len(cloned),
            "symbols": symbols,
            "repair_candidates": cloned,
        }
        total += len(cloned)
    return {
        "target_date": args.target_date,
        "source_report": str(args.workflow_report),
        "source_kind": "inspect.post_repair_repair_candidates",
        "candidate_count": total,
        "assets": assets_payload,
        "report_path": str(report_path),
    }
