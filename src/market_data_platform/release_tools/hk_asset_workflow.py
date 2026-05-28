#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from market_data_platform.current_assets import (
    build_hk_current_contract,
    default_dataset_registry_path,
    default_hk_current_contract_path,
    write_dataset_registry,
    write_current_contract,
)

from .hk_asset_workflow_config import (
    DEFAULT_PACKAGE_PARTS,
    DEFAULT_PHASES,
    INSPECT_ASSETS,
    PATCH_MERGE_SUPPORTED_ASSETS,
    PROVIDER_PERMISSION_EXIT_CODE,
    REFRESH_ASSETS,
    REPAIR_ASSETS,
)
from .hk_asset_workflow_parser import build_parser
from .hk_asset_workflow_paths import (
    ASSETS_ROOT,
    RELEASES_ROOT,
    REPO_ROOT,
    REPORTS_ROOT,
    SnapshotBundle,
    Step,
    current_snapshot_bundle as _current_snapshot_bundle_impl,
    default_remaining_repair_candidates_path as _default_remaining_repair_candidates_path_impl,
    default_repair_queue_path as _default_repair_queue_path_impl,
    default_workflow_report_path as _default_workflow_report_path_impl,
    refreshed_snapshot_bundle as _refreshed_snapshot_bundle_impl,
)
from .hk_asset_workflow_report import (
    GATE_SEVERITY_RANK,
    REPAIR_SEVERITY_RANK,
    health_summary_hits_gate as _health_summary_hits_gate,
    init_workflow_report as _init_workflow_report_impl,
    record_blocked_alias_update as _record_blocked_alias_update,
    record_gate_trigger as _record_gate_trigger,
    record_skipped_step as _record_skipped_step,
    workflow_gate_enabled as _workflow_gate_enabled,
    write_json_report as _write_json_report,
    write_workflow_report as _write_workflow_report,
)
from .hk_asset_workflow_state import (
    WorkflowExecutionResult as _WorkflowExecutionResult,
    WorkflowGateState as _WorkflowGateState,
    WorkflowPlan as _WorkflowPlan,
)
from .package_assets import create_relative_symlink


def _normalize_target_date(value: str) -> str:
    token = value.replace("-", "").strip()
    if len(token) != 8 or not token.isdigit():
        raise SystemExit(f"--target-date must be YYYYMMDD or YYYY-MM-DD. Got: {value!r}")
    return token


def _repo_relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _platform_executable() -> list[str]:
    return [sys.executable, "-m", "market_data_platform.cli"]


def _current_snapshot_bundle() -> SnapshotBundle:
    return _current_snapshot_bundle_impl(assets_root=ASSETS_ROOT)


def _refreshed_snapshot_bundle(target_date: str) -> SnapshotBundle:
    return _refreshed_snapshot_bundle_impl(target_date, assets_root=ASSETS_ROOT)


def _default_workflow_report_path(target_date: str) -> Path:
    return _default_workflow_report_path_impl(target_date, reports_root=REPORTS_ROOT)


def _default_repair_queue_path(target_date: str) -> Path:
    return _default_repair_queue_path_impl(target_date, reports_root=REPORTS_ROOT)


def _default_remaining_repair_candidates_path(target_date: str) -> Path:
    return _default_remaining_repair_candidates_path_impl(
        target_date,
        reports_root=REPORTS_ROOT,
    )


def _run(cmd: list[str], *, dry_run: bool) -> subprocess.CompletedProcess:
    printable = " ".join(shlex.quote(part) for part in cmd)
    print("+", printable)
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(
        cmd,
        check=False,
        capture_output=False,
        text=True,
        cwd=REPO_ROOT,
    )


def _phase_selection(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.phase or DEFAULT_PHASES))


def _selected_refresh_assets(args: argparse.Namespace) -> tuple[str, ...]:
    selected = list(dict.fromkeys(args.refresh_asset or REFRESH_ASSETS))
    if "etf_daily_clean" in selected and "etf_daily" not in selected:
        selected.insert(selected.index("etf_daily_clean"), "etf_daily")
    if any(asset in selected for asset in ("etf_daily", "etf_daily_clean")) and "etf_instruments" not in selected:
        insert_at = min(
            selected.index(asset)
            for asset in ("etf_daily", "etf_daily_clean")
            if asset in selected
        )
        selected.insert(insert_at, "etf_instruments")
    return tuple(dict.fromkeys(selected))


def _selected_inspect_assets(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.inspect_asset or INSPECT_ASSETS))


def _selected_parts(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.part or DEFAULT_PACKAGE_PARTS))


def _selected_repair_assets(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.repair_asset or REPAIR_ASSETS))


def _should_refresh_universe(
    args: argparse.Namespace,
    *,
    phases: tuple[str, ...],
    selected_mutating_assets: tuple[str, ...],
) -> bool:
    if not bool(getattr(args, "refresh_universe", True)):
        return False
    if args.no_repoint_latest:
        return False
    if not any(phase in phases for phase in ("refresh", "repair")):
        return False
    return "daily_clean" in selected_mutating_assets


def _load_asset_manifest(asset_dir: Path, *, asset_name: str) -> dict[str, object]:
    manifest_path = asset_dir / "manifest.yml"
    if not manifest_path.exists():
        raise SystemExit(
            f"Patch refresh requires an existing {asset_name} manifest: {manifest_path}"
        )
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Invalid manifest payload for {asset_name}: {manifest_path}")
    return payload


def _resolve_asset_end_date(asset_dir: Path, *, asset_name: str) -> str:
    manifest = _load_asset_manifest(asset_dir, asset_name=asset_name)
    query = manifest.get("query")
    if not isinstance(query, dict):
        raise SystemExit(
            f"Patch refresh requires manifest.query with an end date for {asset_name}: {asset_dir / 'manifest.yml'}"
        )
    for key in ("end_date", "date", "mapping_date", "as_of_date"):
        value = query.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return _normalize_target_date(text)
    raise SystemExit(
        f"Could not resolve current end date for patch refresh: {asset_dir / 'manifest.yml'}"
    )


def _subtract_calendar_days(date_text: str, days: int) -> str:
    parsed = datetime.strptime(date_text, "%Y%m%d")
    return (parsed - timedelta(days=days)).strftime("%Y%m%d")


def _patch_lookback_days(args: argparse.Namespace, *, asset_name: str) -> int:
    if asset_name in {"daily", "etf_daily"}:
        return int(args.daily_patch_lookback_days)
    return int(args.dated_patch_lookback_days)


def _resolve_patch_start_date(
    args: argparse.Namespace,
    *,
    asset_name: str,
    current_path: Path,
    floor_start_date: str,
) -> str:
    current_end_date = _resolve_asset_end_date(current_path, asset_name=asset_name)
    if args.target_date < current_end_date:
        raise SystemExit(
            f"Patch refresh requires --target-date >= current {asset_name} end date "
            f"({current_end_date}), got {args.target_date}."
        )
    lookback_days = _patch_lookback_days(args, asset_name=asset_name)
    window_start = _subtract_calendar_days(current_end_date, lookback_days - 1)
    return max(window_start, floor_start_date)


def _patch_snapshot_path(refreshed_path: Path) -> Path:
    return refreshed_path.parent / f"{refreshed_path.name}__patch"


def _etf_symbols_file_path(target_date: str) -> Path:
    return ASSETS_ROOT / "rqdata" / "hk" / "instruments" / f"hk_etf_symbols_{target_date}.txt"


def _normalize_report_path(path: Path | None, *, base_root: Path) -> Path | None:
    if path is None:
        return None
    return path.resolve() if path.is_absolute() else (base_root / path).resolve()


def _infer_manifest_path(path: Path) -> Path | None:
    if path.is_dir():
        candidate = path / "manifest.yml"
        if candidate.exists():
            return candidate.resolve()
        return None
    candidates = (
        path.with_name(f"{path.stem}.manifest.yml"),
        path.parent / "manifest.yml",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _load_manifest_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    output_dir = str(payload.get("output_dir") or "").strip() or None
    return {
        "path": str(path),
        "dataset": str(payload.get("dataset") or "").strip() or None,
        "status": str(payload.get("status") or "").strip() or None,
        "output_dir": output_dir,
        "snapshot_name": Path(output_dir).name if output_dir else None,
        "query": {
            key: str(query.get(key)).strip()
            for key in ("start_date", "end_date", "date", "mapping_date", "as_of_date")
            if query.get(key) is not None and str(query.get(key)).strip()
        },
        "totals": {
            key: int(totals.get(key) or 0)
            for key in ("rows", "files", "symbols_written", "symbols_missing_remote")
            if key in totals
        },
    }


def _describe_path(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    manifest_path = _infer_manifest_path(path)
    exists = path.exists()
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()) if exists or path.is_symlink() else str(path),
        "exists": exists,
        "is_symlink": path.is_symlink(),
        "kind": "directory" if path.is_dir() else "file" if path.is_file() else "missing",
        "manifest": _load_manifest_summary(manifest_path),
    }


def _load_health_report_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    quality_checks = payload.get("quality_checks") if isinstance(payload.get("quality_checks"), list) else []
    severity_counts = {"error": 0, "warning": 0, "info": 0}
    for item in quality_checks:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").strip().lower()
        if severity in severity_counts:
            severity_counts[severity] += 1
    issue_count = int(sum(severity_counts.values()))
    overall_severity = "none"
    if severity_counts["error"] > 0:
        overall_severity = "error"
    elif severity_counts["warning"] > 0:
        overall_severity = "warning"
    elif severity_counts["info"] > 0:
        overall_severity = "info"
    return {
        "report_path": str(path),
        "issue_count": issue_count,
        "severity_counts": severity_counts,
        "overall_severity": overall_severity,
        "history_issue_count": int(summary.get("history_issue_count") or 0),
    }


def _load_health_report_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Health report payload must be a JSON object: {path}")
    return payload


