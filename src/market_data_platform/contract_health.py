from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from market_data_platform.contract import build_current_contract
from market_data_platform.paths import (
    current_contract_path,
    normalize_market,
    resolve_artifacts_root,
)

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_FAIL_ON_SEVERITIES = ("none", "info", "warning", "error")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _normalize_fail_on_severity(value: object) -> str:
    text = str(value or "none").strip().lower()
    if not text:
        return "none"
    if text not in _FAIL_ON_SEVERITIES:
        raise SystemExit("fail_on_severity must be one of: none, info, warning, error.")
    return text


def _format_quality_check_label(row: Mapping[str, object]) -> str:
    check = str(row.get("check") or "").strip() or "unknown_check"
    field = str(row.get("field") or "").strip()
    return f"{check} [{field}]" if field else check


def _summarize_quality_checks(
    quality_checks: Sequence[Mapping[str, object]] | None,
    *,
    fail_on_severity: object = "none",
) -> dict[str, object]:
    threshold = _normalize_fail_on_severity(fail_on_severity)
    severity_counts: Counter[str] = Counter()
    failing_labels: list[str] = []
    max_rank = -1
    max_severity = "none"

    for row in quality_checks or []:
        if not isinstance(row, Mapping):
            continue
        severity = str(row.get("severity") or "info").strip().lower()
        if severity not in _SEVERITY_RANK:
            severity = "info"
        severity_counts[severity] += 1
        severity_rank = _SEVERITY_RANK[severity]
        if severity_rank > max_rank:
            max_rank = severity_rank
            max_severity = severity
        if threshold != "none" and severity_rank >= _SEVERITY_RANK.get(threshold, 99):
            label = _format_quality_check_label(row)
            if label not in failing_labels and len(failing_labels) < 5:
                failing_labels.append(label)

    issue_count = int(sum(severity_counts.values()))
    severity_counts_dict = {
        "error": int(severity_counts.get("error", 0)),
        "warning": int(severity_counts.get("warning", 0)),
        "info": int(severity_counts.get("info", 0)),
    }
    failing_issue_count = 0
    if threshold != "none":
        threshold_rank = _SEVERITY_RANK[threshold]
        failing_issue_count = int(
            sum(
                count
                for severity, count in severity_counts_dict.items()
                if _SEVERITY_RANK[severity] >= threshold_rank
            )
        )
    gate_triggered = failing_issue_count > 0
    overall_severity = max_severity if issue_count else "none"
    if issue_count <= 0:
        color = "green"
        message = "No quality issues detected."
    elif threshold != "none" and gate_triggered:
        color = "red" if severity_counts_dict["error"] else "yellow"
        message = (
            f"{failing_issue_count} quality issue(s) met fail_on_severity={threshold}; "
            "the inspection gate was triggered."
        )
    elif threshold != "none":
        color = "red" if overall_severity == "error" else "yellow"
        message = f"{issue_count} quality issue(s) detected; none met fail_on_severity={threshold}."
    else:
        color = "red" if overall_severity == "error" else "yellow"
        message = f"{issue_count} quality issue(s) detected; max_severity={overall_severity}."

    return {
        "color": color,
        "overall_severity": overall_severity,
        "issue_count": issue_count,
        "severity_counts": severity_counts_dict,
        "fail_on_severity": threshold,
        "gate_triggered": gate_triggered,
        "gate_status": "fail" if gate_triggered else "pass",
        "failing_issue_count": failing_issue_count,
        "sample_failing_checks": failing_labels,
        "message": message,
    }


def _quality_gate_exit_code(quality_verdict: Mapping[str, object] | None) -> int:
    if isinstance(quality_verdict, Mapping) and bool(quality_verdict.get("gate_triggered")):
        return 2
    return 0


