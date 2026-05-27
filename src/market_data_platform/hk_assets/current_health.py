from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from market_data_platform.current_assets import (
    build_hk_current_contract,
    default_hk_current_contract_path,
    describe_current_path,
    hk_current_candidate_paths,
    load_current_contract,
)
from .health_shared import format_date as _format_date, parse_compact_date as _parse_compact_date
from .quality_gate import (
    append_quality_verdict_lines,
    quality_gate_exit_code,
    summarize_quality_checks,
)


CURRENT_HEALTH_POLICY: dict[str, dict[str, Any]] = {
    "daily": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "daily_clean": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "intraday": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "tick_depth_raw": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 7,
        "cadence": "microstructure_sample",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "tick_depth_daily": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 7,
        "cadence": "microstructure_sample",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "execution_cost_model": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 45,
        "cadence": "calibration_window",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "etf_daily": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed", "completed_with_failures"},
        "require_manifest": True,
    },
    "etf_daily_clean": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "etf_instruments": {
        "required": False,
        "stale_severity": "warning",
        "allowed_statuses": set(),
        "require_manifest": False,
    },
    "valuation": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "instruments": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": set(),
        "require_manifest": False,
    },
    "pit": {
        "required": True,
        "stale_severity": "warning",
        "allowed_lag_days": 45,
        "cadence": "filing_asof",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "ex_factors": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "event_or_trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "dividends": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "event_or_trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "shares": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "event_or_trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "exchange_rate": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 7,
        "cadence": "reference_rate",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "southbound": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "financial_details": {
        "required": False,
        "stale_severity": "warning",
        "allowed_lag_days": 45,
        "cadence": "filing_asof",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "industry_changes": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "event_or_trading_day",
        "allowed_statuses": {"completed"},
        "require_manifest": True,
    },
    "universe_by_date": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": set(),
        "require_manifest": False,
    },
    "universe_symbols": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": set(),
        "require_manifest": False,
    },
    "universe_meta": {
        "required": True,
        "stale_severity": "error",
        "allowed_lag_days": 0,
        "cadence": "trading_day",
        "allowed_statuses": set(),
        "require_manifest": False,
    },
}


def _normalize_asset_keys(
    values: Sequence[str] | None,
    *,
    known_asset_keys: Sequence[str],
) -> list[str]:
    known = list(dict.fromkeys(str(item) for item in known_asset_keys))
    if not values:
        return known
    requested: list[str] = []
    unknown: list[str] = []
    known_set = set(known)
    for raw in values:
        key = str(raw or "").strip()
        if not key:
            continue
        if key in known_set:
            if key not in requested:
                requested.append(key)
            continue
        unknown.append(key)
    if unknown:
        raise SystemExit(
            "Unknown HK current asset key(s): "
            + ", ".join(sorted(unknown))
            + ". Available: "
            + ", ".join(known)
        )
    return requested