def _gate_relevant_quality_checks(
    payload: Mapping[str, Any],
    *,
    threshold: str,
) -> list[dict[str, Any]]:
    quality_checks = payload.get("quality_checks") if isinstance(payload.get("quality_checks"), list) else []
    threshold_rank = GATE_SEVERITY_RANK[threshold]
    relevant: list[dict[str, Any]] = []
    for item in quality_checks:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").strip().lower() or "info"
        if GATE_SEVERITY_RANK.get(severity, -1) >= threshold_rank:
            relevant.append(item)
    return relevant


def _build_gate_quality_summary(path: Path, *, threshold: str) -> dict[str, Any]:
    payload = _load_health_report_payload(path)
    relevant_checks = _gate_relevant_quality_checks(payload, threshold=threshold)
    severity_counts = {"error": 0, "warning": 0, "info": 0}
    for item in relevant_checks:
        severity = str(item.get("severity") or "").strip().lower() or "info"
        if severity in severity_counts:
            severity_counts[severity] += 1
    issue_count = int(sum(severity_counts.values()))
    overall_severity = "none"
    if severity_counts["error"] > 0:
        overall_severity = "error"
    elif severity_counts["warning"] > 0:
        overall_severity = "warning"
    elif severity_counts["info"] > 0:
        overall_severity = "info"
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "report_path": str(path),
        "issue_count": issue_count,
        "severity_counts": severity_counts,
        "overall_severity": overall_severity,
        "history_issue_count": int(summary.get("history_issue_count") or 0),
        "quality_checks": relevant_checks,
    }


RAW_DAILY_DIAGNOSTIC_CHECKS = {
    "daily_price_bounds_violation",
    "daily_nonpositive_price",
    "daily_negative_volume",
    "daily_negative_total_turnover",
    "daily_price_bounds_violation_any_date",
    "daily_nonpositive_price_any_date",
    "daily_negative_volume_any_date",
    "daily_negative_total_turnover_any_date",
}


def _daily_raw_diagnostic_gate_checks(summary: Mapping[str, Any]) -> set[str] | None:
    relevant_checks = summary.get("quality_checks")
    if not isinstance(relevant_checks, list) or not relevant_checks:
        return None
    checks: set[str] = set()
    for item in relevant_checks:
        if not isinstance(item, Mapping):
            return None
        check = str(item.get("check") or "").strip()
        if check not in RAW_DAILY_DIAGNOSTIC_CHECKS:
            return None
        checks.add(check)
    return checks


def _suppress_gate_hits_for_clean_daily_consumer_path(
    gate_results: list[tuple[Step, dict[str, Any]]],
    *,
    threshold: str,
    report: dict[str, Any],
) -> list[tuple[Step, dict[str, Any]]]:
    if not gate_results:
        return gate_results

    by_asset = {
        str(step.asset_name or ""): summary
        for step, summary in gate_results
        if step.asset_name
    }
    daily_summary = by_asset.get("daily")
    daily_clean_summary = by_asset.get("daily_clean")
    if daily_summary is None or daily_clean_summary is None:
        return gate_results
    diagnostic_checks = _daily_raw_diagnostic_gate_checks(daily_summary)
    if not diagnostic_checks:
        return gate_results
    if _health_summary_hits_gate(daily_clean_summary, threshold=threshold):
        return gate_results

    if diagnostic_checks == {"daily_price_bounds_violation"}:
        reason = "raw daily price-bounds-only issues are tolerated when daily_clean passes the gate"
    else:
        reason = (
            "raw daily diagnostic data-quality issues are tolerated when daily_clean passes the gate"
        )
    gate = report.setdefault("gate", {})
    suppressed = gate.setdefault("suppressed_triggered_assets", [])
    suppressed_entry = {
        "asset_name": "daily",
        "overall_severity": daily_summary.get("overall_severity"),
        "severity_counts": dict(daily_summary.get("severity_counts") or {}),
        "report_path": daily_summary.get("report_path"),
        "reason": reason,
    }
    if suppressed_entry not in suppressed:
        suppressed.append(suppressed_entry)

    return [
        (step, summary)
        for step, summary in gate_results
        if not (step.asset_name == "daily" and summary is daily_summary)
    ]


def _append_repair_candidate(
    candidates: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]],
    *,
    symbol: str | None,
    trade_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    check: str,
    severity: str | None,
    field: str | None = None,
    source: str,
    asset_name: str | None = None,
    reference_context: str | None = None,
    error: str | None = None,
) -> None:
    symbol_text = str(symbol or "").strip()
    if not symbol_text:
        return
    key = (
        symbol_text,
        str(trade_date).strip() or None if trade_date is not None else None,
        str(start_date).strip() or None if start_date is not None else None,
        str(end_date).strip() or None if end_date is not None else None,
    )
    entry = candidates.setdefault(
        key,
        {
            "symbol": symbol_text,
            "trade_date": key[1],
            "start_date": key[2],
            "end_date": key[3],
            "checks": [],
            "fields": [],
            "sources": [],
            "reference_contexts": [],
            "errors": [],
            "max_severity": "info",
            "asset_name": asset_name,
        },
    )
    if check and check not in entry["checks"]:
        entry["checks"].append(check)
    if field and field not in entry["fields"]:
        entry["fields"].append(field)
    if source and source not in entry["sources"]:
        entry["sources"].append(source)
    if reference_context and reference_context not in entry["reference_contexts"]:
        entry["reference_contexts"].append(reference_context)
    if error and error not in entry["errors"]:
        entry["errors"].append(error)
    severity_text = str(severity or "info").strip().lower() or "info"
    if REPAIR_SEVERITY_RANK.get(severity_text, -1) > REPAIR_SEVERITY_RANK.get(entry["max_severity"], -1):
        entry["max_severity"] = severity_text


def _extract_health_repair_candidates(
    *,
    payload: Mapping[str, Any],
    asset_name: str | None,
) -> list[dict[str, Any]]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    target_date = str(summary.get("target_date") or "").strip() or None
    field_rows = payload.get("field_coverage") if isinstance(payload.get("field_coverage"), list) else []
    field_map = {
        str(row.get("field")): row
        for row in field_rows
        if isinstance(row, Mapping) and str(row.get("field") or "").strip()
    }
    candidates: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}

    for row in payload.get("sample_missing_asset_file_details") or []:
        if not isinstance(row, Mapping):
            continue
        _append_repair_candidate(
            candidates,
            symbol=str(row.get("symbol") or "").strip() or None,
            trade_date=target_date,
            check="missing_asset_file",
            severity="error",
            source="missing_asset_file",
            asset_name=asset_name,
            error=str(row.get("error") or "").strip() or None,
        )

    for row in payload.get("sample_stale_symbols") or []:
        if not isinstance(row, Mapping):
            continue
        _append_repair_candidate(
            candidates,
            symbol=str(row.get("symbol") or "").strip() or None,
            start_date=str(row.get("latest_date") or "").strip() or None,
            end_date=target_date,
            check="stale_symbol_missing_target_date_row",
            severity="warning",
            source="sample_stale_symbols",
            asset_name=asset_name,
            error=str(row.get("status") or "").strip() or None,
        )

    for issue in payload.get("quality_checks") or []:
        if not isinstance(issue, Mapping):
            continue
        check = str(issue.get("check") or "").strip()
        severity = str(issue.get("severity") or "").strip().lower() or "info"
        field = str(issue.get("field") or "").strip() or None
        sample_symbols = [
            str(item).strip()
            for item in (issue.get("sample_symbols") or [])
            if str(item).strip()
        ]
        if not sample_symbols:
            continue
        field_row = field_map.get(field or "")
        detail_key = None
        if "provider_like_ffill" in check or check == "field_all_clean_missing_on_target_date_provider_like":
            detail_key = "sample_provider_like_ffill_symbols"
        elif "ffill_age_gt_" in check:
            detail_key = "sample_oldest_ffill_symbols"
        elif "fresh_target_gap" in check:
            detail_key = "sample_fresh_target_gap_symbols"
        detail_map = {}
        if detail_key and isinstance(field_row, Mapping):
            detail_map = {
                str(item.get("symbol") or "").strip(): item
                for item in (field_row.get(detail_key) or [])
                if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()
            }
        for symbol in sample_symbols:
            detail = detail_map.get(symbol, {})
            if isinstance(detail, Mapping) and (
                detail.get("start_date") is not None or detail.get("end_date") is not None
            ):
                _append_repair_candidate(
                    candidates,
                    symbol=symbol,
                    start_date=str(detail.get("start_date") or "").strip() or None,
                    end_date=str(detail.get("end_date") or "").strip() or None,
                    check=check,
                    severity=severity,
                    field=field,
                    source="quality_checks",
                    asset_name=asset_name,
                    reference_context=str(detail.get("reference_context") or "").strip() or None,
                )
                continue
            if isinstance(detail, Mapping) and detail.get("last_nonnull_date") is not None:
                _append_repair_candidate(
                    candidates,
                    symbol=symbol,
                    start_date=str(detail.get("last_nonnull_date") or "").strip() or None,
                    end_date=target_date,
                    check=check,
                    severity=severity,
                    field=field,
                    source="quality_checks",
                    asset_name=asset_name,
                    reference_context=str(detail.get("reference_context") or "").strip() or None,
                )
                continue
            _append_repair_candidate(
                candidates,
                symbol=symbol,
                trade_date=target_date,
                check=check,
                severity=severity,
                field=field,
                source="quality_checks",
                asset_name=asset_name,
            )

    history = payload.get("history") if isinstance(payload.get("history"), Mapping) else {}
    for issue in history.get("issues") or []:
        if not isinstance(issue, Mapping):
            continue
        check = str(issue.get("check") or "").strip()
        severity = str(issue.get("severity") or "").strip().lower() or "info"
        field = str(issue.get("field") or "").strip() or None
        for row in issue.get("sample_rows") or []:
            if not isinstance(row, Mapping):
                continue
            _append_repair_candidate(
                candidates,
                symbol=str(row.get("symbol") or "").strip() or None,
                trade_date=str(row.get("trade_date") or "").strip() or None,
                start_date=str(row.get("start_date") or "").strip() or None,
                end_date=str(row.get("end_date") or "").strip() or None,
                check=check,
                severity=severity,
                field=field,
                source="history_issues",
                asset_name=asset_name,
                reference_context=str(row.get("reference_context") or "").strip() or None,
            )

    return sorted(
        candidates.values(),
        key=lambda item: (
            -REPAIR_SEVERITY_RANK.get(str(item.get("max_severity") or "info"), -1),
            str(item.get("symbol") or ""),
            str(item.get("trade_date") or item.get("end_date") or item.get("start_date") or ""),
        ),
    )


