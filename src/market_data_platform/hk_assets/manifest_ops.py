from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .asset_io import _canonicalize_output_columns
from .models import (
    DailyMirrorAuditRecord,
    DailyMirrorEntry,
    DatedMirrorAuditRecord,
    DatedMirrorEntry,
    MirrorAuditRecord,
    MirrorEntry,
)
from .shared import _git_metadata, _load_existing_text_list, _load_manifest


def _validate_global_daily_resume_inputs(
    *,
    output_dir: Path,
    dataset_name: str,
    fields: Sequence[str],
    start_date: str,
    end_date: str,
) -> None:
    manifest = _load_manifest(output_dir / "manifest.yml")
    if manifest and manifest.get("dataset") not in {None, dataset_name}:
        raise SystemExit(
            f"Resume target dataset mismatch: expected {dataset_name!r}, got {manifest.get('dataset')!r}."
        )
    if manifest:
        query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
        checks = [
            ("start_date", start_date),
            ("end_date", end_date),
        ]
        for key, expected in checks:
            actual = query.get(key) if isinstance(query, Mapping) else None
            if actual not in {None, expected}:
                raise SystemExit(
                    f"Resume target query mismatch for {key}: expected {expected!r}, got {actual!r}."
                )

    existing_fields = _load_existing_text_list(output_dir / "fields.txt", strip=False)
    if existing_fields and list(existing_fields) != list(fields):
        raise SystemExit("Resume target fields.txt does not match the requested field list.")


def _validate_resume_inputs(
    *,
    output_dir: Path,
    dataset_name: str,
    fields: Sequence[str],
    symbols: Sequence[str],
    start_quarter: str,
    end_quarter: str,
    statements: str,
    query_date: str | None,
) -> None:
    manifest = _load_manifest(output_dir / "manifest.yml")
    if manifest and manifest.get("dataset") not in {None, dataset_name}:
        raise SystemExit(
            f"Resume target dataset mismatch: expected {dataset_name!r}, got {manifest.get('dataset')!r}."
        )
    if manifest:
        query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
        checks = [
            ("start_quarter", start_quarter),
            ("end_quarter", end_quarter),
            ("statements", statements),
            ("date", query_date),
        ]
        for key, expected in checks:
            actual = query.get(key) if isinstance(query, Mapping) else None
            if actual not in {None, expected}:
                raise SystemExit(
                    f"Resume target query mismatch for {key}: expected {expected!r}, got {actual!r}."
                )

    existing_fields = _load_existing_text_list(output_dir / "fields.txt", strip=False)
    if existing_fields and list(existing_fields) != list(fields):
        raise SystemExit("Resume target fields.txt does not match the requested field list.")
    existing_symbols = _load_existing_text_list(output_dir / "symbols.txt")
    if existing_symbols and list(existing_symbols) != list(symbols):
        raise SystemExit("Resume target symbols.txt does not match the requested symbol list.")


def _validate_daily_resume_inputs(
    *,
    output_dir: Path,
    dataset_name: str,
    fields: Sequence[str],
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    frequency: str,
    adjust_type: str | None,
    skip_suspended: bool,
) -> None:
    manifest = _load_manifest(output_dir / "manifest.yml")
    if manifest and manifest.get("dataset") not in {None, dataset_name}:
        raise SystemExit(
            f"Resume target dataset mismatch: expected {dataset_name!r}, got {manifest.get('dataset')!r}."
        )
    if manifest:
        query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
        checks = [
            ("start_date", start_date),
            ("end_date", end_date),
            ("frequency", frequency),
            ("adjust_type", adjust_type),
            ("skip_suspended", skip_suspended),
        ]
        for key, expected in checks:
            actual = query.get(key) if isinstance(query, Mapping) else None
            if actual not in {None, expected}:
                raise SystemExit(
                    f"Resume target query mismatch for {key}: expected {expected!r}, got {actual!r}."
                )

    existing_fields = _load_existing_text_list(output_dir / "fields.txt", strip=False)
    if existing_fields and list(existing_fields) != list(fields):
        raise SystemExit("Resume target fields.txt does not match the requested field list.")
    existing_symbols = _load_existing_text_list(output_dir / "symbols.txt")
    if existing_symbols and list(existing_symbols) != list(symbols):
        raise SystemExit("Resume target symbols.txt does not match the requested symbol list.")


def _validate_dated_resume_inputs(
    *,
    output_dir: Path,
    dataset_name: str,
    fields: Sequence[str],
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
) -> None:
    manifest = _load_manifest(output_dir / "manifest.yml")
    if manifest and manifest.get("dataset") not in {None, dataset_name}:
        raise SystemExit(
            f"Resume target dataset mismatch: expected {dataset_name!r}, got {manifest.get('dataset')!r}."
        )
    if manifest:
        query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
        checks = [
            ("start_date", start_date),
            ("end_date", end_date),
        ]
        for key, expected in checks:
            actual = query.get(key) if isinstance(query, Mapping) else None
            if actual not in {None, expected}:
                raise SystemExit(
                    f"Resume target query mismatch for {key}: expected {expected!r}, got {actual!r}."
                )

    existing_fields = _load_existing_text_list(output_dir / "fields.txt", strip=False)
    if existing_fields and list(existing_fields) != list(fields):
        raise SystemExit("Resume target fields.txt does not match the requested field list.")
    existing_symbols = _load_existing_text_list(output_dir / "symbols.txt")
    if existing_symbols and list(existing_symbols) != list(symbols):
        raise SystemExit("Resume target symbols.txt does not match the requested symbol list.")