def _detect_date_token(value: object | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(\d{8})", text)
    return match.group(1) if match else None


def _normalize_date_text(value: object | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _parse_compact_date(text, label="date").strftime("%Y%m%d")
    except SystemExit:
        compact = _detect_date_token(text)
        if compact:
            return compact
        formatted = _format_date(text)
        if formatted:
            return formatted.replace("-", "")
        return None


def _parse_optional_date(value: str | None) -> datetime | None:
    text = _normalize_date_text(value)
    if not text:
        return None
    return datetime.strptime(text, "%Y%m%d")


def _format_gap_days(actual: str | None, expected: str | None) -> int | None:
    actual_dt = _parse_optional_date(actual)
    expected_dt = _parse_optional_date(expected)
    if actual_dt is None or expected_dt is None:
        return None
    return (expected_dt - actual_dt).days


def _load_yaml_mapping(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, Mapping) else None


def _load_universe_meta_summary(path: Path | None) -> dict[str, Any] | None:
    payload = _load_yaml_mapping(path)
    if payload is None:
        return None
    settings = payload.get("settings") if isinstance(payload.get("settings"), Mapping) else {}
    build = payload.get("build") if isinstance(payload.get("build"), Mapping) else {}
    source_daily_asset_dir = str(settings.get("daily_asset_dir") or build.get("asset_dir") or "").strip() or None
    last_rebalance_date = _normalize_date_text(build.get("last_rebalance_date"))
    last_trade_date = _normalize_date_text(build.get("last_trade_date"))
    settings_end_date = _normalize_date_text(settings.get("end_date"))
    effective_as_of = last_rebalance_date or settings_end_date or last_trade_date or _detect_date_token(source_daily_asset_dir)
    return {
        "path": str(path),
        "last_rebalance_date": last_rebalance_date,
        "last_trade_date": last_trade_date,
        "settings_end_date": settings_end_date,
        "source_daily_asset_dir": source_daily_asset_dir,
        "source_daily_asset_as_of": _detect_date_token(source_daily_asset_dir),
        "effective_as_of": effective_as_of,
    }


def _resolve_target_date(
    *,
    explicit: str | None,
    contract_payload: Mapping[str, Any] | None,
    asset_records: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str]:
    explicit_text = _normalize_date_text(explicit)
    if explicit_text:
        return explicit_text, "explicit"
    contract = contract_payload.get("contract") if isinstance(contract_payload, Mapping) else None
    if isinstance(contract, Mapping):
        contract_target = _normalize_date_text(contract.get("target_date"))
        if contract_target:
            return contract_target, "current_contract.target_date"
    asset_dates = [
        str(record.get("effective_as_of") or "").strip()
        for record in asset_records
        if str(record.get("effective_as_of") or "").strip()
    ]
    if asset_dates:
        return max(asset_dates), "max_asset_as_of"
    return None, "unresolved"


def _asset_policy(asset_key: str) -> dict[str, Any]:
    policy = CURRENT_HEALTH_POLICY.get(asset_key, {})
    return {
        "required": bool(policy.get("required", False)),
        "stale_severity": str(policy.get("stale_severity") or "warning"),
        "allowed_lag_days": max(0, int(policy.get("allowed_lag_days", 0) or 0)),
        "cadence": str(policy.get("cadence") or "unspecified"),
        "allowed_statuses": set(policy.get("allowed_statuses") or []),
        "require_manifest": bool(policy.get("require_manifest", False)),
    }


def _asset_lag_days(
    *,
    effective_as_of: str | None,
    target_date: str | None,
) -> int | None:
    if not effective_as_of or not target_date:
        return None
    return _format_gap_days(effective_as_of, target_date)


def _asset_lag_exceeds_policy(
    *,
    effective_as_of: str | None,
    target_date: str | None,
    policy: Mapping[str, Any],
) -> bool:
    if not effective_as_of or not target_date or effective_as_of >= target_date:
        return False
    gap_days = _asset_lag_days(effective_as_of=effective_as_of, target_date=target_date)
    if gap_days is None:
        return True
    return gap_days > int(policy.get("allowed_lag_days") or 0)


def _resolve_asset_record(
    *,
    asset_key: str,
    contract_assets: Mapping[str, Any],
    candidate_paths: Mapping[str, Path],
    universe_meta_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contract_entry = contract_assets.get(asset_key) if isinstance(contract_assets, Mapping) else None
    alias_path_text = None
    if isinstance(contract_entry, Mapping):
        alias_path_text = str(contract_entry.get("alias_path") or "").strip() or None
    alias_path = Path(alias_path_text) if alias_path_text else candidate_paths.get(asset_key)
    if alias_path is None:
        raise SystemExit(f"Could not resolve alias path for current asset: {asset_key}")
    live = describe_current_path(alias_path)
    record = {
        "asset_key": asset_key,
        "alias_path": live.get("alias_path"),
        "resolved_path": live.get("resolved_path"),
        "exists": bool(live.get("exists")),
        "is_symlink": bool(live.get("is_symlink")),
        "path_kind": live.get("path_kind"),
        "manifest_path": live.get("manifest_path"),
        "manifest": live.get("manifest"),
        "resolved_name": live.get("resolved_name"),
        "as_of": live.get("as_of"),
        "effective_as_of": live.get("as_of"),
        "effective_as_of_source": "asset_path_or_manifest",
        "freshness_policy": {
            "cadence": _asset_policy(asset_key)["cadence"],
            "allowed_lag_days": _asset_policy(asset_key)["allowed_lag_days"],
        },
    }
    if asset_key in {"universe_by_date", "universe_symbols", "universe_meta"} and isinstance(
        universe_meta_summary,
        Mapping,
    ):
        effective_as_of = str(universe_meta_summary.get("effective_as_of") or "").strip() or None
        if effective_as_of:
            record["effective_as_of"] = effective_as_of
            record["effective_as_of_source"] = "universe_meta"
        record["universe_meta"] = dict(universe_meta_summary)
    return record


def _asset_issue_summary(quality_checks: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    severity_rank = {"none": -1, "info": 0, "warning": 1, "error": 2}
    out: dict[str, dict[str, Any]] = {}
    for item in quality_checks:
        if not isinstance(item, Mapping):
            continue
        asset_key = str(item.get("asset_key") or item.get("field") or "").strip()
        if not asset_key:
            continue
        severity = str(item.get("severity") or "info").strip().lower() or "info"
        check = str(item.get("check") or "").strip()
        entry = out.setdefault(asset_key, {"overall_severity": "none", "checks": []})
        if check and check not in entry["checks"]:
            entry["checks"].append(check)
        if severity_rank.get(severity, -1) > severity_rank.get(entry["overall_severity"], -1):
            entry["overall_severity"] = severity
    return out


def _build_quality_checks(
    *,
    contract_exists: bool,
    current_contract_path: Path,
    target_date: str | None,
    asset_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    quality_checks: list[dict[str, Any]] = []
    if not contract_exists:
        quality_checks.append(
            {
                "check": "current_contract_missing",
                "field": "contract",
                "asset_key": "contract",
                "severity": "error",
                "current_contract_path": str(current_contract_path),
            }
        )

    for record in asset_records:
        asset_key = str(record.get("asset_key") or "")
        policy = _asset_policy(asset_key)
        manifest = record.get("manifest") if isinstance(record.get("manifest"), Mapping) else {}
        manifest_status = str(manifest.get("status") or "").strip() or None
        effective_as_of = str(record.get("effective_as_of") or "").strip() or None

        if not bool(record.get("exists")):
            quality_checks.append(
                {
                    "check": "current_asset_missing",
                    "field": asset_key,
                    "asset_key": asset_key,
                    "severity": "error" if policy["required"] else "warning",
                    "alias_path": record.get("alias_path"),
                }
            )
            continue

        if policy["require_manifest"] and not record.get("manifest_path"):
            quality_checks.append(
                {
                    "check": "asset_manifest_missing",
                    "field": asset_key,
                    "asset_key": asset_key,
                    "severity": "error" if policy["required"] else "warning",
                    "resolved_path": record.get("resolved_path"),
                }
            )

        allowed_statuses = set(policy["allowed_statuses"])
        if manifest_status and allowed_statuses and manifest_status not in allowed_statuses:
            quality_checks.append(
                {
                    "check": "asset_manifest_status_not_healthy",
                    "field": asset_key,
                    "asset_key": asset_key,
                    "severity": "warning" if policy["required"] else "info",
                    "status": manifest_status,
                    "allowed_statuses": sorted(allowed_statuses),
                }
            )

        if target_date:
            if not effective_as_of:
                quality_checks.append(
                    {
                        "check": "asset_as_of_missing",
                        "field": asset_key,
                        "asset_key": asset_key,
                        "severity": "warning" if policy["required"] else "info",
                    }
                )
            elif _asset_lag_exceeds_policy(
                effective_as_of=effective_as_of,
                target_date=target_date,
                policy=policy,
            ):
                quality_checks.append(
                    {
                        "check": "asset_as_of_lagging_target_date",
                        "field": asset_key,
                        "asset_key": asset_key,
                        "severity": policy["stale_severity"],
                        "actual_as_of": effective_as_of,
                        "expected_target_date": target_date,
                        "gap_days": _asset_lag_days(
                            effective_as_of=effective_as_of,
                            target_date=target_date,
                        ),
                        "allowed_lag_days": policy["allowed_lag_days"],
                        "freshness_cadence": policy["cadence"],
                    }
                )

        if asset_key == "universe_meta":
            universe_meta = record.get("universe_meta") if isinstance(record.get("universe_meta"), Mapping) else {}
            source_daily_asset_as_of = str(universe_meta.get("source_daily_asset_as_of") or "").strip() or None
            if target_date and source_daily_asset_as_of and source_daily_asset_as_of < target_date:
                quality_checks.append(
                    {
                        "check": "universe_source_daily_asset_lagging_target_date",
                        "field": asset_key,
                        "asset_key": asset_key,
                        "severity": "warning",
                        "actual_as_of": source_daily_asset_as_of,
                        "expected_target_date": target_date,
                        "gap_days": _format_gap_days(source_daily_asset_as_of, target_date),
                        "source_daily_asset_dir": universe_meta.get("source_daily_asset_dir"),
                    }
                )
    return quality_checks


def _render_current_health_text(payload: Mapping[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    assets = payload.get("assets") if isinstance(payload.get("assets"), Mapping) else {}
    asset_issues = _asset_issue_summary(
        payload.get("quality_checks") if isinstance(payload.get("quality_checks"), Sequence) else []
    )

    lines = [
        "HK Current Health",
        f"current_contract_path: {summary.get('current_contract_path')}",
        f"contract_exists: {summary.get('contract_exists')}",
        f"target_date: {summary.get('target_date')}",
        f"target_date_source: {summary.get('target_date_source')}",
        f"assets_checked: {summary.get('assets_checked')}",
        f"required_assets: {summary.get('required_assets')}",
        f"optional_assets: {summary.get('optional_assets')}",
        f"stale_assets: {summary.get('stale_assets')}",
        f"missing_assets: {summary.get('missing_assets')}",
    ]
    if summary.get("contract_generated_at"):
        lines.append(f"contract_generated_at: {summary.get('contract_generated_at')}")
    if summary.get("contract_generated_by"):
        lines.append(f"contract_generated_by: {summary.get('contract_generated_by')}")

    lines.append("")
    lines.append("Assets")
    for asset_key, record in assets.items():
        if not isinstance(record, Mapping):
            continue
        issue = asset_issues.get(str(asset_key), {"overall_severity": "none", "checks": []})
        status = (
            record.get("manifest", {}).get("status")
            if isinstance(record.get("manifest"), Mapping)
            else None
        )
        line = (
            f"- {asset_key}: severity={issue.get('overall_severity')}, "
            f"exists={record.get('exists')}, "
            f"as_of={record.get('effective_as_of')}, "
            f"status={status}"
        )
        if issue.get("checks"):
            line += ", checks=" + ",".join(str(item) for item in issue["checks"])
        lines.append(line)
        universe_meta = record.get("universe_meta")
        if isinstance(universe_meta, Mapping):
            lines.append(
                "  "
                + f"last_rebalance_date={universe_meta.get('last_rebalance_date')}, "
                + f"source_daily_asset_as_of={universe_meta.get('source_daily_asset_as_of')}"
            )

    append_quality_verdict_lines(
        lines,
        payload.get("quality_verdict") if isinstance(payload.get("quality_verdict"), Mapping) else None,
    )
    return "\n".join(lines) + "\n"


def inspect_hk_current_health(args) -> int:
    artifacts_root = Path(getattr(args, "artifacts_root", "artifacts")).expanduser().resolve()
    current_contract_path = (
        Path(args.current_contract).expanduser().resolve()
        if getattr(args, "current_contract", None)
        else default_hk_current_contract_path(artifacts_root)
    )
    contract_payload = load_current_contract(current_contract_path)
    contract_exists = contract_payload is not None
    if contract_payload is None:
        contract_payload = build_hk_current_contract(
            artifacts_root,
            generated_by="inspect-hk-current-health",
        )

    contract_assets = contract_payload.get("assets") if isinstance(contract_payload.get("assets"), Mapping) else {}
    candidate_paths = hk_current_candidate_paths(artifacts_root)
    known_asset_keys = list(dict.fromkeys([*candidate_paths.keys(), *contract_assets.keys()]))
    selected_assets = _normalize_asset_keys(getattr(args, "asset", None), known_asset_keys=known_asset_keys)

    universe_meta_alias_path = None
    universe_meta_entry = contract_assets.get("universe_meta") if isinstance(contract_assets, Mapping) else None
    if isinstance(universe_meta_entry, Mapping):
        universe_meta_alias_path = str(universe_meta_entry.get("alias_path") or "").strip() or None
    universe_meta_path = (
        Path(universe_meta_alias_path)
        if universe_meta_alias_path
        else candidate_paths.get("universe_meta")
    )
    universe_meta_summary = _load_universe_meta_summary(universe_meta_path)

    asset_records = [
        _resolve_asset_record(
            asset_key=asset_key,
            contract_assets=contract_assets,
            candidate_paths=candidate_paths,
            universe_meta_summary=universe_meta_summary,
        )
        for asset_key in selected_assets
    ]
    target_date, target_date_source = _resolve_target_date(
        explicit=getattr(args, "target_date", None),
        contract_payload=contract_payload,
        asset_records=asset_records,
    )
    quality_checks = _build_quality_checks(
        contract_exists=contract_exists,
        current_contract_path=current_contract_path,
        target_date=target_date,
        asset_records=asset_records,
    )
    quality_verdict = summarize_quality_checks(
        quality_checks,
        fail_on_severity=getattr(args, "fail_on_severity", "none"),
    )
    asset_issue_summary = _asset_issue_summary(quality_checks)

    summary = {
        "artifacts_root": str(artifacts_root),
        "current_contract_path": str(current_contract_path),
        "contract_exists": contract_exists,
        "contract_target_date": (
            _normalize_date_text(
                contract_payload.get("contract", {}).get("target_date")
                if isinstance(contract_payload.get("contract"), Mapping)
                else None
            )
        ),
        "contract_generated_at": (
            str(contract_payload.get("contract", {}).get("generated_at") or "").strip() or None
            if isinstance(contract_payload.get("contract"), Mapping)
            else None
        ),
        "contract_generated_by": (
            str(contract_payload.get("contract", {}).get("generated_by") or "").strip() or None
            if isinstance(contract_payload.get("contract"), Mapping)
            else None
        ),
        "target_date": target_date,
        "target_date_source": target_date_source,
        "assets_checked": len(asset_records),
        "required_assets": sum(1 for record in asset_records if _asset_policy(str(record["asset_key"]))["required"]),
        "optional_assets": sum(1 for record in asset_records if not _asset_policy(str(record["asset_key"]))["required"]),
        "missing_assets": sum(1 for record in asset_records if not bool(record.get("exists"))),
        "stale_assets": sum(
            1
            for record in asset_records
            if _asset_lag_exceeds_policy(
                effective_as_of=str(record.get("effective_as_of") or "").strip() or None,
                target_date=target_date,
                policy=_asset_policy(str(record["asset_key"])),
            )
        ),
        "assets_missing_manifest": sum(
            1
            for record in asset_records
            if _asset_policy(str(record["asset_key"]))["require_manifest"] and not record.get("manifest_path")
        ),
        "assets_with_issues": len(asset_issue_summary),
    }
    payload = {
        "summary": summary,
        "quality_verdict": quality_verdict,
        "quality_checks": quality_checks,
        "assets": {
            str(record["asset_key"]): {
                **record,
                "issue_summary": asset_issue_summary.get(str(record["asset_key"]), {"overall_severity": "none", "checks": []}),
            }
            for record in asset_records
        },
    }

    output_format = str(getattr(args, "format", "text") or "text").strip().lower()
    if output_format == "json":
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        rendered = _render_current_health_text(payload)

    out_path = Path(args.out).expanduser().resolve() if getattr(args, "out", None) else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return quality_gate_exit_code(quality_verdict)