def _load_health_report_analysis(path: Path, *, asset_name: str | None) -> dict[str, Any]:
    payload = _load_health_report_payload(path)
    return {
        "quality": _load_health_report_summary(path),
        "repair_candidates": _extract_health_repair_candidates(payload=payload, asset_name=asset_name),
    }


def _normalize_gate_severity(value: str) -> str:
    text = str(value or "").strip().lower() or "warning"
    if text not in GATE_SEVERITY_RANK:
        raise SystemExit("--gate-on-severity must be one of: none, info, warning, error.")
    return text


def _init_workflow_report(
    *,
    args: argparse.Namespace,
    phases: tuple[str, ...],
) -> dict[str, Any]:
    return _init_workflow_report_impl(
        target_date=args.target_date,
        refresh_mode=args.refresh_mode,
        phases=phases,
        selected_refresh_assets=_selected_refresh_assets(args),
        selected_inspect_assets=_selected_inspect_assets(args),
        selected_repair_assets=_selected_repair_assets(args),
        selected_parts=_selected_parts(args),
        inspect_fail_on_severity=args.inspect_fail_on_severity,
        gate_on_severity=args.gate_on_severity,
        repair_rerun_inspect=args.repair_rerun_inspect,
        repair_only_unresolved=args.repair_only_unresolved,
        repair_min_severity=args.repair_min_severity,
        repair_source_report=args.repair_source_report,
    )


def _record_refresh_report(
    report: dict[str, Any],
    *,
    step: Step,
) -> None:
    if not step.asset_name:
        return
    target_section = "repair" if str((step.report_metadata or {}).get("mode") or "") == "repair" else "refresh"
    assets = report.setdefault(target_section, {}).setdefault("assets", {})
    entry = assets.setdefault(step.asset_name, {"asset_name": step.asset_name})
    metadata = dict(step.report_metadata or {})
    action = str(metadata.get("action") or "").strip()
    mode = str(metadata.get("mode") or "").strip() or None
    if mode:
        entry["mode"] = mode
    if action == "patch_fetch":
        base_path = metadata.get("base_path")
        patch_path = metadata.get("patch_path")
        refreshed_path = metadata.get("refreshed_path")
        entry["base"] = _describe_path(base_path) if isinstance(base_path, Path) else None
        entry["patch_window"] = {
            "start_date": metadata.get("start_date"),
            "end_date": metadata.get("end_date"),
            "lookback_days": metadata.get("lookback_days"),
        }
        if metadata.get("candidate_count") is not None:
            entry["candidate_count"] = int(metadata["candidate_count"])
        if metadata.get("symbols_file") is not None:
            entry["symbols_file"] = str(metadata["symbols_file"])
        if metadata.get("symbols") is not None:
            entry["symbols"] = list(metadata["symbols"])
        entry["patch"] = _describe_path(patch_path) if isinstance(patch_path, Path) else None
        if isinstance(refreshed_path, Path):
            entry["planned_refreshed"] = str(refreshed_path)
        return
    if action in {"patch_merge", "full_refresh", "export"}:
        refreshed_path = metadata.get("refreshed_path")
        alias_path = metadata.get("alias_path")
        patch_path = metadata.get("patch_path")
        if isinstance(patch_path, Path):
            entry["merged_patch"] = _describe_path(patch_path)
        entry["refreshed"] = _describe_path(refreshed_path) if isinstance(refreshed_path, Path) else None
        entry["latest_alias"] = _describe_path(alias_path) if isinstance(alias_path, Path) else None
        if metadata.get("symbols_file") is not None:
            entry["symbols_file"] = str(metadata["symbols_file"])


def _record_inspect_report(
    report: dict[str, Any],
    *,
    step: Step,
) -> None:
    if not step.asset_name or step.summary_path is None or not step.summary_path.exists():
        return
    assets = report.setdefault("inspect", {}).setdefault("assets", {})
    metadata = dict(step.report_metadata or {})
    analysis = _load_health_report_analysis(step.summary_path, asset_name=step.asset_name)
    inspection_stage = str(metadata.get("inspection_stage") or "default").strip() or "default"
    entry = assets.setdefault(
        step.asset_name,
        {
            "asset_name": step.asset_name,
            "runs": [],
        },
    )
    entry["asset_dir"] = metadata.get("asset_dir")
    entry["target_date"] = metadata.get("target_date")
    entry["latest_stage"] = inspection_stage
    entry["quality"] = analysis["quality"]
    entry["repair_candidate_count"] = len(analysis["repair_candidates"])
    entry["repair_candidates"] = analysis["repair_candidates"]
    entry.setdefault("runs", []).append(
        {
            "stage": inspection_stage,
            "quality": analysis["quality"],
            "repair_candidate_count": len(analysis["repair_candidates"]),
            "repair_candidates": analysis["repair_candidates"],
        }
    )
    if inspection_stage == "post_repair":
        entry["post_repair_quality"] = analysis["quality"]
        entry["post_repair_repair_candidate_count"] = len(analysis["repair_candidates"])
        entry["post_repair_repair_candidates"] = analysis["repair_candidates"]


def _record_step_report(
    report: dict[str, Any],
    *,
    step: Step,
    result: subprocess.CompletedProcess,
) -> None:
    report.setdefault("steps", []).append(
        {
            "phase": step.phase,
            "label": step.label,
            "asset_name": step.asset_name,
            "returncode": int(result.returncode),
            "command": step.command,
        }
    )
    if step.phase == "refresh":
        _record_refresh_report(report, step=step)
    elif step.phase == "repair":
        _record_refresh_report(report, step=step)
    elif step.phase == "inspect":
        _record_inspect_report(report, step=step)
    elif step.phase == "post_refresh":
        report.setdefault("post_refresh", {}).setdefault("steps", []).append(
            {
                "label": step.label,
                "asset_name": step.asset_name,
                "metadata": {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in (step.report_metadata or {}).items()
                },
            }
        )


def _record_dependency_skipped_step(
    report: dict[str, Any],
    *,
    step: Step,
    reason: str,
) -> None:
    report.setdefault("steps", []).append(
        {
            "phase": step.phase,
            "label": step.label,
            "asset_name": step.asset_name,
            "returncode": None,
            "command": step.command,
            "skipped": True,
            "reason": reason,
        }
    )
    report.setdefault("workflow", {}).setdefault("skipped_steps", []).append(
        {
            "phase": step.phase,
            "label": step.label,
            "asset_name": step.asset_name,
            "reason": reason,
        }
    )


def _build_patch_refresh_steps(
    args: argparse.Namespace,
    *,
    asset_name: str,
    command_name: str,
    current_path: Path,
    refreshed_path: Path,
    by_date_file: Path | None = None,
    symbols_file: Path | None = None,
    floor_start_date: str,
    extra_mirror_args: tuple[str, ...] = (),
    nonfatal_returncodes: tuple[int, ...] = (),
    fetch_depends_on_assets: tuple[str, ...] = (),
) -> list[Step]:
    patch_start_date = _resolve_patch_start_date(
        args,
        asset_name=asset_name,
        current_path=current_path,
        floor_start_date=floor_start_date,
    )
    patch_dir = _patch_snapshot_path(refreshed_path)
    if (by_date_file is None) == (symbols_file is None):
        raise SystemExit("Patch refresh requires exactly one of by_date_file or symbols_file.")
    mirror_command = _rqdata_command(args, command_name)
    if by_date_file is not None:
        mirror_command.extend(["--by-date-file", _repo_relative(by_date_file)])
    if symbols_file is not None:
        mirror_command.extend(["--symbols-file", _repo_relative(symbols_file)])
    mirror_command.extend(
        [
            "--start-date",
            patch_start_date,
            "--end-date",
            args.target_date,
            "--name",
            patch_dir.name,
            *extra_mirror_args,
        ]
    )
    if args.resume:
        mirror_command.append("--resume")
    merge_command = [
        sys.executable,
        "-m",
        "market_data_platform.hk_assets.patch_merge",
        "--base-dir",
        _repo_relative(current_path),
        "--patch-dir",
        _repo_relative(patch_dir),
        "--out-dir",
        _repo_relative(refreshed_path),
        "--overwrite",
    ]
    display_name = asset_name.replace("_", " ")
    return [
        Step(
            phase="refresh",
            label=f"Mirror HK {display_name} patch window",
            command=mirror_command,
            asset_name=asset_name,
            nonfatal_returncodes=nonfatal_returncodes,
            depends_on_assets=fetch_depends_on_assets,
            report_metadata={
                "action": "patch_fetch",
                "mode": "patch",
                "base_path": current_path,
                "patch_path": patch_dir,
                "refreshed_path": refreshed_path,
                "start_date": patch_start_date,
                "end_date": args.target_date,
                "lookback_days": _patch_lookback_days(args, asset_name=asset_name),
            },
        ),
        Step(
            phase="refresh",
            label=f"Merge HK {display_name} patch into refreshed snapshot",
            command=merge_command,
            alias_target=refreshed_path,
            alias_link=current_path,
            asset_name=asset_name,
            depends_on_assets=(asset_name,),
            report_metadata={
                "action": "patch_merge",
                "mode": "patch",
                "patch_path": patch_dir,
                "refreshed_path": refreshed_path,
                "alias_path": current_path,
            },
        ),
    ]


