from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .hk_asset_workflow_paths import Step

REPAIR_SEVERITY_RANK = {"error": 2, "warning": 1, "info": 0}
GATE_SEVERITY_RANK = {"none": -1, **REPAIR_SEVERITY_RANK}


def workflow_gate_enabled(
    *,
    phases: tuple[str, ...],
    threshold: str,
    repair_rerun_inspect: bool,
) -> bool:
    return (
        threshold != "none"
        and ("inspect" in phases or ("repair" in phases and repair_rerun_inspect))
        and any(phase in phases for phase in ("refresh", "repair", "package", "release"))
    )


def health_summary_hits_gate(summary: Mapping[str, Any], *, threshold: str) -> bool:
    overall = str(summary.get("overall_severity") or "none").strip().lower() or "none"
    return GATE_SEVERITY_RANK.get(overall, -1) >= GATE_SEVERITY_RANK[threshold]


def init_workflow_report(
    *,
    target_date: str,
    refresh_mode: str,
    phases: tuple[str, ...],
    selected_refresh_assets: Sequence[str],
    selected_inspect_assets: Sequence[str],
    selected_repair_assets: Sequence[str],
    selected_parts: Sequence[str],
    inspect_fail_on_severity: str,
    gate_on_severity: str,
    repair_rerun_inspect: bool,
    repair_only_unresolved: bool,
    repair_min_severity: str,
    repair_source_report: Path | None,
) -> dict[str, Any]:
    return {
        "workflow": {
            "target_date": target_date,
            "refresh_mode": refresh_mode,
            "phases": list(phases),
            "selected_refresh_assets": list(selected_refresh_assets),
            "selected_inspect_assets": list(selected_inspect_assets),
            "selected_repair_assets": list(selected_repair_assets),
            "selected_parts": list(selected_parts),
            "inspect_fail_on_severity": inspect_fail_on_severity,
            "gate_on_severity": gate_on_severity,
            "repair_rerun_inspect": repair_rerun_inspect,
            "repair_only_unresolved": repair_only_unresolved,
            "repair_min_severity": repair_min_severity,
            "repair_source_report": str(repair_source_report) if repair_source_report else None,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
        "refresh": {
            "assets": {},
        },
        "inspect": {
            "assets": {},
        },
        "repair": {
            "assets": {},
            "queue": None,
            "remaining_candidates": None,
        },
        "gate": {
            "enabled": workflow_gate_enabled(
                phases=phases,
                threshold=gate_on_severity,
                repair_rerun_inspect=repair_rerun_inspect,
            ),
            "threshold": gate_on_severity,
            "stage": None,
            "triggered": False,
            "triggered_assets": [],
            "blocked_alias_updates": [],
            "skipped_steps": [],
        },
        "steps": [],
    }


def write_workflow_report(path: Path, *, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report.setdefault("workflow", {})["finished_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_json_report(path: Path, *, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def record_gate_trigger(
    report: dict[str, Any],
    *,
    step: Step,
    summary: Mapping[str, Any],
) -> None:
    gate = report.setdefault("gate", {})
    gate["triggered"] = True
    triggered_assets = gate.setdefault("triggered_assets", [])
    entry = {
        "asset_name": step.asset_name,
        "overall_severity": summary.get("overall_severity"),
        "severity_counts": dict(summary.get("severity_counts") or {}),
        "report_path": summary.get("report_path"),
    }
    if entry not in triggered_assets:
        triggered_assets.append(entry)


def record_blocked_alias_update(
    report: dict[str, Any],
    *,
    step: Step,
    reason: str,
) -> None:
    if step.alias_target is None or step.alias_link is None:
        return
    blocked = report.setdefault("gate", {}).setdefault("blocked_alias_updates", [])
    entry = {
        "phase": step.phase,
        "asset_name": step.asset_name,
        "alias_path": str(step.alias_link),
        "target_path": str(step.alias_target),
        "reason": reason,
    }
    if entry not in blocked:
        blocked.append(entry)


def record_skipped_step(
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
    skipped = report.setdefault("gate", {}).setdefault("skipped_steps", [])
    entry = {
        "phase": step.phase,
        "label": step.label,
        "asset_name": step.asset_name,
        "reason": reason,
    }
    if entry not in skipped:
        skipped.append(entry)