def _build_manifest(
    *,
    dataset_name: str,
    api_name: str,
    output_dir: Path,
    fields: Sequence[str],
    field_metadata: Mapping[str, object],
    symbol_metadata: Mapping[str, object],
    symbols_requested: Sequence[str],
    entries: Sequence[MirrorEntry],
    missing_symbols: Sequence[str],
    query_date: str | None,
    start_quarter: str,
    end_quarter: str,
    statements: str,
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    audit_file: Path,
    audit_records: Sequence[MirrorAuditRecord],
    field_coverage: Sequence[Mapping[str, object]],
    started_at: str,
    finished_at: str,
    status: str,
    error: str | None,
    config_ref: str | None,
) -> dict:
    status_counts = Counter(item.status for item in audit_records)
    return {
        "name": output_dir.name,
        "created_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "error": error,
        "dataset": dataset_name,
        "api": api_name,
        "market": "hk",
        "config_ref": config_ref,
        "repo_root": str(Path.cwd().resolve()),
        "output_dir": str(output_dir),
        "query": {
            "start_quarter": start_quarter,
            "end_quarter": end_quarter,
            "date": query_date,
            "statements": statements,
            "fields_count": len(fields),
            "fields": list(fields),
            "field_profile": list(field_metadata.get("field_profile") or []),
            "fields_file": list(field_metadata.get("fields_file") or []),
        },
        "symbol_source": dict(symbol_metadata),
        "columns": _canonicalize_output_columns(columns, preferred=("symbol", "order_book_id")),
        "audit_file": str(audit_file),
        "status_counts": dict(status_counts),
        "field_coverage": list(field_coverage),
        "batches": list(batches),
        "entries": [
            {
                "symbol": item.symbol,
                "order_book_id": item.order_book_id,
                "path": str(item.path),
                "rows": item.rows,
                "total_bytes": item.total_bytes,
                "min_quarter": item.min_quarter,
                "max_quarter": item.max_quarter,
                "min_info_date": item.min_info_date,
                "max_info_date": item.max_info_date,
            }
            for item in entries
        ],
        "missing_symbols": list(missing_symbols),
        "failed_symbols": [item.symbol for item in audit_records if item.status == "failed"],
        "quota_blocked_symbols": [item.symbol for item in audit_records if item.status == "quota_blocked"],
        "provider_permission_blocked_symbols": [
            item.symbol for item in audit_records if item.status == "provider_permission_blocked"
        ],
        "totals": {
            "symbols_requested": len(symbols_requested),
            "symbols_written": len(entries),
            "symbols_newly_written": int(status_counts.get("written", 0)),
            "symbols_skipped_existing": int(status_counts.get("skipped_existing", 0)),
            "symbols_missing_remote": int(status_counts.get("missing_remote", 0)),
            "symbols_failed": int(status_counts.get("failed", 0)),
            "symbols_quota_blocked": int(status_counts.get("quota_blocked", 0)),
            "symbols_provider_permission_blocked": int(
                status_counts.get("provider_permission_blocked", 0)
            ),
            "files": len(entries),
            "rows": sum(item.rows for item in entries),
            "bytes": sum(item.total_bytes for item in entries),
        },
        "git": _git_metadata(Path.cwd().resolve()),
    }