def _append_quality_verdict_lines(
    lines: list[str],
    quality_verdict: Mapping[str, object] | None,
) -> None:
    if not isinstance(quality_verdict, Mapping):
        return
    lines.append("")
    lines.append("Quality Verdict")
    for key in ("color", "overall_severity", "issue_count", "gate_status", "fail_on_severity"):
        lines.append(f"{key}: {quality_verdict.get(key)}")
    severity_counts = quality_verdict.get("severity_counts")
    if isinstance(severity_counts, Mapping):
        lines.append(
            "severity_counts: "
            f"error={int(severity_counts.get('error', 0))}, "
            f"warning={int(severity_counts.get('warning', 0))}, "
            f"info={int(severity_counts.get('info', 0))}"
        )
    lines.append(f"gate_triggered: {bool(quality_verdict.get('gate_triggered'))}")
    message = quality_verdict.get("message")
    if message:
        lines.append(f"message: {message}")


def _normalize_asset_keys(
    selected_assets: Sequence[str] | None,
    *,
    known_asset_keys: Sequence[str],
) -> list[str]:
    if not selected_assets:
        return list(known_asset_keys)
    known = set(known_asset_keys)
    normalized: list[str] = []
    for asset in selected_assets:
        key = str(asset or "").strip()
        if not key:
            continue
        if key not in known:
            available = ", ".join(sorted(known))
            raise SystemExit(f"Unknown asset key '{key}'. Available assets: {available}.")
        if key not in normalized:
            normalized.append(key)
    return normalized