def _normalize_repair_min_severity(value: str) -> str:
    text = str(value or "").strip().lower() or "warning"
    if text not in REPAIR_SEVERITY_RANK:
        raise SystemExit("--repair-min-severity must be one of: info, warning, error.")
    return text


def _repair_candidate_passes_threshold(candidate: Mapping[str, Any], *, min_severity: str) -> bool:
    candidate_severity = str(candidate.get("max_severity") or "info").strip().lower() or "info"
    return REPAIR_SEVERITY_RANK.get(candidate_severity, -1) >= REPAIR_SEVERITY_RANK[min_severity]


def _load_repair_source_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Repair source report not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Repair source report must be a JSON object: {path}")
    return payload


def _repair_source_kind(*, only_unresolved: bool) -> str:
    return "remaining_repair_candidates" if only_unresolved else "repair_candidates"


def _clone_candidate_list(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in items]


def _repair_source_candidates(
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
        return [], _repair_source_kind(only_unresolved=only_unresolved)

    if only_unresolved:
        remaining_assets = source_report.get("repair", {}).get("remaining_candidates", {}).get("assets")
        if isinstance(remaining_assets, Mapping):
            remaining_payload = remaining_assets.get(asset_name)
            if isinstance(remaining_payload, Mapping):
                candidates = remaining_payload.get("repair_candidates")
                if isinstance(candidates, list):
                    return _clone_candidate_list(
                        [item for item in candidates if isinstance(item, Mapping)]
                    ), "repair.remaining_candidates"
        candidates = asset_payload.get("post_repair_repair_candidates")
        if isinstance(candidates, list):
            return _clone_candidate_list(
                [item for item in candidates if isinstance(item, Mapping)]
            ), "inspect.post_repair_repair_candidates"
        return [], _repair_source_kind(only_unresolved=True)

    candidates = asset_payload.get("repair_candidates")
    if not isinstance(candidates, list):
        return [], "inspect.repair_candidates"
    return _clone_candidate_list(
        [item for item in candidates if isinstance(item, Mapping)]
    ), "inspect.repair_candidates"


def _repair_unresolved_source_available(source_report: Mapping[str, Any]) -> bool:
    remaining_assets = source_report.get("repair", {}).get("remaining_candidates", {}).get("assets")
    if isinstance(remaining_assets, Mapping):
        return True
    inspect_assets = source_report.get("inspect", {}).get("assets")
    if not isinstance(inspect_assets, Mapping):
        return False
    return any(
        isinstance(asset_payload, Mapping) and isinstance(asset_payload.get("post_repair_repair_candidates"), list)
        for asset_payload in inspect_assets.values()
    )


def _asset_path_from_bundle(bundle: SnapshotBundle, asset_name: str) -> Path:
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


def _repair_command_name(asset_name: str) -> str:
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


def _repair_symbols_file_path(args: argparse.Namespace, *, asset_name: str) -> Path:
    return args.reports_dir / "repair_inputs" / f"{asset_name}_{args.target_date}_repair_symbols.txt"


def _repair_patch_snapshot_path(refreshed_path: Path) -> Path:
    return refreshed_path.parent / f"{refreshed_path.name}__repair"


def _candidate_window_bounds(candidate: Mapping[str, Any]) -> tuple[str | None, str | None]:
    trade_date = str(candidate.get("trade_date") or "").strip() or None
    start_date = str(candidate.get("start_date") or "").strip() or None
    end_date = str(candidate.get("end_date") or "").strip() or None
    return trade_date or start_date or end_date, trade_date or end_date or start_date


def _write_repair_symbols_file(path: Path, *, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{symbol}\n" for symbol in symbols), encoding="utf-8")


def _repair_candidates_from_steps(steps: list[Step]) -> dict[str, list[dict[str, Any]]]:
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
        assets[step.asset_name] = _clone_candidate_list(
            [item for item in candidates if isinstance(item, Mapping)]
        )
    return assets


def _build_repair_candidate_payload(
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
        symbols = sorted({str(item.get("symbol") or "").strip() for item in candidates if str(item.get("symbol") or "").strip()})
        assets_payload[asset_name] = {
            "asset_name": asset_name,
            "candidate_count": len(candidates),
            "symbols": symbols,
            "repair_candidates": _clone_candidate_list(candidates),
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


def _build_remaining_repair_candidates_payload(
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
        cloned = _clone_candidate_list([item for item in candidates if isinstance(item, Mapping)])
        symbols = sorted({str(item.get("symbol") or "").strip() for item in cloned if str(item.get("symbol") or "").strip()})
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


def _build_repair_steps(
    args: argparse.Namespace,
    *,
    current: SnapshotBundle,
    refreshed: SnapshotBundle,
) -> list[Step]:
    source_report = _load_repair_source_report(args.repair_source_report)
    inspect_assets = source_report.get("inspect", {}).get("assets")
    if not isinstance(inspect_assets, Mapping):
        raise SystemExit(
            f"Repair source report does not contain inspect.assets: {args.repair_source_report}"
        )
    if args.repair_only_unresolved and not _repair_unresolved_source_available(source_report):
        raise SystemExit(
            "--repair-only-unresolved requires a source workflow report with "
            "repair.remaining_candidates or inspect.assets.<asset>.post_repair_repair_candidates."
        )

    min_severity = _normalize_repair_min_severity(args.repair_min_severity)
    steps: list[Step] = []
    for asset_name in _selected_repair_assets(args):
        raw_candidates, source_kind = _repair_source_candidates(
            source_report,
            asset_name=asset_name,
            only_unresolved=args.repair_only_unresolved,
        )
        candidates = [
            item
            for item in raw_candidates
            if isinstance(item, Mapping)
            and _repair_candidate_passes_threshold(item, min_severity=min_severity)
            and str(item.get("symbol") or "").strip()
        ]
        if not candidates:
            continue

        symbols = sorted({str(item.get("symbol")).strip() for item in candidates if str(item.get("symbol")).strip()})
        starts: list[str] = []
        ends: list[str] = []
        for item in candidates:
            start, end = _candidate_window_bounds(item)
            if start:
                starts.append(_normalize_target_date(start))
            if end:
                ends.append(_normalize_target_date(end))
        if not starts or not ends:
            continue

        start_date = min(starts)
        end_date = max(ends)
        current_path = _asset_path_from_bundle(current, asset_name)
        refreshed_path = _asset_path_from_bundle(refreshed, asset_name)
        patch_path = _repair_patch_snapshot_path(refreshed_path)
        symbols_file = _repair_symbols_file_path(args, asset_name=asset_name)
        if not args.dry_run:
            _write_repair_symbols_file(symbols_file, symbols=symbols)

        mirror_command = _rqdata_command(
            args,
            _repair_command_name(asset_name),
            "--symbols-file",
            _repo_relative(symbols_file),
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--name",
            patch_path.name,
        )
        if args.resume:
            mirror_command.append("--resume")
        merge_command = [
            sys.executable,
            "-m",
            "market_data_platform.hk_assets.patch_merge",
            "--base-dir",
            _repo_relative(current_path),
            "--patch-dir",
            _repo_relative(patch_path),
            "--out-dir",
            _repo_relative(refreshed_path),
            "--overwrite",
        ]
        display_name = asset_name.replace("_", " ")
        metadata = {
            "action": "patch_fetch",
            "mode": "repair",
            "base_path": current_path,
            "patch_path": patch_path,
            "refreshed_path": refreshed_path,
            "start_date": start_date,
            "end_date": end_date,
            "lookback_days": None,
            "candidate_count": len(candidates),
            "symbols_file": symbols_file,
            "symbols": symbols,
            "source_report": args.repair_source_report,
            "source_kind": source_kind,
            "min_severity": min_severity,
            "candidates": _clone_candidate_list(candidates),
        }
        steps.extend(
            [
                Step(
                    phase="repair",
                    label=f"Mirror HK {display_name} repair window",
                    command=mirror_command,
                    asset_name=asset_name,
                    report_metadata=metadata,
                ),
                Step(
                    phase="repair",
                    label=f"Merge HK {display_name} repair patch into refreshed snapshot",
                    command=merge_command,
                    alias_target=refreshed_path,
                    alias_link=current_path,
                    asset_name=asset_name,
                    report_metadata={
                        "action": "patch_merge",
                        "mode": "repair",
                        "patch_path": patch_path,
                        "refreshed_path": refreshed_path,
                        "alias_path": current_path,
                    },
                ),
            ]
        )
    return steps


def _planned_bundle(
    current: SnapshotBundle,
    refreshed: SnapshotBundle,
    *,
    selected_refresh_assets: tuple[str, ...],
) -> SnapshotBundle:
    payload = current.__dict__.copy()
    mapping = {
        "instruments": "instruments_file",
        "etf_instruments": "etf_instruments_file",
        "daily": "daily_dir",
        "daily_clean": "daily_clean_dir",
        "etf_daily": "etf_daily_dir",
        "etf_daily_clean": "etf_daily_clean_dir",
        "valuation": "valuation_dir",
        "ex_factors": "ex_factors_dir",
        "dividends": "dividends_dir",
        "shares": "shares_dir",
        "industry_changes": "industry_changes_dir",
        "southbound": "southbound_dir",
    }
    for asset_name in selected_refresh_assets:
        field_name = mapping.get(asset_name)
        if field_name is not None:
            payload[field_name] = getattr(refreshed, field_name)
    return SnapshotBundle(**payload)


def _forward_rqdata_credentials(args: argparse.Namespace) -> list[str]:
    forwarded: list[str] = []
    if args.config:
        forwarded.extend(["--config", args.config])
    if args.username:
        forwarded.extend(["--username", args.username])
    if args.password:
        forwarded.extend(["--password", args.password])
    return forwarded


def _rqdata_command(args: argparse.Namespace, *rest: str) -> list[str]:
    return [*_platform_executable(), "rqdata", "hk-assets", "--", *rest, *_forward_rqdata_credentials(args)]


def _build_refresh_steps(
    args: argparse.Namespace,
    *,
    current: SnapshotBundle,
    refreshed: SnapshotBundle,
) -> list[Step]:
    selected = _selected_refresh_assets(args)
    steps: list[Step] = []
    patch_mode = args.refresh_mode == "patch"

    if "instruments" in selected:
        steps.append(
            Step(
                phase="refresh",
                label="Export HK instruments",
                command=_rqdata_command(
                    args,
                    "export-hk-instruments",
                    "--by-date-file",
                    _repo_relative(args.universe_by_date),
                    "--out",
                    _repo_relative(refreshed.instruments_file),
                    "--force",
                ),
                alias_target=refreshed.instruments_file,
                alias_link=current.instruments_file,
                asset_name="instruments",
                report_metadata={
                    "action": "export",
                    "mode": "full",
                    "refreshed_path": refreshed.instruments_file,
                    "alias_path": current.instruments_file,
                },
            )
        )

    if "etf_instruments" in selected:
        etf_symbols_file = _etf_symbols_file_path(args.target_date)
        steps.append(
            Step(
                phase="refresh",
                label="Export HK ETF instruments",
                command=_rqdata_command(
                    args,
                    "export-hk-instruments",
                    "--instrument-type",
                    "ETF",
                    "--out",
                    _repo_relative(refreshed.etf_instruments_file),
                    "--symbols-out",
                    _repo_relative(etf_symbols_file),
                    "--force",
                ),
                alias_target=refreshed.etf_instruments_file,
                alias_link=current.etf_instruments_file,
                asset_name="etf_instruments",
                report_metadata={
                    "action": "export",
                    "mode": "full",
                    "refreshed_path": refreshed.etf_instruments_file,
                    "alias_path": current.etf_instruments_file,
                    "symbols_file": etf_symbols_file,
                },
            )
        )

    if "daily" in selected:
        if patch_mode:
            steps.extend(
                _build_patch_refresh_steps(
                    args,
                    asset_name="daily",
                    command_name="mirror-hk-daily",
                    current_path=current.daily_dir,
                    refreshed_path=refreshed.daily_dir,
                    by_date_file=args.universe_by_date,
                    floor_start_date=args.start_date,
                )
            )
        else:
            command = _rqdata_command(
                args,
                "mirror-hk-daily",
                "--by-date-file",
                _repo_relative(args.universe_by_date),
                "--start-date",
                args.start_date,
                "--end-date",
                args.target_date,
                "--name",
                refreshed.daily_dir.name,
            )
            if args.resume:
                command.append("--resume")
            steps.append(
                Step(
                    phase="refresh",
                    label="Mirror HK daily",
                    command=command,
                    alias_target=refreshed.daily_dir,
                    alias_link=current.daily_dir,
                    asset_name="daily",
                    report_metadata={
                        "action": "full_refresh",
                        "mode": "full",
                        "refreshed_path": refreshed.daily_dir,
                        "alias_path": current.daily_dir,
                        "start_date": args.start_date,
                        "end_date": args.target_date,
                    },
                )
            )

    if "daily_clean" in selected:
        steps.append(
            Step(
                phase="refresh",
                label="Build HK daily clean layer",
                command=[
                    *_platform_executable(),
                    "rqdata",
                    "hk-assets",
                    "--",
                    "build-hk-daily-clean-layer",
                    "--asset-dir",
                    _repo_relative(refreshed.daily_dir if "daily" in selected else current.daily_dir),
                    "--out-dir",
                    _repo_relative(refreshed.daily_clean_dir),
                    "--overwrite",
                ],
                alias_target=refreshed.daily_clean_dir,
                alias_link=current.daily_clean_dir,
                asset_name="daily_clean",
            )
        )

    if "etf_daily" in selected:
        etf_symbols_file = _etf_symbols_file_path(args.target_date)
        if patch_mode:
            steps.extend(
                _build_patch_refresh_steps(
                    args,
                    asset_name="etf_daily",
                    command_name="mirror-hk-daily",
                    current_path=current.etf_daily_dir,
                    refreshed_path=refreshed.etf_daily_dir,
                    symbols_file=etf_symbols_file,
                    floor_start_date=args.start_date,
                    extra_mirror_args=("--provider-permission-preflight",),
                    nonfatal_returncodes=(PROVIDER_PERMISSION_EXIT_CODE,),
                    fetch_depends_on_assets=("etf_instruments",),
                )
            )
        else:
            command = _rqdata_command(
                args,
                "mirror-hk-daily",
                "--symbols-file",
                _repo_relative(etf_symbols_file),
                "--start-date",
                args.start_date,
                "--end-date",
                args.target_date,
                "--name",
                refreshed.etf_daily_dir.name,
                "--provider-permission-preflight",
            )
            if args.resume:
                command.append("--resume")
            steps.append(
                Step(
                    phase="refresh",
                    label="Mirror HK ETF daily",
                    command=command,
                    alias_target=refreshed.etf_daily_dir,
                    alias_link=current.etf_daily_dir,
                    asset_name="etf_daily",
                    nonfatal_returncodes=(PROVIDER_PERMISSION_EXIT_CODE,),
                    depends_on_assets=("etf_instruments",),
                    report_metadata={
                        "action": "full_refresh",
                        "mode": "full",
                        "refreshed_path": refreshed.etf_daily_dir,
                        "alias_path": current.etf_daily_dir,
                        "start_date": args.start_date,
                        "end_date": args.target_date,
                        "symbols_file": etf_symbols_file,
                    },
                )
            )

    if "etf_daily_clean" in selected:
        steps.append(
            Step(
                phase="refresh",
                label="Build HK ETF daily clean layer",
                command=[
                    *_platform_executable(),
                    "rqdata",
                    "hk-assets",
                    "--",
                    "build-hk-daily-clean-layer",
                    "--asset-dir",
                    _repo_relative(refreshed.etf_daily_dir if "etf_daily" in selected else current.etf_daily_dir),
                    "--out-dir",
                    _repo_relative(refreshed.etf_daily_clean_dir),
                    "--instruments-file",
                    _repo_relative(refreshed.etf_instruments_file if "etf_instruments" in selected else current.etf_instruments_file),
                    "--overwrite",
                ],
                alias_target=refreshed.etf_daily_clean_dir,
                alias_link=current.etf_daily_clean_dir,
                asset_name="etf_daily_clean",
                depends_on_assets=("etf_daily",),
            )
        )

    dated_assets = (
        ("valuation", "mirror-hk-valuation", refreshed.valuation_dir, current.valuation_dir),
        ("ex_factors", "mirror-hk-ex-factors", refreshed.ex_factors_dir, current.ex_factors_dir),
        ("dividends", "mirror-hk-dividends", refreshed.dividends_dir, current.dividends_dir),
        ("shares", "mirror-hk-shares", refreshed.shares_dir, current.shares_dir),
        (
            "industry_changes",
            "mirror-hk-industry-changes",
            refreshed.industry_changes_dir,
            current.industry_changes_dir,
        ),
    )
    for asset_name, command_name, refreshed_path, alias_link in dated_assets:
        if asset_name not in selected:
            continue
        if patch_mode and asset_name in PATCH_MERGE_SUPPORTED_ASSETS:
            steps.extend(
                _build_patch_refresh_steps(
                    args,
                    asset_name=asset_name,
                    command_name=command_name,
                    current_path=alias_link,
                    refreshed_path=refreshed_path,
                    by_date_file=args.universe_by_date,
                    floor_start_date=args.start_date,
                )
            )
            continue
        command = _rqdata_command(
            args,
            command_name,
            "--by-date-file",
            _repo_relative(args.universe_by_date),
            "--start-date",
            args.start_date,
            "--end-date",
            args.target_date,
            "--name",
            refreshed_path.name,
        )
        if args.resume:
            command.append("--resume")
        steps.append(
            Step(
                phase="refresh",
                label=f"Mirror HK {asset_name}",
                command=command,
                alias_target=refreshed_path,
                alias_link=alias_link,
                asset_name=asset_name,
                report_metadata={
                    "action": "full_refresh",
                    "mode": "full",
                    "refreshed_path": refreshed_path,
                    "alias_path": alias_link,
                    "start_date": args.start_date,
                    "end_date": args.target_date,
                },
            )
        )

    if "southbound" in selected:
        command = _rqdata_command(
            args,
            "mirror-hk-southbound",
            "--by-date-file",
            _repo_relative(args.southbound_by_date),
            "--start-date",
            args.southbound_start_date,
            "--end-date",
            args.target_date,
            "--trading-type",
            "both",
            "--name",
            refreshed.southbound_dir.name,
        )
        if args.resume:
            command.append("--resume")
        steps.append(
            Step(
                phase="refresh",
                label="Mirror HK southbound",
                command=command,
                alias_target=refreshed.southbound_dir,
                alias_link=current.southbound_dir,
                asset_name="southbound",
                report_metadata={
                    "action": "full_refresh",
                    "mode": "full",
                    "refreshed_path": refreshed.southbound_dir,
                    "alias_path": current.southbound_dir,
                    "start_date": args.southbound_start_date,
                    "end_date": args.target_date,
                },
            )
        )

    return steps


def _inspect_report_name(asset_name: str, target_date: str, *, stage: str = "default") -> str:
    suffix = "_post_repair" if stage == "post_repair" else ""
    if asset_name == "valuation":
        return f"hk_valuation_health_{target_date}_with_daily_ref{suffix}.json"
    return f"hk_{asset_name}_health_{target_date}_full_history{suffix}.json"


def _build_inspect_steps(
    args: argparse.Namespace,
    *,
    bundle: SnapshotBundle,
    asset_names: tuple[str, ...] | None = None,
    inspection_stage: str = "default",
) -> list[Step]:
    selected = asset_names or _selected_inspect_assets(args)
    steps: list[Step] = []
    mapping = {
        "daily": bundle.daily_dir,
        "daily_clean": bundle.daily_clean_dir,
        "valuation": bundle.valuation_dir,
        "ex_factors": bundle.ex_factors_dir,
        "dividends": bundle.dividends_dir,
        "shares": bundle.shares_dir,
        "industry_changes": bundle.industry_changes_dir,
        "southbound": bundle.southbound_dir,
    }
    for asset_name in selected:
        report_path = args.reports_dir / _inspect_report_name(
            asset_name,
            args.target_date,
            stage=inspection_stage,
        )
        command = [
            *_platform_executable(),
            "rqdata",
            "hk-assets",
            "--",
            "inspect-hk-asset-health",
            "--asset-dir",
            _repo_relative(mapping[asset_name]),
            "--target-date",
            args.target_date,
            "--format",
            "json",
            "--out",
            _repo_relative(report_path),
            "--fail-on-severity",
            args.inspect_fail_on_severity,
        ]
        include_history = not args.skip_history
        if inspection_stage == "post_repair" and args.repair_post_inspect_skip_history:
            include_history = False
        if include_history:
            command.append("--include-history")
        if asset_name == "valuation":
            command.extend(["--daily-asset-dir", _repo_relative(bundle.daily_clean_dir)])
            if include_history:
                if int(args.valuation_history_tail_days) > 0:
                    command.extend(["--history-tail-days", str(args.valuation_history_tail_days)])
                if float(args.valuation_history_timeout_seconds) > 0:
                    command.extend(
                        [
                            "--history-timeout-seconds",
                            str(args.valuation_history_timeout_seconds),
                        ]
                    )
                if int(args.valuation_history_progress_every_symbols) > 0:
                    command.extend(
                        [
                            "--history-progress-every-symbols",
                            str(args.valuation_history_progress_every_symbols),
                        ]
                    )
        label = f"Inspect HK {asset_name} asset health"
        if inspection_stage == "post_repair":
            label = f"Inspect HK {asset_name} asset health after repair"
        steps.append(
            Step(
                phase="inspect",
                label=label,
                command=command,
                summary_path=report_path,
                asset_name=asset_name,
                report_metadata={
                    "asset_dir": str(mapping[asset_name]),
                    "target_date": args.target_date,
                    "inspection_stage": inspection_stage,
                },
            )
        )
    return steps


def _build_package_step(
    args: argparse.Namespace,
    *,
    bundle: SnapshotBundle,
) -> Step:
    command = [
        sys.executable,
        "-m",
        "market_data_platform.release_tools.package_assets",
        "--preset",
        args.preset,
        "--dest",
        _repo_relative(args.package_dest),
        "--name",
        args.distribution_name,
        "--as-of",
        args.target_date,
        "--overwrite",
        "--daily-snapshot",
        _repo_relative(bundle.daily_dir),
        "--valuation-snapshot",
        _repo_relative(bundle.valuation_dir),
        "--instruments-file",
        _repo_relative(bundle.instruments_file),
        "--ex-factors-snapshot",
        _repo_relative(bundle.ex_factors_dir),
        "--dividends-snapshot",
        _repo_relative(bundle.dividends_dir),
        "--shares-snapshot",
        _repo_relative(bundle.shares_dir),
        "--southbound-snapshot",
        _repo_relative(bundle.southbound_dir),
        "--industry-changes-snapshot",
        _repo_relative(bundle.industry_changes_dir),
        "--universe-by-date",
        _repo_relative(bundle.universe_by_date),
        "--universe-symbols",
        _repo_relative(bundle.universe_symbols),
    ]
    if bundle.universe_meta is not None:
        command.extend(["--universe-meta", _repo_relative(bundle.universe_meta)])
    if bundle.pit_dir is not None:
        command.extend(["--pit-snapshot", _repo_relative(bundle.pit_dir)])
    if bundle.exchange_rate_dir is not None:
        command.extend(["--exchange-rate-snapshot", _repo_relative(bundle.exchange_rate_dir)])
    if bundle.financial_details_dir is not None:
        command.extend(["--financial-details-snapshot", _repo_relative(bundle.financial_details_dir)])
    for part_name in _selected_parts(args):
        command.extend(["--part", part_name])
    return Step(phase="package", label="Stage HK asset release parts", command=command)


def _build_universe_refresh_step(
    args: argparse.Namespace,
    *,
    bundle: SnapshotBundle,
) -> Step:
    command = [
        sys.executable,
        "-m",
        "market_data_platform.hk_assets.build_hk_daily_asset_universe",
        "--daily-asset-dir",
        _repo_relative(bundle.daily_clean_dir),
        "--start-date",
        args.start_date,
        "--end-date",
        args.target_date,
        "--out",
        _repo_relative(bundle.universe_by_date),
        "--latest-out",
        _repo_relative(bundle.universe_symbols),
        "--meta-out",
        _repo_relative(bundle.universe_meta) if bundle.universe_meta is not None else "",
        "--write-meta",
    ]
    if bundle.universe_meta is None:
        meta_index = command.index("--meta-out")
        del command[meta_index : meta_index + 2]
    return Step(
        phase="post_refresh",
        label="Rebuild HK universe from refreshed daily clean layer",
        command=command,
        asset_name="universe",
        report_metadata={
            "action": "rebuild_universe",
            "daily_asset_dir": bundle.daily_clean_dir,
            "by_date_file": bundle.universe_by_date,
            "symbols_file": bundle.universe_symbols,
            "meta_file": bundle.universe_meta,
            "start_date": args.start_date,
            "end_date": args.target_date,
        },
    )


def _build_release_step(args: argparse.Namespace) -> Step:
    command = [
        sys.executable,
        "-m",
        "market_data_platform.release_tools.release_assets",
        "--staged-root",
        _repo_relative(args.package_dest),
        "--tar-dir",
        _repo_relative(args.tar_dir),
    ]
    for part_name in _selected_parts(args):
        command.extend(["--part", part_name])
    if args.repo:
        command.extend(["--repo", args.repo])
    if args.tag:
        command.extend(["--tag", args.tag])
    if args.title:
        command.extend(["--title", args.title])
    if args.prerelease:
        command.append("--prerelease")
    if args.draft:
        command.append("--draft")
    if args.latest:
        command.append("--latest")
    if args.clobber:
        command.append("--clobber")
    return Step(phase="release", label="Create or update GitHub Release", command=command)


def _summarize_report(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    checks = payload.get("quality_checks") or []
    severity_counts = {"error": 0, "warning": 0, "info": 0}
    for item in checks:
        severity = str(item.get("severity") or "").lower()
        if severity in severity_counts:
            severity_counts[severity] += 1
    summary = payload.get("summary") or {}
    return (
        f"errors={severity_counts['error']} "
        f"warnings={severity_counts['warning']} "
        f"info={severity_counts['info']} "
        f"history_issues={summary.get('history_issue_count', 0)} "
        f"report={_repo_relative(path)}"
    )


def _block_alias_repoint(
    step: Step,
    *,
    dry_run: bool,
    report: dict[str, Any] | None,
    reason: str,
) -> None:
    if step.alias_target is None or step.alias_link is None:
        return
    if dry_run:
        return
    print(
        "  blocked latest alias repoint:",
        f"{_repo_relative(step.alias_link)} -> {step.alias_target.name}",
        f"({reason})",
    )
    if report is not None:
        _record_blocked_alias_update(report, step=step, reason=reason)


def _maybe_repoint_alias(step: Step, *, dry_run: bool, repoint_latest: bool) -> None:
    if dry_run or not repoint_latest:
        return
    if step.alias_target is None or step.alias_link is None:
        return
    if not step.alias_target.exists():
        raise SystemExit(f"Expected refreshed output not found: {step.alias_target}")
    create_relative_symlink(step.alias_target, step.alias_link)
    print(f"  repointed latest alias: {_repo_relative(step.alias_link)} -> {step.alias_target.name}")


def _is_safe_intermediate_patch_path(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(ASSETS_ROOT.resolve())
    except ValueError:
        return False
    if path.is_symlink():
        return False
    return path.name.endswith("__patch") or path.name.endswith("__repair")


def _prune_successful_patch_dirs(
    patch_dirs: Sequence[Path],
    *,
    report: dict[str, Any],
) -> None:
    results: list[dict[str, Any]] = []
    for path in sorted(dict.fromkeys(patch_dirs), key=lambda item: item.as_posix()):
        result = {
            "path": str(path),
            "safe_intermediate_path": _is_safe_intermediate_patch_path(path),
        }
        if not result["safe_intermediate_path"]:
            result["status"] = "skipped"
            result["reason"] = "not_safe_intermediate_patch_path"
        elif not path.exists():
            result["status"] = "skipped"
            result["reason"] = "path_missing"
        else:
            shutil.rmtree(path)
            result["status"] = "deleted"
        results.append(result)
        if result["status"] == "deleted":
            print(f"  pruned intermediate patch dir: {_repo_relative(path)}")
    report.setdefault("workflow", {})["pruned_intermediate_patch_dirs"] = results


def _update_active_bundle(
    bundle: SnapshotBundle,
    step: Step,
) -> SnapshotBundle:
    if step.alias_target is None:
        return bundle
    replacements = {
        bundle.instruments_file: ("instruments_file", step.alias_target),
        bundle.etf_instruments_file: ("etf_instruments_file", step.alias_target),
        bundle.daily_dir: ("daily_dir", step.alias_target),
        bundle.daily_clean_dir: ("daily_clean_dir", step.alias_target),
        bundle.etf_daily_dir: ("etf_daily_dir", step.alias_target),
        bundle.etf_daily_clean_dir: ("etf_daily_clean_dir", step.alias_target),
        bundle.valuation_dir: ("valuation_dir", step.alias_target),
        bundle.ex_factors_dir: ("ex_factors_dir", step.alias_target),
        bundle.dividends_dir: ("dividends_dir", step.alias_target),
        bundle.shares_dir: ("shares_dir", step.alias_target),
        bundle.industry_changes_dir: ("industry_changes_dir", step.alias_target),
        bundle.southbound_dir: ("southbound_dir", step.alias_target),
    }
    if step.alias_link not in replacements:
        return bundle
    field_name, value = replacements[step.alias_link]
    payload = bundle.__dict__.copy()
    payload[field_name] = value
    return SnapshotBundle(**payload)


def _default_package_dest(target_date: str) -> Path:
    return RELEASES_ROOT / f"hk_rqdata_assets_{target_date}" / "staged"


def _default_tar_dir(target_date: str) -> Path:
    return RELEASES_ROOT / f"hk_rqdata_assets_{target_date}" / "tarballs"


def _normalize_workflow_args(args: argparse.Namespace) -> None:
    args.target_date = _normalize_target_date(args.target_date)
    args.start_date = _normalize_target_date(args.start_date)
    args.southbound_start_date = _normalize_target_date(args.southbound_start_date)
    args.gate_on_severity = _normalize_gate_severity(args.gate_on_severity)
    if args.daily_patch_lookback_days <= 0:
        raise SystemExit("--daily-patch-lookback-days must be > 0.")
    if args.dated_patch_lookback_days <= 0:
        raise SystemExit("--dated-patch-lookback-days must be > 0.")
    if args.valuation_history_tail_days < 0:
        raise SystemExit("--valuation-history-tail-days must be >= 0.")
    if args.valuation_history_timeout_seconds < 0:
        raise SystemExit("--valuation-history-timeout-seconds must be >= 0.")
    if args.valuation_history_progress_every_symbols < 0:
        raise SystemExit("--valuation-history-progress-every-symbols must be >= 0.")
    args.package_dest = args.package_dest or _default_package_dest(args.target_date)
    args.tar_dir = args.tar_dir or _default_tar_dir(args.target_date)
    args.reports_dir = args.reports_dir.resolve() if args.reports_dir.is_absolute() else REPO_ROOT / args.reports_dir
    args.workflow_report = _normalize_report_path(
        args.workflow_report or _default_workflow_report_path(args.target_date),
        base_root=REPO_ROOT,
    )
    args.repair_source_report = _normalize_report_path(
        args.repair_source_report or args.workflow_report,
        base_root=REPO_ROOT,
    )
    args.package_dest = (
        args.package_dest.resolve() if args.package_dest.is_absolute() else REPO_ROOT / args.package_dest
    )
    args.tar_dir = args.tar_dir.resolve() if args.tar_dir.is_absolute() else REPO_ROOT / args.tar_dir


def _build_workflow_plan(
    args: argparse.Namespace,
    *,
    phases: tuple[str, ...],
    current: SnapshotBundle,
    refreshed: SnapshotBundle,
    active_bundle: SnapshotBundle,
) -> _WorkflowPlan:
    selected_refresh_assets = _selected_refresh_assets(args)
    selected_repair_assets = _selected_repair_assets(args)
    planned_refresh_bundle = _planned_bundle(
        current,
        refreshed,
        selected_refresh_assets=selected_refresh_assets,
    )

    steps: list[Step] = []
    if "refresh" in phases:
        steps.extend(_build_refresh_steps(args, current=current, refreshed=refreshed))
    if "inspect" in phases:
        inspect_bundle = active_bundle if "refresh" not in phases else planned_refresh_bundle
        steps.extend(_build_inspect_steps(args, bundle=inspect_bundle))
    repair_steps: list[Step] = []
    if "repair" in phases:
        repair_bundle_current = active_bundle if "refresh" not in phases else planned_refresh_bundle
        repair_bundle_refreshed = _planned_bundle(
            repair_bundle_current,
            refreshed,
            selected_refresh_assets=selected_repair_assets,
        )
        repair_steps = _build_repair_steps(
            args,
            current=repair_bundle_current,
            refreshed=repair_bundle_refreshed,
        )
        steps.extend(repair_steps)
        if args.repair_rerun_inspect and repair_steps:
            repaired_assets = tuple(
                dict.fromkeys(
                    step.asset_name
                    for step in repair_steps
                    if step.asset_name in INSPECT_ASSETS
                )
            )
            if args.repair_rerun_inspect_asset:
                selected_post_repair = set(args.repair_rerun_inspect_asset)
                repaired_assets = tuple(
                    asset for asset in repaired_assets if asset in selected_post_repair
                )
            if repaired_assets:
                steps.extend(
                    _build_inspect_steps(
                        args,
                        bundle=repair_bundle_refreshed,
                        asset_names=repaired_assets,
                        inspection_stage="post_repair",
                    )
                )
    repair_assets_with_steps = tuple(
        dict.fromkeys(
            step.asset_name
            for step in repair_steps
            if step.asset_name
        )
    )
    selected_mutating_assets = tuple(
        dict.fromkeys(
            [
                *(selected_refresh_assets if "refresh" in phases else ()),
                *(repair_assets_with_steps if "repair" in phases else ()),
            ]
        )
    )
    planned_bundle = _planned_bundle(
        current,
        refreshed,
        selected_refresh_assets=selected_mutating_assets,
    )
    if _should_refresh_universe(
        args,
        phases=phases,
        selected_mutating_assets=selected_mutating_assets,
    ):
        steps.append(_build_universe_refresh_step(args, bundle=planned_bundle))
    if "package" in phases:
        package_bundle = active_bundle if not any(phase in phases for phase in ("refresh", "repair")) else planned_bundle
        steps.append(_build_package_step(args, bundle=package_bundle))
    if "release" in phases:
        steps.append(_build_release_step(args))

    return _WorkflowPlan(
        steps=steps,
        repair_steps=repair_steps,
        selected_mutating_assets=selected_mutating_assets,
        planned_bundle=planned_bundle,
    )


def _ensure_workflow_output_dirs(args: argparse.Namespace) -> None:
    if args.dry_run:
        return
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    args.package_dest.parent.mkdir(parents=True, exist_ok=True)
    args.tar_dir.parent.mkdir(parents=True, exist_ok=True)
    if args.workflow_report is not None:
        args.workflow_report.parent.mkdir(parents=True, exist_ok=True)


def _workflow_gate_stage(steps: Sequence[Step]) -> str | None:
    inspect_stages = [
        _step_inspection_stage(step)
        for step in steps
        if step.phase == "inspect"
    ]
    if "post_repair" in inspect_stages:
        return "post_repair"
    if inspect_stages:
        return "default"
    return None


def _step_inspection_stage(step: Step) -> str:
    return str((step.report_metadata or {}).get("inspection_stage") or "default")


def _init_workflow_gate_state(
    *,
    args: argparse.Namespace,
    phases: tuple[str, ...],
    steps: Sequence[Step],
    workflow_report: dict[str, Any],
) -> _WorkflowGateState:
    stage = _workflow_gate_stage(steps)
    enabled = bool(
        stage
        and _workflow_gate_enabled(
            phases=phases,
            threshold=args.gate_on_severity,
            repair_rerun_inspect=args.repair_rerun_inspect,
        )
    )
    workflow_report.setdefault("gate", {})["enabled"] = enabled
    workflow_report.setdefault("gate", {})["stage"] = stage
    remaining_inspect_steps = sum(
        1
        for step in steps
        if step.phase == "inspect" and _step_inspection_stage(step) == stage
    )
    return _WorkflowGateState(
        stage=stage,
        enabled=enabled,
        triggered=False,
        results=[],
        remaining_inspect_steps=remaining_inspect_steps,
        pending_alias_steps=[],
    )


def _print_step_header(index: int, total: int, step: Step) -> None:
    print(f"==> [{index}/{total}] {step.phase}: {step.label}")


def _dependency_skip_reason(step: Step, non_actionable_assets: set[str]) -> str | None:
    dependency_hits = sorted(set(step.depends_on_assets).intersection(non_actionable_assets))
    if not dependency_hits:
        return None
    return "dependency marked non-actionable: " + ", ".join(dependency_hits)


def _skip_step_for_dependency(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    step: Step,
    reason: str,
) -> None:
    print(f"  skipped: {reason}")
    if not args.dry_run:
        _record_dependency_skipped_step(workflow_report, step=step, reason=reason)


def _skip_step_for_gate(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    step: Step,
    gate: _WorkflowGateState,
) -> bool:
    if not gate.triggered or step.phase not in {"post_refresh", "package", "release"}:
        return False
    reason = f"inspect gate triggered at severity >= {args.gate_on_severity}"
    print(f"  skipped due to inspect gate: {reason}")
    if not args.dry_run:
        if step.phase == "post_refresh":
            _record_dependency_skipped_step(workflow_report, step=step, reason=reason)
        else:
            _record_skipped_step(workflow_report, step=step, reason=reason)
    return True


def _record_nonfatal_step_result(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    step: Step,
    result: subprocess.CompletedProcess,
    non_actionable_assets: set[str],
) -> None:
    if step.asset_name:
        non_actionable_assets.add(step.asset_name)
        workflow_report.setdefault("workflow", {}).setdefault(
            "non_actionable_assets",
            [],
        ).append(
            {
                "asset_name": step.asset_name,
                "phase": step.phase,
                "label": step.label,
                "returncode": int(result.returncode),
                "reason": "provider_permission_or_boundary_gap",
            }
        )
    print(
        "  non-actionable provider/boundary gap:",
        f"asset={step.asset_name}",
        f"returncode={result.returncode}",
    )
    if not args.dry_run:
        _record_step_report(workflow_report, step=step, result=result)


def _record_patch_merge_success(step: Step, successful_patch_merge_dirs: list[Path]) -> None:
    metadata = step.report_metadata or {}
    if str(metadata.get("action") or "") != "patch_merge":
        return
    patch_path = metadata.get("patch_path")
    if isinstance(patch_path, Path):
        successful_patch_merge_dirs.append(patch_path)


def _handle_alias_after_step(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    step: Step,
    gate: _WorkflowGateState,
) -> None:
    should_defer_alias = (
        gate.enabled
        and gate.remaining_inspect_steps > 0
        and not args.no_repoint_latest
        and step.alias_target is not None
        and step.alias_link is not None
    )
    if should_defer_alias and not args.dry_run:
        gate.pending_alias_steps.append(step)
        print(
            "  deferred latest alias repoint until inspect gate clears:",
            f"{_repo_relative(step.alias_link)} -> {step.alias_target.name}",
        )
    elif gate.triggered:
        _block_alias_repoint(
            step,
            dry_run=args.dry_run,
            report=workflow_report if not args.dry_run else None,
            reason=f"inspect gate triggered at severity >= {args.gate_on_severity}",
        )
    else:
        _maybe_repoint_alias(step, dry_run=args.dry_run, repoint_latest=not args.no_repoint_latest)


def _release_pending_aliases(
    *,
    args: argparse.Namespace,
    gate: _WorkflowGateState,
) -> None:
    for pending_step in gate.pending_alias_steps:
        _maybe_repoint_alias(
            pending_step,
            dry_run=args.dry_run,
            repoint_latest=not args.no_repoint_latest,
        )
    gate.pending_alias_steps.clear()


def _block_pending_aliases(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    gate: _WorkflowGateState,
) -> None:
    for pending_step in gate.pending_alias_steps:
        _block_alias_repoint(
            pending_step,
            dry_run=args.dry_run,
            report=workflow_report,
            reason=f"inspect gate triggered at severity >= {args.gate_on_severity}",
        )
    gate.pending_alias_steps.clear()


def _finalize_inspect_gate_if_ready(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    gate: _WorkflowGateState,
) -> None:
    if gate.remaining_inspect_steps != 0:
        return
    gate_hits = [
        item
        for item in _suppress_gate_hits_for_clean_daily_consumer_path(
            gate.results,
            threshold=args.gate_on_severity,
            report=workflow_report,
        )
        if _health_summary_hits_gate(item[1], threshold=args.gate_on_severity)
    ]
    if gate_hits:
        gate.triggered = True
        for gate_step, gate_quality in gate_hits:
            _record_gate_trigger(workflow_report, step=gate_step, summary=gate_quality)
            print(
                "  inspect gate triggered:",
                f"asset={gate_step.asset_name}",
                f"stage={gate.stage}",
                f"overall_severity={gate_quality.get('overall_severity')}",
                f"threshold={args.gate_on_severity}",
            )
        _block_pending_aliases(args=args, workflow_report=workflow_report, gate=gate)
    elif gate.pending_alias_steps:
        _release_pending_aliases(args=args, gate=gate)


def _handle_inspect_summary_after_step(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
    step: Step,
    gate: _WorkflowGateState,
) -> None:
    if step.summary_path is None or args.dry_run:
        return
    if not step.summary_path.exists():
        raise SystemExit(f"Expected health report not found: {step.summary_path}")
    print("  " + _summarize_report(step.summary_path))
    inspection_stage = _step_inspection_stage(step)
    if gate.enabled and step.phase == "inspect" and inspection_stage == gate.stage:
        gate_quality = _build_gate_quality_summary(
            step.summary_path,
            threshold=args.gate_on_severity,
        )
        gate.results.append((step, gate_quality))
        gate.remaining_inspect_steps = max(0, gate.remaining_inspect_steps - 1)
        _finalize_inspect_gate_if_ready(
            args=args,
            workflow_report=workflow_report,
            gate=gate,
        )


def _run_workflow_steps(
    *,
    args: argparse.Namespace,
    phases: tuple[str, ...],
    steps: list[Step],
    workflow_report: dict[str, Any],
    active_bundle: SnapshotBundle,
) -> _WorkflowExecutionResult:
    gate = _init_workflow_gate_state(
        args=args,
        phases=phases,
        steps=steps,
        workflow_report=workflow_report,
    )
    non_actionable_assets: set[str] = set()
    successful_patch_merge_dirs: list[Path] = []

    for index, step in enumerate(steps, start=1):
        _print_step_header(index, len(steps), step)
        dependency_skip_reason = _dependency_skip_reason(step, non_actionable_assets)
        if dependency_skip_reason is not None:
            _skip_step_for_dependency(
                args=args,
                workflow_report=workflow_report,
                step=step,
                reason=dependency_skip_reason,
            )
            continue
        if _skip_step_for_gate(
            args=args,
            workflow_report=workflow_report,
            step=step,
            gate=gate,
        ):
            continue
        result = _run(step.command, dry_run=args.dry_run)
        if result.returncode != 0:
            if result.returncode in step.nonfatal_returncodes:
                _record_nonfatal_step_result(
                    args=args,
                    workflow_report=workflow_report,
                    step=step,
                    result=result,
                    non_actionable_assets=non_actionable_assets,
                )
                continue
            raise SystemExit(result.returncode)

        _record_patch_merge_success(step, successful_patch_merge_dirs)
        _handle_alias_after_step(
            args=args,
            workflow_report=workflow_report,
            step=step,
            gate=gate,
        )
        active_bundle = _update_active_bundle(active_bundle, step)
        _handle_inspect_summary_after_step(
            args=args,
            workflow_report=workflow_report,
            step=step,
            gate=gate,
        )
        if not args.dry_run:
            _record_step_report(workflow_report, step=step, result=result)

    return _WorkflowExecutionResult(
        gate_triggered=gate.triggered,
        successful_patch_merge_dirs=successful_patch_merge_dirs,
    )


def _write_repair_queue_reports(
    *,
    args: argparse.Namespace,
    repair_steps: list[Step],
    workflow_report: dict[str, Any],
) -> None:
    if args.dry_run or not repair_steps:
        return

    repair_queue_path = _default_repair_queue_path(args.target_date)
    repair_queue_payload = _build_repair_candidate_payload(
        args=args,
        source_report=args.repair_source_report,
        source_kind=_repair_source_kind(only_unresolved=args.repair_only_unresolved),
        candidates_by_asset=_repair_candidates_from_steps(repair_steps),
        report_path=repair_queue_path,
    )
    _write_json_report(repair_queue_path, payload=repair_queue_payload)
    workflow_report.setdefault("repair", {})["queue"] = repair_queue_payload
    if args.repair_rerun_inspect:
        remaining_path = _default_remaining_repair_candidates_path(args.target_date)
        remaining_payload = _build_remaining_repair_candidates_payload(
            args=args,
            workflow_report=workflow_report,
            report_path=remaining_path,
        )
        if remaining_payload is not None:
            _write_json_report(remaining_path, payload=remaining_payload)
            workflow_report.setdefault("repair", {})["remaining_candidates"] = remaining_payload


def _write_current_asset_contracts(
    *,
    args: argparse.Namespace,
    workflow_report: dict[str, Any],
) -> None:
    if args.dry_run:
        return
    current_contract_path = default_hk_current_contract_path(ASSETS_ROOT.parent)
    dataset_registry_path = default_dataset_registry_path(ASSETS_ROOT.parent)
    current_contract_payload = build_hk_current_contract(
        ASSETS_ROOT.parent,
        generated_by="hk_asset_workflow",
        target_date=args.target_date,
    )
    write_current_contract(current_contract_path, current_contract_payload)
    write_dataset_registry(dataset_registry_path, current_contract_payload)
    workflow_report.setdefault("workflow", {})["current_contract_path"] = str(current_contract_path)
    workflow_report.setdefault("workflow", {})["dataset_registry_path"] = str(dataset_registry_path)


def _finalize_workflow_outputs(
    *,
    args: argparse.Namespace,
    phases: tuple[str, ...],
    workflow_report: dict[str, Any],
    gate_triggered: bool,
    successful_patch_merge_dirs: list[Path],
) -> int:
    if not args.dry_run and args.prune_successful_patches:
        _prune_successful_patch_dirs(
            successful_patch_merge_dirs,
            report=workflow_report,
        )

    _write_current_asset_contracts(args=args, workflow_report=workflow_report)

    if not args.dry_run and args.workflow_report is not None:
        _write_workflow_report(args.workflow_report, report=workflow_report)
        print(f"Workflow report: {_repo_relative(args.workflow_report)}")

    if gate_triggered:
        print("Workflow gate triggered:", f"threshold={args.gate_on_severity}")
        return 2

    print(
        "Workflow complete:",
        f"phases={','.join(phases)}",
        f"target_date={args.target_date}",
        f"package_dest={_repo_relative(args.package_dest)}",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _normalize_workflow_args(args)

    phases = _phase_selection(args)
    if "repair" in phases and not args.repair_source_report.exists() and "inspect" in phases:
        raise SystemExit(
            "Repair requires an existing workflow report with inspect.assets.repair_candidates. "
            "Run the workflow once with inspect enabled, then rerun with --phase repair."
        )
    workflow_report = _init_workflow_report(args=args, phases=phases)
    current = _current_snapshot_bundle()
    refreshed = _refreshed_snapshot_bundle(args.target_date)
    active_bundle = current
    plan = _build_workflow_plan(
        args,
        phases=phases,
        current=current,
        refreshed=refreshed,
        active_bundle=active_bundle,
    )
    steps = plan.steps

    if not steps:
        print("No steps selected.")
        return 0

    _ensure_workflow_output_dirs(args)
    execution = _run_workflow_steps(
        args=args,
        phases=phases,
        steps=steps,
        workflow_report=workflow_report,
        active_bundle=active_bundle,
    )
    _write_repair_queue_reports(
        args=args,
        repair_steps=plan.repair_steps,
        workflow_report=workflow_report,
    )
    return _finalize_workflow_outputs(
        args=args,
        phases=phases,
        workflow_report=workflow_report,
        gate_triggered=execution.gate_triggered,
        successful_patch_merge_dirs=execution.successful_patch_merge_dirs,
    )


if __name__ == "__main__":
    raise SystemExit(main())