def _build_daily_manifest(
    *,
    dataset_name: str,
    api_name: str,
    output_dir: Path,
    fields: Sequence[str],
    field_metadata: Mapping[str, object],
    symbol_metadata: Mapping[str, object],
    symbols_requested: Sequence[str],
    entries: Sequence[DailyMirrorEntry],
    missing_symbols: Sequence[str],
    start_date: str,
    end_date: str,
    frequency: str,
    adjust_type: str | None,
    skip_suspended: bool,
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    audit_file: Path,
    audit_records: Sequence[DailyMirrorAuditRecord],
    field_coverage: Sequence[Mapping[str, object]],
    started_at: str,
    finished_at: str,
    status: str,
    error: str | None,
    config_ref: str | None,
) -> dict:
    status_counts = Counter(item.status for item in audit_records)
    return {
        "name": output_dir.name,
        "created_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "error": error,
        "dataset": dataset_name,
        "api": api_name,
        "market": "hk",
        "config_ref": config_ref,
        "repo_root": str(Path.cwd().resolve()),
        "output_dir": str(output_dir),
        "query": {
            "start_date": start_date,
            "end_date": end_date,
            "frequency": frequency,
            "adjust_type": adjust_type,
            "skip_suspended": skip_suspended,
            "fields_count": len(fields),
            "fields": list(fields),
            "fields_file": list(field_metadata.get("fields_file") or []),
            "field_source": field_metadata.get("source"),
            "base_fields": list(field_metadata.get("base_fields") or []),
        },
        "symbol_source": dict(symbol_metadata),
        "columns": _canonicalize_output_columns(columns, preferred=("trade_date", "symbol", "order_book_id")),
        "audit_file": str(audit_file),
        "status_counts": dict(status_counts),
        "field_coverage": list(field_coverage),
        "batches": list(batches),
        "entries": [
            {
                "symbol": item.symbol,
                "order_book_id": item.order_book_id,
                "path": str(item.path),
                "rows": item.rows,
                "total_bytes": item.total_bytes,
                "min_trade_date": item.min_trade_date,
                "max_trade_date": item.max_trade_date,
            }
            for item in entries
        ],
        "missing_symbols": list(missing_symbols),
        "failed_symbols": [item.symbol for item in audit_records if item.status == "failed"],
        "quota_blocked_symbols": [item.symbol for item in audit_records if item.status == "quota_blocked"],
        "provider_permission_blocked_symbols": [
            item.symbol for item in audit_records if item.status == "provider_permission_blocked"
        ],
        "totals": {
            "symbols_requested": len(symbols_requested),
            "symbols_written": len(entries),
            "symbols_newly_written": int(status_counts.get("written", 0)),
            "symbols_skipped_existing": int(status_counts.get("skipped_existing", 0)),
            "symbols_missing_remote": int(status_counts.get("missing_remote", 0)),
            "symbols_failed": int(status_counts.get("failed", 0)),
            "symbols_quota_blocked": int(status_counts.get("quota_blocked", 0)),
            "symbols_provider_permission_blocked": int(
                status_counts.get("provider_permission_blocked", 0)
            ),
            "files": len(entries),
            "rows": sum(item.rows for item in entries),
            "bytes": sum(item.total_bytes for item in entries),
        },
        "git": _git_metadata(Path.cwd().resolve()),
    }


def _build_dated_manifest(
    *,
    dataset_name: str,
    api_name: str,
    output_dir: Path,
    fields: Sequence[str],
    field_metadata: Mapping[str, object],
    symbol_metadata: Mapping[str, object],
    symbols_requested: Sequence[str],
    entries: Sequence[DatedMirrorEntry],
    missing_symbols: Sequence[str],
    start_date: str,
    end_date: str,
    date_column: str,
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    audit_file: Path,
    audit_records: Sequence[DatedMirrorAuditRecord],
    field_coverage: Sequence[Mapping[str, object]],
    started_at: str,
    finished_at: str,
    status: str,
    error: str | None,
    config_ref: str | None,
) -> dict:
    status_counts = Counter(item.status for item in audit_records)
    return {
        "name": output_dir.name,
        "created_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "error": error,
        "dataset": dataset_name,
        "api": api_name,
        "market": "hk",
        "config_ref": config_ref,
        "repo_root": str(Path.cwd().resolve()),
        "output_dir": str(output_dir),
        "query": {
            "start_date": start_date,
            "end_date": end_date,
            "date_column": date_column,
            "fields_count": len(fields),
            "fields": list(fields),
            "fields_file": list(field_metadata.get("fields_file") or []),
            "field_source": field_metadata.get("source"),
            "base_fields": list(field_metadata.get("base_fields") or []),
        },
        "symbol_source": dict(symbol_metadata),
        "columns": _canonicalize_output_columns(columns, preferred=("symbol", "order_book_id", date_column)),
        "audit_file": str(audit_file),
        "status_counts": dict(status_counts),
        "field_coverage": list(field_coverage),
        "batches": list(batches),
        "entries": [
            {
                "symbol": item.symbol,
                "order_book_id": item.order_book_id,
                "path": str(item.path),
                "rows": item.rows,
                "total_bytes": item.total_bytes,
                "min_date": item.min_date,
                "max_date": item.max_date,
            }
            for item in entries
        ],
        "missing_symbols": list(missing_symbols),
        "failed_symbols": [item.symbol for item in audit_records if item.status == "failed"],
        "quota_blocked_symbols": [item.symbol for item in audit_records if item.status == "quota_blocked"],
        "totals": {
            "symbols_requested": len(symbols_requested),
            "symbols_written": len(entries),
            "symbols_newly_written": int(status_counts.get("written", 0)),
            "symbols_skipped_existing": int(status_counts.get("skipped_existing", 0)),
            "symbols_missing_remote": int(status_counts.get("missing_remote", 0)),
            "symbols_failed": int(status_counts.get("failed", 0)),
            "symbols_quota_blocked": int(status_counts.get("quota_blocked", 0)),
            "files": len(entries),
            "rows": sum(item.rows for item in entries),
            "bytes": sum(item.total_bytes for item in entries),
        },
        "git": _git_metadata(Path.cwd().resolve()),
    }