def _load_contract(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Contract is not a JSON object: {path}")
    return payload


def _asset_as_of_lags(*, as_of: str | None, target_date: str | None) -> bool:
    if not as_of or not target_date:
        return False
    return as_of < target_date


def _asset_record(asset_key: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _mapping(entry.get("manifest"))
    manifest_status = str(manifest.get("status") or "").strip() or None
    return {
        "asset_key": asset_key,
        "alias_path": entry.get("alias_path"),
        "exists": bool(entry.get("exists")),
        "is_symlink": bool(entry.get("is_symlink")),
        "path_kind": entry.get("path_kind"),
        "resolved_path": entry.get("resolved_path"),
        "manifest_path": entry.get("manifest_path"),
        "manifest_status": manifest_status,
        "as_of": str(entry.get("as_of") or "").strip() or None,
    }


def _build_quality_checks(
    *,
    contract_exists: bool,
    current_contract: Path,
    target_date: str | None,
    asset_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not contract_exists:
        checks.append(
            {
                "check": "current_contract_missing",
                "field": "contract",
                "asset_key": "contract",
                "severity": "error",
                "current_contract_path": str(current_contract),
            }
        )
    for record in asset_records:
        asset_key = str(record.get("asset_key") or "")
        if not bool(record.get("exists")):
            checks.append(
                {
                    "check": "current_asset_missing",
                    "field": asset_key,
                    "asset_key": asset_key,
                    "severity": "warning",
                    "alias_path": record.get("alias_path"),
                }
            )
            continue
        manifest_status = str(record.get("manifest_status") or "").strip()
        if manifest_status and manifest_status != "completed":
            checks.append(
                {
                    "check": "asset_manifest_status_not_completed",
                    "field": asset_key,
                    "asset_key": asset_key,
                    "severity": "warning",
                    "status": manifest_status,
                }
            )
        as_of = str(record.get("as_of") or "").strip() or None
        if _asset_as_of_lags(as_of=as_of, target_date=target_date):
            checks.append(
                {
                    "check": "asset_as_of_lagging_target_date",
                    "field": asset_key,
                    "asset_key": asset_key,
                    "severity": "warning",
                    "actual_as_of": as_of,
                    "expected_target_date": target_date,
                }
            )
    return checks


def inspect_current_contract(
    artifacts_root: str | Path | None = None,
    *,
    market: str | None = None,
    provider: str | None = None,
    current_contract: str | Path | None = None,
    target_date: str | None = None,
    assets: Sequence[str] | None = None,
    fail_on_severity: str = "none",
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    market_name = normalize_market(market)
    contract_path = (
        Path(current_contract).expanduser().resolve()
        if current_contract is not None
        else current_contract_path(root, market=market_name)
    )
    payload = _load_contract(contract_path)
    contract_exists = payload is not None
    if payload is None:
        payload = build_current_contract(root, market=market_name, provider=provider)
    contract_meta = _mapping(payload.get("contract"))
    contract_assets = _mapping(payload.get("assets"))
    known_asset_keys = list(contract_assets.keys())
    selected_assets = _normalize_asset_keys(assets, known_asset_keys=known_asset_keys)
    asset_records = [
        _asset_record(asset_key, _mapping(contract_assets.get(asset_key)))
        for asset_key in selected_assets
    ]
    effective_target_date = (
        str(target_date or contract_meta.get("target_date") or "").strip() or None
    )
    quality_checks = _build_quality_checks(
        contract_exists=contract_exists,
        current_contract=contract_path,
        target_date=effective_target_date,
        asset_records=asset_records,
    )
    quality_verdict = _summarize_quality_checks(
        quality_checks,
        fail_on_severity=fail_on_severity,
    )
    return {
        "summary": {
            "artifacts_root": str(root),
            "current_contract_path": str(contract_path),
            "contract_exists": contract_exists,
            "contract_name": contract_meta.get("name"),
            "market": contract_meta.get("market") or market_name,
            "provider": contract_meta.get("provider") or provider,
            "contract_target_date": contract_meta.get("target_date"),
            "target_date": effective_target_date,
            "assets_checked": len(asset_records),
            "missing_assets": sum(1 for record in asset_records if not record["exists"]),
            "stale_assets": sum(
                1
                for record in asset_records
                if _asset_as_of_lags(
                    as_of=str(record.get("as_of") or "").strip() or None,
                    target_date=effective_target_date,
                )
            ),
            "assets_with_manifest": sum(
                1 for record in asset_records if record.get("manifest_path")
            ),
        },
        "quality_verdict": quality_verdict,
        "quality_checks": quality_checks,
        "assets": {str(record["asset_key"]): dict(record) for record in asset_records},
    }


def render_current_contract_health_text(payload: Mapping[str, Any]) -> str:
    summary = _mapping(payload.get("summary"))
    assets = _mapping(payload.get("assets"))
    lines = [
        "Current Contract Health",
        f"current_contract_path: {summary.get('current_contract_path')}",
        f"contract_exists: {summary.get('contract_exists')}",
        f"market: {summary.get('market')}",
        f"provider: {summary.get('provider')}",
        f"target_date: {summary.get('target_date')}",
        f"assets_checked: {summary.get('assets_checked')}",
        f"missing_assets: {summary.get('missing_assets')}",
        f"stale_assets: {summary.get('stale_assets')}",
    ]
    lines.append("")
    lines.append("Assets")
    for asset_key, raw_record in assets.items():
        record = _mapping(raw_record)
        lines.append(
            f"- {asset_key}: exists={record.get('exists')}, "
            f"as_of={record.get('as_of')}, "
            f"status={record.get('manifest_status')}, "
            f"path_kind={record.get('path_kind')}"
        )
    _append_quality_verdict_lines(lines, _mapping(payload.get("quality_verdict")))
    return "\n".join(lines) + "\n"


def write_current_contract_health_report(
    payload: Mapping[str, Any],
    *,
    output: str | Path | None,
    output_format: str,
) -> None:
    rendered = (
        json.dumps(dict(payload), ensure_ascii=False, indent=2)
        if output_format == "json"
        else render_current_contract_health_text(payload)
    )
    if output is None:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
        return
    out_path = Path(output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")


def current_contract_health_exit_code(payload: Mapping[str, Any]) -> int:
    return _quality_gate_exit_code(_mapping(payload.get("quality_verdict")))
