from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .hk_asset_workflow_paths import Step
from .hk_asset_workflow_report import (
    GATE_SEVERITY_RANK,
    REPAIR_SEVERITY_RANK,
    health_summary_hits_gate,
)


def load_health_report_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    quality_checks = (
        payload.get("quality_checks") if isinstance(payload.get("quality_checks"), list) else []
    )
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


def load_health_report_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Health report payload must be a JSON object: {path}")
    return payload


def gate_relevant_quality_checks(
    payload: Mapping[str, Any],
    *,
    threshold: str,
) -> list[dict[str, Any]]:
    quality_checks = (
        payload.get("quality_checks") if isinstance(payload.get("quality_checks"), list) else []
    )
    threshold_rank = GATE_SEVERITY_RANK[threshold]
    relevant: list[dict[str, Any]] = []
    for item in quality_checks:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").strip().lower() or "info"
        if GATE_SEVERITY_RANK.get(severity, -1) >= threshold_rank:
            relevant.append(item)
    return relevant


def build_gate_quality_summary(path: Path, *, threshold: str) -> dict[str, Any]:
    payload = load_health_report_payload(path)
    relevant_checks = gate_relevant_quality_checks(payload, threshold=threshold)
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


def suppress_gate_hits_for_clean_daily_consumer_path(
    gate_results: list[tuple[Step, dict[str, Any]]],
    *,
    threshold: str,
    report: dict[str, Any],
) -> list[tuple[Step, dict[str, Any]]]:
    if not gate_results:
        return gate_results

    by_asset = {
        str(step.asset_name or ""): summary for step, summary in gate_results if step.asset_name
    }
    daily_summary = by_asset.get("daily")
    daily_clean_summary = by_asset.get("daily_clean")
    if daily_summary is None or daily_clean_summary is None:
        return gate_results
    diagnostic_checks = _daily_raw_diagnostic_gate_checks(daily_summary)
    if not diagnostic_checks:
        return gate_results
    if health_summary_hits_gate(daily_clean_summary, threshold=threshold):
        return gate_results

    if diagnostic_checks == {"daily_price_bounds_violation"}:
        reason = "raw daily price-bounds-only issues are tolerated when daily_clean passes the gate"
    else:
        reason = (
            "raw daily diagnostic data-quality issues are tolerated when "
            "daily_clean passes the gate"
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
    if REPAIR_SEVERITY_RANK.get(severity_text, -1) > REPAIR_SEVERITY_RANK.get(
        entry["max_severity"], -1
    ):
        entry["max_severity"] = severity_text


def extract_health_repair_candidates(
    *,
    payload: Mapping[str, Any],
    asset_name: str | None,
) -> list[dict[str, Any]]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    target_date = str(summary.get("target_date") or "").strip() or None
    field_rows = (
        payload.get("field_coverage") if isinstance(payload.get("field_coverage"), list) else []
    )
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
            str(item).strip() for item in (issue.get("sample_symbols") or []) if str(item).strip()
        ]
        if not sample_symbols:
            continue
        field_row = field_map.get(field or "")
        detail_key = None
        if (
            "provider_like_ffill" in check
            or check == "field_all_clean_missing_on_target_date_provider_like"
        ):
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


def load_health_report_analysis(path: Path, *, asset_name: str | None) -> dict[str, Any]:
    payload = load_health_report_payload(path)
    return {
        "quality": load_health_report_summary(path),
        "repair_candidates": extract_health_repair_candidates(
            payload=payload, asset_name=asset_name
        ),
    }
