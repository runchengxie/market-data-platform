from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from market_data_platform.current_assets import (
    build_hk_current_contract,
    default_hk_current_contract_path,
    hk_current_candidate_paths,
    load_current_contract,
)
from market_data_platform.repo_paths import find_repo_root, resolve_repo_path
from market_data_platform.intraday_paths import resolve_input_parquet_paths
from .asset_health import _infer_date_column
from .intraday_health import inspect_hk_intraday_health
from .quality_gate import (
    append_quality_verdict_lines,
    quality_gate_exit_code,
    summarize_quality_checks,
)
from .shared import _load_manifest, _normalize_frame_columns

_DEFAULT_SNAPSHOT_FAMILIES = (
    "daily",
    "intraday",
    "instruments",
    "valuation",
    "pit_financials",
    "ex_factors",
    "dividends",
    "shares",
    "exchange_rate",
    "southbound",
    "financial_details",
    "industry_changes",
    "instrument_industry",
    "announcement",
)
_EXPECTED_REPORT_KINDS = (
    "current_health",
    "daily_clean_health",
    "valuation_health",
    "pit_coverage",
    "intraday_health",
    "asset_refresh",
)
_SEVERITY_RANK = {"none": -1, "info": 0, "warning": 1, "error": 2}
_REPORT_KIND_FILENAME_PREFIXES = {
    "current_health": ("hk_current_health_",),
    "daily_clean_health": ("hk_daily_clean_health_",),
    "valuation_health": ("hk_valuation_health_",),
    "pit_coverage": ("hk_pit",),
    "intraday_health": ("hk_intraday_health_",),
    "asset_refresh": ("hk_asset_refresh_",),
}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_date(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "nat"}:
        return None
    timestamp = pd.to_datetime(text, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.normalize().strftime("%Y%m%d")


def _iso_date(value: object) -> str | None:
    text = _norm_date(value)
    if text is None:
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _max_severity(values: Iterable[object]) -> str:
    severity = "none"
    for value in values:
        text = str(value or "none").strip().lower()
        if _SEVERITY_RANK.get(text, -1) > _SEVERITY_RANK[severity]:
            severity = text
    return severity


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _path_text(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _resolve_optional_path(value: object, *, repo_root: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return resolve_repo_path(text, repo_root=repo_root)


def _relative_or_absolute(path: Path, *, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _infer_query_date(manifest: Mapping[str, Any] | None, keys: Sequence[str]) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    query = manifest.get("query")
    if not isinstance(query, Mapping):
        return None
    for key in keys:
        normalized = _norm_date(query.get(key))
        if normalized:
            return normalized
    return None


def _manifest_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = _load_manifest(path)
    if not isinstance(payload, Mapping):
        return None
    output_dir = str(payload.get("output_dir") or "").strip() or None
    status_counts = payload.get("status_counts") if isinstance(payload.get("status_counts"), Mapping) else {}
    return {
        "path": str(path.resolve()),
        "dataset": str(payload.get("dataset") or "").strip() or None,
        "status": str(payload.get("status") or "").strip() or None,
        "error": str(payload.get("error") or "").strip() or None,
        "output_dir": output_dir,
        "snapshot_name": Path(output_dir).name if output_dir else None,
        "query_start_date": _infer_query_date(payload, ("start_date", "start", "from")),
        "query_end_date": _infer_query_date(payload, ("end_date", "date", "mapping_date", "as_of_date", "to")),
        "status_counts": {
            str(key): int(value)
            for key, value in status_counts.items()
            if str(key).strip() and str(value).strip().isdigit()
        },
    }


def _infer_manifest_path(path: Path) -> Path | None:
    candidates: list[Path] = []
    if path.is_dir():
        candidates.append(path / "manifest.yml")
    else:
        candidates.append(path.with_name(f"{path.stem}.manifest.yml"))
        candidates.append(path.parent / "manifest.yml")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _path_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    if path.exists():
        return "other"
    return "missing"


def _record_key(path: Path) -> str:
    return str(path.resolve(strict=False))


def _new_record(
    *,
    path: Path,
    family: str | None,
    asset_key: str | None = None,
    source: str,
) -> dict[str, Any]:
    resolved = path.resolve(strict=False)
    manifest_path = _infer_manifest_path(resolved if resolved.exists() else path)
    manifest = _manifest_summary(manifest_path)
    as_of = None
    if isinstance(manifest, Mapping):
        as_of = manifest.get("query_end_date")
    if as_of is None:
        as_of = _norm_date(resolved.name)
    metadata_issues: list[dict[str, str]] = []
    if path.exists() and path.is_dir() and manifest_path is None:
        metadata_issues.append({"code": "manifest_missing", "severity": "warning"})
    if isinstance(manifest, Mapping) and manifest.get("status") not in {None, "", "completed"}:
        if _is_provider_permission_manifest(manifest):
            metadata_issues.append(
                {
                    "code": "manifest_status_provider_permission_boundary",
                    "severity": "info",
                }
            )
        else:
            metadata_issues.append({"code": "manifest_status_not_completed", "severity": "warning"})
    if isinstance(manifest, Mapping) and manifest.get("output_dir"):
        manifest_output = Path(str(manifest["output_dir"])).expanduser()
        if not manifest_output.is_absolute():
            manifest_output = (Path.cwd() / manifest_output).resolve()
        else:
            manifest_output = manifest_output.resolve()
        if path.exists() and manifest_output != resolved:
            metadata_issues.append({"code": "manifest_output_dir_mismatch", "severity": "warning"})

    return {
        "record_id": None,
        "asset_keys": [asset_key] if asset_key else [],
        "family": family,
        "path": str(path.resolve(strict=False)),
        "resolved_path": str(resolved),
        "path_kind": _path_kind(path),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
        "aliases": [],
        "sources": [source],
        "manifest_path": _path_text(manifest_path),
        "manifest_status": manifest.get("status") if isinstance(manifest, Mapping) else None,
        "manifest": manifest,
        "as_of": as_of,
        "query_start_date": manifest.get("query_start_date") if isinstance(manifest, Mapping) else None,
        "query_end_date": manifest.get("query_end_date") if isinstance(manifest, Mapping) else None,
        "latest_observed_trade_date": None,
        "references": [],
        "metadata_issues": metadata_issues,
        "classification": "unreferenced",
    }


def _merge_record(
    records: dict[str, dict[str, Any]],
    record: dict[str, Any],
    *,
    key_path: Path,
) -> dict[str, Any]:
    def _empty(value: object) -> bool:
        return value is None or value == "" or value == []

    key = _record_key(key_path)
    existing = records.get(key)
    if existing is None:
        record["record_id"] = f"asset:{len(records) + 1}"
        records[key] = record
        return record
    for field in ("asset_keys", "sources", "aliases", "references", "metadata_issues"):
        current = list(existing.get(field) or [])
        for item in record.get(field) or []:
            if item not in current:
                current.append(item)
        existing[field] = current
    for field in ("family", "manifest_path", "manifest_status", "manifest", "as_of", "query_start_date", "query_end_date"):
        if _empty(existing.get(field)) and not _empty(record.get(field)):
            existing[field] = record[field]
    existing["exists"] = bool(existing.get("exists")) or bool(record.get("exists"))
    return existing


def _add_reference(record: dict[str, Any], *, ref_type: str, path: Path | None = None, detail: str | None = None) -> None:
    ref = {"type": ref_type}
    if path is not None:
        ref["path"] = str(path)
    if detail:
        ref["detail"] = detail
    refs = list(record.get("references") or [])
    if ref not in refs:
        refs.append(ref)
    record["references"] = refs


def _scan_reference_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in ("reports", "releases"):
        base = root / dirname
        if not base.exists():
            continue
        for pattern in ("*.json", "*.yml", "*.yaml"):
            files.extend(
                path
                for path in base.rglob(pattern)
                if path.is_file() and not path.name.startswith("hk_data_asset_audit_")
            )
    return sorted(files)


def _annotate_report_references(records: dict[str, dict[str, Any]], *, artifacts_root: Path) -> None:
    searchable = [
        (key, record, [record.get("resolved_path"), record.get("path"), record.get("manifest_path")])
        for key, record in records.items()
    ]
    for ref_file in _scan_reference_files(artifacts_root):
        try:
            if ref_file.stat().st_size > 2_000_000:
                continue
            text = ref_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for _key, record, needles in searchable:
            if any(str(needle) and str(needle) in text for needle in needles):
                ref_type = "release" if "/releases/" in ref_file.as_posix() else "report"
                _add_reference(record, ref_type=ref_type, path=ref_file)


def _classify_inventory_record(record: dict[str, Any]) -> str:
    metadata_issues = [
        item
        for item in (record.get("metadata_issues") or [])
        if isinstance(item, Mapping)
    ]
    if any(str(item.get("severity") or "warning") != "info" for item in metadata_issues):
        return "metadata-inconsistent"
    refs = record.get("references") if isinstance(record.get("references"), list) else []
    ref_types = {str(item.get("type")) for item in refs if isinstance(item, Mapping)}
    if "current" in ref_types:
        return "current"
    if "workflow" in ref_types:
        return "retained"
    return "unreferenced"


def collect_inventory(
    *,
    artifacts_root: Path,
    current_contract_path: Path | None,
    selected_assets: Sequence[str] | None = None,
    scan_families: Sequence[str] | None = None,
) -> dict[str, Any]:
    contract_path = current_contract_path or default_hk_current_contract_path(artifacts_root)
    contract = load_current_contract(contract_path)
    contract_exists = contract is not None
    if contract is None:
        contract = build_hk_current_contract(artifacts_root, generated_by="inspect-hk-data-assets")
    contract_assets = contract.get("assets") if isinstance(contract.get("assets"), Mapping) else {}
    candidate_paths = hk_current_candidate_paths(artifacts_root)
    selected = set(selected_assets or [])

    records: dict[str, dict[str, Any]] = {}
    for asset_key, candidate in candidate_paths.items():
        if selected and asset_key not in selected:
            continue
        entry = contract_assets.get(asset_key) if isinstance(contract_assets, Mapping) else None
        entry = entry if isinstance(entry, Mapping) else {}
        alias_path = Path(str(entry.get("alias_path") or candidate)).expanduser()
        resolved_path = Path(str(entry.get("resolved_path") or alias_path.resolve(strict=False))).expanduser()
        record = _new_record(
            path=resolved_path,
            family=resolved_path.parent.name if resolved_path.parent.name != "hk" else None,
            asset_key=asset_key,
            source="current_contract" if entry else "default_alias",
        )
        record["aliases"] = [str(alias_path.resolve(strict=False))]
        record = _merge_record(records, record, key_path=resolved_path)
        if asset_key not in record["asset_keys"]:
            record["asset_keys"].append(asset_key)
        if str(alias_path.resolve(strict=False)) not in record["aliases"]:
            record["aliases"].append(str(alias_path.resolve(strict=False)))
        _add_reference(record, ref_type="current", path=contract_path, detail=asset_key)

    for family in scan_families or _DEFAULT_SNAPSHOT_FAMILIES:
        base = artifacts_root / "assets" / "rqdata" / "hk" / family
        if not base.exists():
            continue
        for child in sorted(base.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            if child.is_dir() or child.suffix.lower() in {".parquet", ".csv", ".txt", ".yml", ".yaml"}:
                record = _new_record(path=child, family=family, source="snapshot_scan")
                _merge_record(records, record, key_path=child.resolve(strict=False))

    snapshots_base = artifacts_root / "snapshots"
    if (not selected) and snapshots_base.exists():
        for child in sorted(snapshots_base.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            if child.is_dir() or child.is_file():
                record = _new_record(path=child, family="snapshots", source="snapshot_scan")
                _merge_record(records, record, key_path=child.resolve(strict=False))

    _annotate_report_references(records, artifacts_root=artifacts_root)
    ordered = sorted(records.values(), key=lambda item: (str(item.get("family") or ""), str(item.get("resolved_path") or "")))
    for record in ordered:
        record["classification"] = _classify_inventory_record(record)

    counts = Counter(str(record.get("classification") or "unreferenced") for record in ordered)
    return {
        "summary": {
            "contract_path": str(contract_path),
            "contract_exists": contract_exists,
            "records": len(ordered),
            "current": int(counts.get("current", 0)),
            "retained": int(counts.get("retained", 0)),
            "unreferenced": int(counts.get("unreferenced", 0)),
            "metadata_inconsistent": int(counts.get("metadata-inconsistent", 0)),
        },
        "records": ordered,
    }


def _current_record(inventory: Mapping[str, Any], asset_key: str) -> dict[str, Any] | None:
    for record in inventory.get("records") or []:
        if isinstance(record, Mapping) and asset_key in set(record.get("asset_keys") or []):
            return dict(record)
    return None


def _asset_dir_from_record(record: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(record, Mapping):
        return None
    path_text = str(record.get("resolved_path") or record.get("path") or "").strip()
    if not path_text:
        return None
    path = Path(path_text)
    return path if path.exists() and path.is_dir() else None


def _read_symbol_dates(path: Path, *, date_column: str | None = None) -> tuple[str | None, str | None, int]:
    try:
        frame = pd.read_parquet(path, columns=[date_column] if date_column else None)
    except Exception:
        frame = pd.read_parquet(path)
    frame = _normalize_frame_columns(frame)
    column = _infer_date_column(frame.columns.tolist(), date_column)
    dates = pd.to_datetime(frame[column], errors="coerce").dropna()
    if dates.empty:
        return None, None, 0
    dates = dates.dt.normalize() if hasattr(dates, "dt") else pd.Series(pd.to_datetime(dates)).dt.normalize()
    return _iso_date(dates.min()), _iso_date(dates.max()), int(len(frame))


def _read_asset_audit_rows(asset_dir: Path) -> dict[str, dict[str, str]]:
    audit_path = asset_dir / "audit.csv"
    if not audit_path.exists():
        return {}
    try:
        frame = pd.read_csv(audit_path, dtype=str).fillna("")
    except Exception:
        return {}
    if "symbol" not in frame.columns:
        return {}
    rows: dict[str, dict[str, str]] = {}
    for item in frame.to_dict("records"):
        symbol = str(item.get("symbol") or "").strip()
        if symbol:
            rows[symbol] = {str(key): str(value) for key, value in item.items()}
    return rows


def _is_provider_permission_error(text: object) -> bool:
    lowered = str(text or "").lower()
    return "permission" in lowered and ("ricequant" in lowered or "instrument" in lowered or "access" in lowered)


def _is_provider_permission_manifest(manifest: Mapping[str, Any] | None) -> bool:
    if not isinstance(manifest, Mapping):
        return False
    status = str(manifest.get("status") or "").strip().lower()
    if "provider_permission" in status:
        return True
    if _is_provider_permission_error(manifest.get("error")):
        return True
    status_counts = manifest.get("status_counts")
    return isinstance(status_counts, Mapping) and int(status_counts.get("provider_permission_blocked") or 0) > 0


def _find_etf_provider_permission_blocker(
    *,
    inventory: Mapping[str, Any],
    effective_end: str | None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for record in inventory.get("records") or []:
        if not isinstance(record, Mapping):
            continue
        if str(record.get("family") or "") != "daily":
            continue
        resolved_name = Path(str(record.get("resolved_path") or record.get("path") or "")).name.lower()
        if "hk_etf" not in resolved_name:
            continue
        manifest = record.get("manifest") if isinstance(record.get("manifest"), Mapping) else None
        if not _is_provider_permission_manifest(manifest):
            continue
        query_end = _norm_date(record.get("query_end_date") or record.get("as_of"))
        if effective_end and query_end and query_end < effective_end:
            continue
        candidates.append(
            {
                "path": record.get("resolved_path") or record.get("path"),
                "manifest_path": record.get("manifest_path"),
                "query_end_date": _iso_date(query_end),
                "status": record.get("manifest_status"),
                "error": manifest.get("error") if isinstance(manifest, Mapping) else None,
            }
        )
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: str(item.get("query_end_date") or ""))[-1]


def _build_etf_missing_file_issues(
    *,
    missing_files: list[str],
    audit_rows: Mapping[str, Mapping[str, str]],
    sample_limit: int,
) -> list[dict[str, Any]]:
    provider_permission_missing = {
        symbol
        for symbol in missing_files
        if _is_provider_permission_error(audit_rows.get(symbol, {}).get("error"))
    }
    local_missing = [
        symbol for symbol in missing_files if symbol not in provider_permission_missing
    ]
    issues: list[dict[str, Any]] = []
    if local_missing:
        issues.append(
            {
                "code": "missing_symbol_files",
                "severity": "error",
                "classification": "local-gap",
                "affected_symbols": len(local_missing),
                "sample_symbols": local_missing[:sample_limit],
            }
        )
    if provider_permission_missing:
        provider_symbols = sorted(provider_permission_missing)
        issues.append(
            {
                "code": "provider_permission_symbol_files",
                "severity": "warning",
                "classification": "provider-permission-gap",
                "affected_symbols": len(provider_symbols),
                "sample_symbols": provider_symbols[:sample_limit],
            }
        )
    return issues


def _scan_etf_daily_symbol_ranges(
    *,
    files_by_symbol: Mapping[str, Path],
    audit_rows: Mapping[str, Mapping[str, str]],
    target_date: str,
    sample_limit: int,
) -> dict[str, Any]:
    date_min_values: list[str] = []
    date_max_values: list[str] = []
    local_stale_symbols: list[dict[str, Any]] = []
    provider_boundary_stale_symbols: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    rows_scanned = 0

    for symbol, path in files_by_symbol.items():
        min_date, max_date, row_count = _read_symbol_dates(path)
        rows_scanned += row_count
        if min_date:
            date_min_values.append(_norm_date(min_date) or "")
        if max_date:
            max_date_norm = _norm_date(max_date) or ""
            date_max_values.append(max_date_norm)
            if max_date_norm < target_date:
                row = audit_rows.get(symbol, {})
                status = str(row.get("status") or "").strip().lower()
                stale_item = {"symbol": symbol, "latest_date": _iso_date(max_date_norm)}
                if status in {"linked_base", "missing_remote"}:
                    provider_boundary_stale_symbols.append(stale_item)
                elif _is_provider_permission_error(row.get("error")):
                    provider_boundary_stale_symbols.append(stale_item)
                else:
                    local_stale_symbols.append(stale_item)
        if len(samples) < sample_limit:
            samples.append(
                {
                    "symbol": symbol,
                    "min_date": min_date,
                    "max_date": max_date,
                    "rows": row_count,
                }
            )

    return {
        "date_min_values": date_min_values,
        "date_max_values": date_max_values,
        "local_stale_symbols": local_stale_symbols,
        "provider_boundary_stale_symbols": provider_boundary_stale_symbols,
        "samples": samples,
        "rows_scanned": rows_scanned,
    }


def _etf_start_gap_issue(
    *,
    effective_start: str,
    expected_start_date: str,
    record: Mapping[str, Any] | object,
) -> dict[str, Any] | None:
    manifest_start = _norm_date(
        record.get("query_start_date") if isinstance(record, Mapping) else None
    )
    expected_ts = pd.to_datetime(expected_start_date, errors="coerce")
    observed_ts = pd.to_datetime(effective_start, errors="coerce")
    within_start_tolerance = (
        not pd.isna(expected_ts)
        and not pd.isna(observed_ts)
        and 0 <= int((observed_ts - expected_ts).days) <= 7
    )
    if manifest_start and manifest_start > expected_start_date:
        classification = "provider-boundary"
    elif within_start_tolerance:
        classification = "provider-boundary"
    else:
        classification = "local-gap"
    if within_start_tolerance and classification == "provider-boundary":
        return None
    return {
        "code": "coverage_starts_after_expected_start",
        "severity": "info" if classification == "provider-boundary" else "error",
        "classification": classification,
        "expected_start_date": _iso_date(expected_start_date),
        "observed_start_date": _iso_date(effective_start),
    }


def verify_etf_daily_completeness(
    *,
    inventory: Mapping[str, Any],
    target_date: str,
    expected_start_date: str = "20000101",
    scan_data: bool = True,
    sample_limit: int = 5,
) -> dict[str, Any]:
    record = _current_record(inventory, "etf_daily")
    asset_dir = _asset_dir_from_record(record)
    issues: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    if asset_dir is None:
        return {
            "asset_key": "etf_daily",
            "status": "fail",
            "classification": "local-gap",
            "checked_path": record.get("resolved_path") if isinstance(record, Mapping) else None,
            "issues": [{"code": "asset_missing", "severity": "error"}],
        }

    data_dir = asset_dir / "data"
    data_files = sorted(data_dir.glob("*.parquet")) if data_dir.exists() else []
    symbols_file = asset_dir / "symbols.txt"
    expected_symbols = [
        line.strip()
        for line in symbols_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if symbols_file.exists() else [path.stem for path in data_files]
    files_by_symbol = {path.stem: path for path in data_files}
    audit_rows = _read_asset_audit_rows(asset_dir)
    missing_files = [symbol for symbol in expected_symbols if symbol not in files_by_symbol]
    issues.extend(
        _build_etf_missing_file_issues(
            missing_files=missing_files,
            audit_rows=audit_rows,
            sample_limit=sample_limit,
        )
    )

    date_min_values: list[str] = []
    date_max_values: list[str] = []
    local_stale_symbols: list[dict[str, Any]] = []
    provider_boundary_stale_symbols: list[dict[str, Any]] = []
    rows_scanned = 0
    if scan_data:
        scan_result = _scan_etf_daily_symbol_ranges(
            files_by_symbol=files_by_symbol,
            audit_rows=audit_rows,
            target_date=target_date,
            sample_limit=sample_limit,
        )
        date_min_values = scan_result["date_min_values"]
        date_max_values = scan_result["date_max_values"]
        local_stale_symbols = scan_result["local_stale_symbols"]
        provider_boundary_stale_symbols = scan_result["provider_boundary_stale_symbols"]
        samples = scan_result["samples"]
        rows_scanned = scan_result["rows_scanned"]
    else:
        if isinstance(record, Mapping) and record.get("query_start_date"):
            date_min_values.append(str(record["query_start_date"]))
        if isinstance(record, Mapping) and (record.get("query_end_date") or record.get("as_of")):
            date_max_values.append(str(record.get("query_end_date") or record.get("as_of")))

    effective_start = min(date_min_values) if date_min_values else None
    effective_end = max(date_max_values) if date_max_values else None
    provider_permission_blocker = _find_etf_provider_permission_blocker(
        inventory=inventory,
        effective_end=effective_end,
    )
    if provider_permission_blocker is not None and local_stale_symbols:
        provider_boundary_stale_symbols.extend(local_stale_symbols)
        local_stale_symbols = []
    if effective_start and effective_start > expected_start_date:
        start_issue = _etf_start_gap_issue(
            effective_start=effective_start,
            expected_start_date=expected_start_date,
            record=record,
        )
        if start_issue is not None:
            issues.append(start_issue)
    if effective_end and effective_end < target_date:
        stale_classification = "provider-permission-gap" if provider_permission_blocker is not None else "local-gap"
        issues.append(
            {
                "code": "asset_stale_before_target",
                "severity": "warning" if provider_permission_blocker is not None else "error",
                "classification": stale_classification,
                "target_date": _iso_date(target_date),
                "latest_observed_date": _iso_date(effective_end),
                **(
                    {"provider_permission_blocker": provider_permission_blocker}
                    if provider_permission_blocker is not None
                    else {}
                ),
            }
        )
    if local_stale_symbols:
        issues.append(
            {
                "code": "stale_symbols_before_target",
                "severity": "error",
                "classification": "local-gap",
                "affected_symbols": len(local_stale_symbols),
                "sample_symbols": local_stale_symbols[:sample_limit],
            }
        )
    if provider_boundary_stale_symbols:
        issues.append(
            {
                "code": "provider_boundary_stale_symbols_before_target",
                "severity": "warning",
                "classification": (
                    "provider-permission-gap"
                    if provider_permission_blocker is not None
                    else "provider-boundary"
                ),
                "affected_symbols": len(provider_boundary_stale_symbols),
                "sample_symbols": provider_boundary_stale_symbols[:sample_limit],
                **(
                    {"provider_permission_blocker": provider_permission_blocker}
                    if provider_permission_blocker is not None
                    else {}
                ),
            }
        )

    local_failures = [item for item in issues if item.get("classification") == "local-gap"]
    status = "pass" if not local_failures else "fail"
    return {
        "asset_key": "etf_daily",
        "status": status,
        "classification": "complete" if not issues else ("local-gap" if local_failures else "provider-boundary"),
        "checked_path": str(asset_dir),
        "target_date": _iso_date(target_date),
        "expected_start_date": _iso_date(expected_start_date),
        "effective_start_date": _iso_date(effective_start),
        "effective_end_date": _iso_date(effective_end),
        "symbol_count": len(expected_symbols),
        "data_file_count": len(data_files),
        "rows_scanned": rows_scanned if scan_data else None,
        "evidence_source": "parquet_scan" if scan_data else "manifest",
        "sample_symbol_ranges": samples,
        "issues": issues,
    }


def _read_intraday_latest_date(path: Path) -> tuple[str | None, int, int]:
    parquet_paths = resolve_input_parquet_paths([str(path)])
    latest: str | None = None
    rows_scanned = 0
    files_scanned = 0
    for parquet_path in parquet_paths:
        try:
            frame = pd.read_parquet(parquet_path, columns=["trade_datetime"])
        except Exception:
            try:
                frame = pd.read_parquet(parquet_path, columns=["datetime"])
            except Exception:
                frame = pd.read_parquet(parquet_path)
        frame = _normalize_frame_columns(frame)
        column = "trade_datetime" if "trade_datetime" in frame.columns else "datetime"
        dates = pd.to_datetime(frame.get(column), errors="coerce").dropna()
        rows_scanned += int(len(frame))
        files_scanned += 1
        if dates.empty:
            continue
        observed = _norm_date(dates.max())
        if observed and (latest is None or observed > latest):
            latest = observed
    return latest, rows_scanned, files_scanned


def verify_intraday_freshness(
    *,
    inventory: Mapping[str, Any],
    target_date: str,
    mode: str = "metadata",
    daily_asset_dir: Path | None = None,
    sample_limit: int = 5,
    fail_on_severity: str = "none",
) -> dict[str, Any]:
    record = _current_record(inventory, "intraday")
    asset_dir = _asset_dir_from_record(record)
    if asset_dir is None:
        return {
            "asset_key": "intraday",
            "status": "fail",
            "checked_path": record.get("resolved_path") if isinstance(record, Mapping) else None,
            "issues": [{"code": "asset_missing", "severity": "error", "classification": "local-gap"}],
        }

    health_report = None
    latest = None
    rows_scanned = None
    files_scanned = None
    evidence_source = "manifest"
    if mode == "scan":
        latest, rows_scanned, files_scanned = _read_intraday_latest_date(asset_dir)
        evidence_source = "parquet_scan"
    elif mode == "health":
        tmp_handle = tempfile.NamedTemporaryFile(
            prefix="cstree_intraday_health_",
            suffix=".json",
            delete=False,
        )
        tmp_handle.close()
        out_path = Path(tmp_handle.name)
        args = SimpleNamespace(
            input=[str(asset_dir)],
            daily_asset_dir=str(daily_asset_dir) if daily_asset_dir else None,
            sample_limit=sample_limit,
            expected_bars_per_day=66,
            numeric_rtol=1e-6,
            numeric_atol=1e-8,
            format="json",
            out=str(out_path),
            fail_on_severity=fail_on_severity,
        )
        inspect_hk_intraday_health(args)
        health_report = _safe_read_json(out_path)
        summary = health_report.get("summary") if isinstance(health_report, Mapping) else {}
        latest = _norm_date(summary.get("trade_date_max") if isinstance(summary, Mapping) else None)
        rows_scanned = summary.get("rows_scanned") if isinstance(summary, Mapping) else None
        files_scanned = summary.get("parquet_files_scanned") if isinstance(summary, Mapping) else None
        evidence_source = "intraday_health"
    else:
        latest = _norm_date(
            (record.get("query_end_date") or record.get("as_of")) if isinstance(record, Mapping) else None
        )

    issues: list[dict[str, Any]] = []
    if latest is None:
        issues.append({"code": "latest_trade_date_unknown", "severity": "warning", "classification": "manual-review"})
    elif latest < target_date:
        issues.append(
            {
                "code": "intraday_stale_before_target",
                "severity": "error",
                "classification": "local-gap",
                "target_date": _iso_date(target_date),
                "latest_observed_date": _iso_date(latest),
            }
        )
    if isinstance(health_report, Mapping):
        verdict = health_report.get("quality_verdict")
        if isinstance(verdict, Mapping) and str(verdict.get("overall_severity")) in {"warning", "error"}:
            issues.append(
                {
                    "code": "intraday_health_issues",
                    "severity": str(verdict.get("overall_severity")),
                    "classification": "local-gap",
                    "issue_count": verdict.get("issue_count"),
                }
            )

    return {
        "asset_key": "intraday",
        "status": "pass" if not [item for item in issues if item.get("classification") == "local-gap"] else "fail",
        "checked_path": str(asset_dir),
        "target_date": _iso_date(target_date),
        "latest_observed_trade_date": _iso_date(latest),
        "rows_scanned": rows_scanned,
        "parquet_files_scanned": files_scanned,
        "evidence_source": evidence_source,
        "health_report": health_report,
        "issues": issues,
    }


def _default_health_report_paths(*, reports_dir: Path, target_date: str) -> dict[str, list[Path]]:
    return {
        "current_health": sorted(reports_dir.glob(f"hk_current_health_{target_date}*.json")),
        "daily_clean_health": sorted(reports_dir.glob(f"hk_daily_clean_health_{target_date}*.json")),
        "valuation_health": sorted(reports_dir.glob(f"hk_valuation_health_{target_date}*.json")),
        "pit_coverage": sorted(reports_dir.glob(f"hk_pit*{target_date}*.json")),
        "intraday_health": sorted(reports_dir.glob(f"hk_intraday_health_{target_date}*.json")),
        "asset_refresh": sorted(reports_dir.glob(f"hk_asset_refresh_{target_date}*.json")),
    }


def _latest_report_only(paths: Sequence[Path]) -> list[Path]:
    existing = _dedupe_paths(path for path in paths if path.exists())
    if not existing:
        return []

    def sort_key(path: Path) -> tuple[float, str]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (mtime, path.name)

    return [max(existing, key=sort_key)]


def _path_identity(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        key = _path_identity(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _infer_health_report_kind(path: Path) -> str:
    name = path.name.lower()
    for kind, prefixes in _REPORT_KIND_FILENAME_PREFIXES.items():
        if any(name.startswith(prefix) for prefix in prefixes):
            return kind
    return "explicit"


def aggregate_health_reports(
    *,
    reports_dir: Path,
    target_date: str,
    extra_reports: Sequence[Path] | None = None,
    expected_report_kinds: Sequence[str] | None = None,
) -> dict[str, Any]:
    grouped = _default_health_report_paths(reports_dir=reports_dir, target_date=target_date)
    if extra_reports:
        for report in extra_reports:
            grouped.setdefault(_infer_health_report_kind(report), []).append(report)
    expected_kinds = tuple(_EXPECTED_REPORT_KINDS if expected_report_kinds is None else expected_report_kinds)
    ordered_kinds = tuple(dict.fromkeys((*expected_kinds, *grouped.keys(), "explicit")))
    sources: list[dict[str, Any]] = []
    merged_issues: list[dict[str, Any]] = []
    processed_paths: set[str] = set()
    for kind in ordered_kinds:
        paths = _dedupe_paths(grouped.get(kind) or [])
        if kind != "explicit":
            paths = _latest_report_only(paths)
        if not paths and kind in expected_kinds:
            sources.append({"kind": kind, "status": "missing", "paths": [], "overall_severity": "warning", "issue_count": 1})
            merged_issues.append({"source": kind, "check": "expected_report_missing", "severity": "warning"})
            continue
        for path in paths:
            path_key = _path_identity(path)
            if path_key in processed_paths:
                continue
            processed_paths.add(path_key)
            payload = _safe_read_json(path)
            if payload is None:
                sources.append({"kind": kind, "status": "unavailable", "path": str(path), "overall_severity": "warning", "issue_count": 1})
                merged_issues.append({"source": kind, "path": str(path), "check": "report_unreadable", "severity": "warning"})
                continue
            verdict = payload.get("quality_verdict") if isinstance(payload, Mapping) else None
            checks = payload.get("quality_checks") if isinstance(payload.get("quality_checks"), list) else []
            severity = str(verdict.get("overall_severity") if isinstance(verdict, Mapping) else "none")
            issue_count = int(verdict.get("issue_count") or 0) if isinstance(verdict, Mapping) else len(checks)
            sources.append(
                {
                    "kind": kind,
                    "status": "available",
                    "path": str(path),
                    "overall_severity": severity,
                    "issue_count": issue_count,
                    "quality_verdict": verdict,
                }
            )
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                merged = dict(check)
                merged["source"] = kind
                merged["source_path"] = str(path)
                merged_issues.append(merged)
            if not checks and issue_count > 0:
                merged_issues.append(
                    {
                        "source": kind,
                        "source_path": str(path),
                        "check": "source_report_issue_count",
                        "severity": severity if severity in {"info", "warning", "error"} else "info",
                        "affected_items": issue_count,
                    }
                )
    verdict = summarize_quality_checks(merged_issues)
    return {
        "summary": {
            "reports_dir": str(reports_dir),
            "sources": len(sources),
            "available_sources": sum(1 for item in sources if item.get("status") == "available"),
            "missing_sources": sum(1 for item in sources if item.get("status") == "missing"),
            "overall_severity": verdict["overall_severity"],
            "issue_count": verdict["issue_count"],
        },
        "sources": sources,
        "merged_issues": merged_issues,
        "quality_verdict": verdict,
    }


def _candidate_action_for_issue(issue: Mapping[str, Any], *, asset_key: str) -> str:
    classification = str(issue.get("classification") or "").strip()
    code = str(issue.get("code") or issue.get("check") or "").strip()
    if classification.startswith("provider") or "provider" in code:
        return "provider-boundary"
    if code in {"intraday_daily_rows_missing_from_asset", "intraday_after_daily_end_with_trading"}:
        return "patch-refresh"
    if asset_key == "intraday" and ("stale" in code or "missing" in code):
        return "targeted-rebuild"
    if "missing" in code and asset_key not in {"daily", "daily_clean", "etf_daily", "valuation"}:
        return "manual-review"
    if "stale" in code or "gap" in code or "missing" in code:
        return "patch-refresh"
    return "manual-review"


def _asset_key_for_health_issue(issue: Mapping[str, Any], *, source: str) -> str:
    explicit = str(issue.get("asset_key") or "").strip()
    if explicit:
        return explicit
    code = str(issue.get("code") or issue.get("check") or "").strip()
    if code in {"intraday_daily_rows_missing_from_asset", "intraday_after_daily_end_with_trading"}:
        return "daily_clean"
    if code == "daily_active_but_intraday_missing":
        return "intraday"
    return source


def _next_business_date(value: object) -> str | None:
    normalized = _norm_date(value)
    if normalized is None:
        return None
    timestamp = pd.to_datetime(normalized, errors="coerce")
    if pd.isna(timestamp):
        return None
    return (timestamp + pd.offsets.BDay(1)).strftime("%Y%m%d")


def _candidate_affected_range(
    *,
    asset_key: str,
    issue: Mapping[str, Any],
    result: Mapping[str, Any],
    target_date: str,
) -> dict[str, str | None]:
    code = str(issue.get("code") or issue.get("check") or "").strip()
    if asset_key == "intraday" and "stale" in code:
        latest = issue.get("latest_observed_date") or result.get("latest_observed_trade_date")
        return {
            "start": _iso_date(_next_business_date(latest) or target_date),
            "end": _iso_date(target_date),
        }
    return {
        "start": issue.get("observed_start_date") or result.get("effective_start_date"),
        "end": issue.get("latest_observed_date") or result.get("effective_end_date"),
    }


def _candidate_action_for_inventory_record(record: Mapping[str, Any]) -> str:
    issues = [item for item in record.get("metadata_issues") or [] if isinstance(item, Mapping)]
    codes = {str(item.get("code") or "") for item in issues}
    refs = [item for item in record.get("references") or [] if isinstance(item, Mapping)]
    ref_types = {str(item.get("type") or "") for item in refs}
    if "current" in ref_types and "manifest_output_dir_mismatch" in codes:
        return "repoint"
    return "manual-review"


def build_repair_candidates(
    *,
    freshness: Mapping[str, Any],
    inventory: Mapping[str, Any],
    health: Mapping[str, Any],
    target_date: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for asset_key in ("etf_daily", "intraday"):
        result = freshness.get(asset_key) if isinstance(freshness.get(asset_key), Mapping) else None
        if not isinstance(result, Mapping):
            continue
        for issue in result.get("issues") or []:
            if not isinstance(issue, Mapping):
                continue
            action = _candidate_action_for_issue(issue, asset_key=asset_key)
            affected_range = _candidate_affected_range(
                asset_key=asset_key,
                issue=issue,
                result=result,
                target_date=target_date,
            )
            candidate = {
                "asset_key": asset_key,
                "action": action,
                "severity": issue.get("severity", "warning"),
                "target_date": _iso_date(target_date),
                "affected_range": affected_range,
                "evidence": dict(issue),
                "checked_path": result.get("checked_path"),
                "auto_executable": action in {"patch-refresh", "targeted-rebuild"},
                "command": _repair_command(
                    asset_key=asset_key,
                    action=action,
                    target_date=target_date,
                    affected_range=affected_range,
                ),
            }
            candidates.append(candidate)

    for record in inventory.get("records") or []:
        if not isinstance(record, Mapping) or not record.get("metadata_issues"):
            continue
        action = _candidate_action_for_inventory_record(record)
        candidates.append(
            {
                "asset_key": ",".join(record.get("asset_keys") or []) or None,
                "action": action,
                "severity": _max_severity(item.get("severity") for item in record.get("metadata_issues") or []),
                "target_date": _iso_date(target_date),
                "checked_path": record.get("resolved_path"),
                "evidence": {"metadata_issues": record.get("metadata_issues")},
                "auto_executable": False,
                "command": None,
            }
        )

    for issue in health.get("merged_issues") or []:
        if not isinstance(issue, Mapping):
            continue
        severity = str(issue.get("severity") or "info")
        if severity not in {"warning", "error"}:
            continue
        source = str(issue.get("source") or "")
        asset_key = _asset_key_for_health_issue(issue, source=source)
        candidates.append(
            {
                "asset_key": asset_key,
                "action": _candidate_action_for_issue(issue, asset_key=asset_key),
                "severity": severity,
                "target_date": _iso_date(target_date),
                "checked_path": issue.get("source_path"),
                "evidence": dict(issue),
                "auto_executable": False,
                "command": None,
            }
        )
    return candidates


def _repair_command(
    *,
    asset_key: str,
    action: str,
    target_date: str,
    affected_range: Mapping[str, Any] | None = None,
) -> list[str] | None:
    if action == "patch-refresh":
        command = [
            "scripts/dev/refresh_hk_current.sh",
            "--target-date",
            target_date,
            "--",
            "--refresh-mode",
            "patch",
        ]
        if asset_key == "etf_daily":
            command.extend(
                [
                    "--refresh-asset",
                    "etf_daily",
                    "--refresh-asset",
                    "etf_daily_clean",
                ]
            )
        return command
    if action == "targeted-rebuild" and asset_key == "intraday":
        start_date = _norm_date((affected_range or {}).get("start")) or target_date
        end_date = _norm_date((affected_range or {}).get("end")) or target_date
        return [
            "marketdata",
            "rqdata",
            "refresh-hk-intraday",
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--resume",
        ]
    return None


def execute_repair_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    approved_actions: set[str],
    execute: bool,
    repo_root: Path,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        action = str(candidate.get("action") or "")
        command = candidate.get("command")
        if action not in approved_actions:
            results.append({"index": index, "status": "skipped", "reason": "action_not_approved", "action": action})
            continue
        if not execute:
            results.append({"index": index, "status": "skipped", "reason": "execute_repair_false", "action": action})
            continue
        if not candidate.get("auto_executable") or not isinstance(command, list):
            results.append({"index": index, "status": "skipped", "reason": "not_auto_executable", "action": action})
            continue
        completed = subprocess.run(command, cwd=repo_root, check=False, text=True, capture_output=True)
        results.append(
            {
                "index": index,
                "status": "succeeded" if completed.returncode == 0 else "failed",
                "action": action,
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
                "post_repair_verification_required": True,
            }
        )
    return {"execute": execute, "approved_actions": sorted(approved_actions), "results": results}


def _path_size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return int(path.lstat().st_size)
        total = 0
        pending = [path]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    else:
                        total += int(stat.st_size)
        return total
    except OSError:
        return 0


def _record_references_summary(record: Mapping[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in record.get("references") or []:
        if isinstance(item, Mapping):
            counts[str(item.get("type") or "unknown")] += 1
    return dict(sorted(counts.items()))


def _current_replacement_for_record(
    record: Mapping[str, Any],
    *,
    current_by_family: Mapping[str, Sequence[Mapping[str, Any]]],
    target_date: str,
) -> str | None:
    family = str(record.get("family") or "")
    record_as_of = _norm_date(record.get("as_of"))
    for current in current_by_family.get(family, []):
        current_as_of = _norm_date(current.get("as_of"))
        if current_as_of and (record_as_of is None or current_as_of >= max(record_as_of, target_date)):
            return str(current.get("resolved_path") or "") or None
    return None


def _manual_prune_reason(
    record: Mapping[str, Any],
    *,
    current_by_family: Mapping[str, Sequence[Mapping[str, Any]]],
    target_date: str,
) -> tuple[str | None, str | None]:
    classification = str(record.get("classification") or "unreferenced")
    if classification == "current":
        return None, None
    replacement = _current_replacement_for_record(
        record,
        current_by_family=current_by_family,
        target_date=target_date,
    )
    name = Path(str(record.get("resolved_path") or record.get("path") or "")).name.lower()
    if record.get("metadata_issues"):
        return "metadata_inconsistent", replacement
    if any(token in name for token in ("patch", "repair", "broken", "tmp")):
        return "intermediate_artifact_requires_manual_review", replacement
    if replacement:
        return "superseded_by_current_but_referenced_or_unproven", replacement
    if str(record.get("family") or "") == "snapshots":
        return "snapshot_retention_policy_required", replacement
    return None, None


def build_manual_prune_candidates(
    *,
    records: Sequence[Mapping[str, Any]],
    artifacts_root: Path,
    current_by_family: Mapping[str, Sequence[Mapping[str, Any]]],
    target_date: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        path_text = str(record.get("resolved_path") or "")
        if not path_text:
            continue
        path = Path(path_text)
        try:
            path.resolve(strict=False).relative_to(artifacts_root.resolve())
        except ValueError:
            continue
        if not path.exists():
            continue
        reason, replacement = _manual_prune_reason(
            record,
            current_by_family=current_by_family,
            target_date=target_date,
        )
        if reason is None:
            continue
        candidates.append(
            {
                "path": path_text,
                "reason": reason,
                "family": record.get("family"),
                "classification": record.get("classification"),
                "as_of": record.get("as_of"),
                "replacement": replacement,
                "bytes": _path_size(path),
                "references": _record_references_summary(record),
                "metadata_issues": record.get("metadata_issues", []),
                "delete_mode": "manual-approval-required",
            }
        )
    return sorted(
        candidates,
        key=lambda item: (-int(item.get("bytes") or 0), str(item.get("path") or "")),
    )


def build_prune_plan(
    *,
    inventory: Mapping[str, Any],
    artifacts_root: Path,
    target_date: str,
) -> dict[str, Any]:
    records = [record for record in inventory.get("records") or [] if isinstance(record, Mapping)]
    current_by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("classification") == "current" and record.get("family"):
            current_by_family[str(record["family"])].append(record)

    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for record in records:
        path_text = str(record.get("resolved_path") or "")
        if not path_text:
            continue
        path = Path(path_text)
        try:
            path.relative_to(artifacts_root)
        except ValueError:
            continue
        classification = str(record.get("classification") or "unreferenced")
        manifest = record.get("manifest") if isinstance(record.get("manifest"), Mapping) else None
        if _is_provider_permission_manifest(manifest):
            protected.append(
                {
                    "path": path_text,
                    "classification": classification,
                    "references": record.get("references", []),
                    "reason": "provider_permission_boundary_evidence",
                }
            )
            continue
        if classification != "unreferenced":
            protected.append(
                {
                    "path": path_text,
                    "classification": classification,
                    "references": record.get("references", []),
                    "reason": "referenced_or_metadata_inconsistent",
                }
            )
            continue

        family = str(record.get("family") or "")
        replacement = None
        for current in current_by_family.get(family, []):
            current_as_of = _norm_date(current.get("as_of"))
            record_as_of = _norm_date(record.get("as_of"))
            if current_as_of and (record_as_of is None or current_as_of >= max(record_as_of, target_date)):
                replacement = current.get("resolved_path")
                break
        name = path.name.lower()
        reason = None
        if any(token in name for token in ("patch", "repair", "broken", "tmp")):
            reason = "obsolete_intermediate_artifact"
        elif replacement:
            reason = "superseded_by_current_replacement"
        if reason:
            candidates.append(
                {
                    "path": path_text,
                    "reason": reason,
                    "replacement": replacement,
                    "bytes": _path_size(path),
                    "classification": "deletion-candidate",
                    "references": _record_references_summary(record),
                    "references_checked": ["current"],
                    "soft_references_ignored_for_prune": ["release", "report"],
                }
            )
        else:
            protected.append(
                {
                    "path": path_text,
                    "classification": "protected",
                    "references": [],
                    "reason": "unreferenced_but_no_safe_replacement_evidence",
                }
            )
    manual_candidates = build_manual_prune_candidates(
        records=records,
        artifacts_root=artifacts_root,
        current_by_family=current_by_family,
        target_date=target_date,
    )
    return {
        "summary": {
            "candidates": len(candidates),
            "protected": len(protected),
            "candidate_bytes": sum(int(item.get("bytes") or 0) for item in candidates),
            "manual_review_candidates": len(manual_candidates),
            "manual_review_candidate_bytes": sum(
                int(item.get("bytes") or 0) for item in manual_candidates
            ),
        },
        "candidates": candidates,
        "manual_review_candidates": manual_candidates,
        "protected": protected,
    }


def execute_prune_plan(
    candidates: Sequence[Mapping[str, Any]],
    *,
    delete: bool,
    approved_paths: set[str],
    artifacts_root: Path,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        path_text = str(candidate.get("path") or "")
        path = Path(path_text)
        try:
            path.resolve().relative_to(artifacts_root.resolve())
        except ValueError:
            results.append({"path": path_text, "status": "skipped", "reason": "outside_artifacts_root"})
            continue
        if not delete:
            results.append({"path": path_text, "status": "dry-run", "reason": "delete_flag_not_set"})
            continue
        if path_text not in approved_paths and path.resolve().as_posix() not in approved_paths:
            results.append({"path": path_text, "status": "skipped", "reason": "path_not_approved"})
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except Exception as exc:
            results.append({"path": path_text, "status": "failed", "reason": str(exc)})
        else:
            results.append({"path": path_text, "status": "deleted"})
    return {"delete": delete, "approved_paths": sorted(approved_paths), "results": results}


def _freshness_quality_checks(freshness: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for asset_key, result in freshness.items():
        if not isinstance(result, Mapping) or asset_key == "refresh":
            continue
        for issue in result.get("issues") or []:
            if not isinstance(issue, Mapping):
                continue
            checks.append(
                {
                    "check": issue.get("code", "freshness_issue"),
                    "asset_key": asset_key,
                    "severity": issue.get("severity", "warning"),
                    "classification": issue.get("classification"),
                    "affected_items": issue.get("affected_symbols") or issue.get("affected_items") or 1,
                    "sample_rows": issue.get("sample_symbols") or issue.get("sample_rows") or [],
                }
            )
    return checks


def _inventory_quality_checks(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for record in inventory.get("records") or []:
        if not isinstance(record, Mapping):
            continue
        for issue in record.get("metadata_issues") or []:
            if not isinstance(issue, Mapping):
                continue
            checks.append(
                {
                    "check": issue.get("code", "metadata_issue"),
                    "severity": issue.get("severity", "warning"),
                    "asset_key": ",".join(record.get("asset_keys") or []),
                    "path": record.get("resolved_path"),
                    "affected_items": 1,
                }
            )
    return checks


def _health_quality_checks(health: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for issue in health.get("merged_issues") or []:
        if not isinstance(issue, Mapping):
            continue
        severity = str(issue.get("severity") or "info")
        checks.append(
            {
                "check": issue.get("check", "health_issue"),
                "source": issue.get("source"),
                "severity": severity if severity in {"info", "warning", "error"} else "info",
                "affected_items": issue.get("affected_items") or issue.get("affected_symbols") or 1,
            }
        )
    return checks


def _run_refresh_workflow(args: Any, *, repo_root: Path, target_date: str, reports_dir: Path) -> dict[str, Any]:
    if not bool(getattr(args, "run_refresh", False)):
        return {"requested": False, "status": "skipped"}
    report_path = reports_dir / f"hk_asset_refresh_{target_date}.json"
    command = [
        sys.executable,
        "scripts/internal/run_hk_asset_workflow.py",
        "--phase",
        "refresh",
        "--phase",
        "inspect",
        "--target-date",
        target_date,
        "--refresh-mode",
        str(getattr(args, "refresh_mode", "patch") or "patch"),
        "--workflow-report",
        str(report_path),
    ]
    for asset in getattr(args, "refresh_asset", []) or []:
        command.extend(["--refresh-asset", str(asset)])
        if str(asset) in {
            "daily",
            "daily_clean",
            "valuation",
            "ex_factors",
            "dividends",
            "shares",
            "industry_changes",
            "southbound",
        }:
            command.extend(["--inspect-asset", str(asset)])
    if getattr(args, "config", None):
        command.extend(["--config", str(args.config)])
    if getattr(args, "refresh_dry_run", False):
        command.append("--dry-run")
    completed = subprocess.run(command, cwd=repo_root, check=False, text=True, capture_output=True)
    return {
        "requested": True,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "command": command,
        "workflow_report": str(report_path),
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def build_hk_data_asset_audit_report(args: Any) -> dict[str, Any]:
    repo_root = find_repo_root()
    artifacts_root = _resolve_optional_path(getattr(args, "artifacts_root", "artifacts"), repo_root=repo_root) or (repo_root / "artifacts")
    reports_dir = _resolve_optional_path(getattr(args, "reports_dir", None), repo_root=repo_root) or (artifacts_root / "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    current_contract_path = _resolve_optional_path(getattr(args, "current_contract", None), repo_root=repo_root)

    contract = load_current_contract(current_contract_path or default_hk_current_contract_path(artifacts_root))
    contract_target = None
    if isinstance(contract, Mapping):
        contract_meta = contract.get("contract") if isinstance(contract.get("contract"), Mapping) else {}
        contract_target = _norm_date(contract_meta.get("target_date"))
    target_date = _norm_date(getattr(args, "target_date", None)) or contract_target or datetime.now().strftime("%Y%m%d")
    target_date_source = "explicit" if getattr(args, "target_date", None) else ("contract" if contract_target else "today")

    selected_assets = list(getattr(args, "asset", []) or [])
    scan_families = list(getattr(args, "scan_family", []) or []) or list(_DEFAULT_SNAPSHOT_FAMILIES)
    pre_inventory = collect_inventory(
        artifacts_root=artifacts_root,
        current_contract_path=current_contract_path,
        selected_assets=selected_assets,
        scan_families=scan_families,
    )
    pre_freshness = {
        "etf_daily": verify_etf_daily_completeness(
            inventory=pre_inventory,
            target_date=target_date,
            scan_data=not bool(getattr(args, "metadata_only_etf_daily", False)),
            sample_limit=int(getattr(args, "sample_limit", 5) or 5),
        ),
        "intraday": verify_intraday_freshness(
            inventory=pre_inventory,
            target_date=target_date,
            mode=str(getattr(args, "intraday_mode", "metadata") or "metadata"),
            daily_asset_dir=_asset_dir_from_record(_current_record(pre_inventory, "daily")),
            sample_limit=int(getattr(args, "sample_limit", 5) or 5),
            fail_on_severity=str(getattr(args, "fail_on_severity", "none") or "none"),
        ),
    }

    refresh_result = _run_refresh_workflow(args, repo_root=repo_root, target_date=target_date, reports_dir=reports_dir)
    if refresh_result.get("requested"):
        inventory = collect_inventory(
            artifacts_root=artifacts_root,
            current_contract_path=current_contract_path,
            selected_assets=selected_assets,
            scan_families=scan_families,
        )
        final_freshness = {
            "etf_daily": verify_etf_daily_completeness(
                inventory=inventory,
                target_date=target_date,
                scan_data=not bool(getattr(args, "metadata_only_etf_daily", False)),
                sample_limit=int(getattr(args, "sample_limit", 5) or 5),
            ),
            "intraday": verify_intraday_freshness(
                inventory=inventory,
                target_date=target_date,
                mode=str(getattr(args, "intraday_mode", "metadata") or "metadata"),
                daily_asset_dir=_asset_dir_from_record(_current_record(inventory, "daily")),
                sample_limit=int(getattr(args, "sample_limit", 5) or 5),
                fail_on_severity=str(getattr(args, "fail_on_severity", "none") or "none"),
            ),
        }
    else:
        inventory = pre_inventory
        final_freshness = pre_freshness

    extra_reports = [
        resolve_repo_path(path, repo_root=repo_root)
        for path in (getattr(args, "health_report", []) or [])
    ]
    intraday_mode = str(getattr(args, "intraday_mode", "metadata") or "metadata")
    expected_report_kinds = [
        kind
        for kind in _EXPECTED_REPORT_KINDS
        if kind != "intraday_health" or intraday_mode == "health"
    ]
    health = aggregate_health_reports(
        reports_dir=reports_dir,
        target_date=target_date,
        extra_reports=extra_reports,
        expected_report_kinds=expected_report_kinds,
    )
    freshness = {
        **final_freshness,
        "refresh": {
            **refresh_result,
            "pre_refresh": pre_freshness,
            "post_refresh": final_freshness if refresh_result.get("requested") else None,
        },
    }
    candidates = build_repair_candidates(
        freshness=final_freshness,
        inventory=inventory,
        health=health,
        target_date=target_date,
    )
    repair_execution = execute_repair_candidates(
        candidates,
        approved_actions=set(getattr(args, "approved_repair_action", []) or []),
        execute=bool(getattr(args, "execute_repair", False)),
        repo_root=repo_root,
    )
    prune = build_prune_plan(inventory=inventory, artifacts_root=artifacts_root, target_date=target_date)
    prune["delete_result"] = execute_prune_plan(
        prune["candidates"],
        delete=bool(getattr(args, "delete_prune_candidates", False)),
        approved_paths={str(resolve_repo_path(path, repo_root=repo_root)) for path in (getattr(args, "approved_prune_path", []) or [])},
        artifacts_root=artifacts_root,
    )

    quality_checks = [
        *_inventory_quality_checks(inventory),
        *_freshness_quality_checks(final_freshness),
        *_health_quality_checks(health),
    ]
    quality_verdict = summarize_quality_checks(
        quality_checks,
        fail_on_severity=getattr(args, "fail_on_severity", "none"),
    )
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "generated_by": "inspect-hk-data-assets",
        "repo_root": str(repo_root),
        "artifacts_root": str(artifacts_root),
        "target_date": target_date,
        "target_date_iso": _iso_date(target_date),
        "target_date_source": target_date_source,
        "inventory": inventory,
        "freshness": freshness,
        "health": health,
        "repair": {
            "summary": {
                "candidates": len(candidates),
                "auto_executable": sum(1 for item in candidates if item.get("auto_executable")),
            },
            "candidates": candidates,
            "execution": repair_execution,
        },
        "prune": prune,
        "quality_checks": quality_checks,
        "quality_verdict": quality_verdict,
    }


def _render_text(payload: Mapping[str, Any]) -> str:
    lines = [
        "HK Data Asset Audit",
        f"target_date: {payload.get('target_date')}",
        f"artifacts_root: {payload.get('artifacts_root')}",
        "",
        "Inventory",
    ]
    inventory_summary = payload.get("inventory", {}).get("summary", {}) if isinstance(payload.get("inventory"), Mapping) else {}
    for key in ("records", "current", "retained", "unreferenced", "metadata_inconsistent"):
        lines.append(f"{key}: {inventory_summary.get(key)}")
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), Mapping) else {}
    lines.extend(["", "Freshness"])
    for key in ("etf_daily", "intraday"):
        result = freshness.get(key) if isinstance(freshness.get(key), Mapping) else {}
        lines.append(
            f"{key}: status={result.get('status')} path={result.get('checked_path')} latest={result.get('effective_end_date') or result.get('latest_observed_trade_date')}"
        )
    health_summary = payload.get("health", {}).get("summary", {}) if isinstance(payload.get("health"), Mapping) else {}
    lines.extend(["", "Health", f"available_sources: {health_summary.get('available_sources')}", f"missing_sources: {health_summary.get('missing_sources')}"])
    repair_summary = payload.get("repair", {}).get("summary", {}) if isinstance(payload.get("repair"), Mapping) else {}
    prune_summary = payload.get("prune", {}).get("summary", {}) if isinstance(payload.get("prune"), Mapping) else {}
    lines.extend(["", "Repair And Prune", f"repair_candidates: {repair_summary.get('candidates')}", f"prune_candidates: {prune_summary.get('candidates')}"])
    append_quality_verdict_lines(lines, payload.get("quality_verdict") if isinstance(payload.get("quality_verdict"), Mapping) else None)
    return "\n".join(lines) + "\n"


def inspect_hk_data_assets(args: Any) -> int:
    payload = build_hk_data_asset_audit_report(args)
    output_format = str(getattr(args, "format", "text") or "text").strip().lower()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) if output_format == "json" else _render_text(payload)
    out_path = Path(args.out).expanduser().resolve() if getattr(args, "out", None) else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return quality_gate_exit_code(payload.get("quality_verdict") if isinstance(payload, Mapping) else None)
