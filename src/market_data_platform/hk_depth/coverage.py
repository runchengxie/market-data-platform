"""Coverage and validation helpers for raw depth snapshot parquet parts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from market_data_platform.hk_depth.storage import (
    discover_parquet_parts,
    infer_part_layout,
    parse_symbol_date_part_path,
)

IDENTITY_COLUMNS = ("order_book_id", "datetime", "trading_date")
REQUIRED_VALIDATION_COLUMNS = ("order_book_id", "datetime", "trading_date")
VALID_STATUS = "valid"
STATUS_MISSING = "missing"
STATUS_UNREADABLE = "unreadable"
STATUS_IDENTITY_MISMATCH = "identity_mismatch"
STATUS_SCHEMA_MISMATCH = "schema_mismatch"
STATUS_FIELD_MISMATCH = "field_mismatch"
STATUS_EMPTY_OR_INCOMPLETE = "empty_or_incomplete"


def fields_fingerprint(fields: Sequence[str]) -> str:
    payload = json.dumps(list(fields), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def schema_fingerprint(columns: Sequence[tuple[str, str]] | Sequence[str]) -> str:
    payload = json.dumps(list(columns), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _schema_columns(parquet_file: pq.ParquetFile) -> list[tuple[str, str]]:
    schema = parquet_file.schema_arrow
    return [(field.name, str(field.type)) for field in schema]


def _compression(parquet_file: pq.ParquetFile) -> str | None:
    metadata = parquet_file.metadata
    if metadata is None or metadata.num_row_groups == 0:
        return None
    row_group = metadata.row_group(0)
    if row_group.num_columns == 0:
        return None
    value = row_group.column(0).compression
    return str(value).lower() if value is not None else None


def _path_trade_date(path: Path) -> str | None:
    for parent in [path.parent, *path.parents]:
        if parent.name.startswith("trade_date="):
            return parent.name.removeprefix("trade_date=")
    return None


def _base_row(
    *,
    path: Path,
    status: str,
    order_book_id: str | None = None,
    trading_date: str | None = None,
    row_count: int = 0,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    fields: Sequence[str] = (),
    schema_columns: Sequence[tuple[str, str]] | Sequence[str] = (),
    compression: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    layout = infer_part_layout(path)
    return {
        "order_book_id": order_book_id,
        "trading_date": trading_date,
        "row_count": int(row_count),
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        "fields": list(fields),
        "fields_hash": fields_fingerprint(fields),
        "schema_fingerprint": schema_fingerprint(schema_columns),
        "layout_version": layout,
        "source_layout": layout,
        "compression": compression,
        "file_path": str(path),
        "status": status,
        "reason": reason,
    }


def _read_identity_frame(path: Path, columns: Sequence[str]) -> pd.DataFrame:
    selected = [column for column in REQUIRED_VALIDATION_COLUMNS if column in columns]
    if not selected:
        return pd.DataFrame()
    return pd.read_parquet(path, columns=selected)


def _field_sets_match(data_fields: Sequence[str], requested_fields: Sequence[str]) -> bool:
    return len(data_fields) == len(requested_fields) and set(data_fields) == set(requested_fields)


def inspect_raw_part(
    path: str | Path,
    *,
    requested_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Inspect one parquet part and return symbol-date coverage rows."""
    part = Path(path)
    path_trade_date, path_symbol = parse_symbol_date_part_path(part)
    path_trade_date = path_trade_date or _path_trade_date(part)

    try:
        parquet_file = pq.ParquetFile(part)
        schema_columns = _schema_columns(parquet_file)
        columns = [name for name, _ in schema_columns]
        row_count = int(parquet_file.metadata.num_rows if parquet_file.metadata else 0)
        compression = _compression(parquet_file)
    except Exception as exc:
        return [
            _base_row(
                path=part,
                status=STATUS_UNREADABLE,
                order_book_id=path_symbol,
                trading_date=path_trade_date,
                reason=str(exc),
            )
        ]

    data_fields = [column for column in columns if column not in IDENTITY_COLUMNS]
    requested = list(requested_fields)
    base_status = VALID_STATUS
    reason: str | None = None

    missing_required = [column for column in REQUIRED_VALIDATION_COLUMNS if column not in columns]
    if missing_required:
        base_status = STATUS_SCHEMA_MISMATCH
        reason = f"missing required columns: {', '.join(missing_required)}"
    elif requested and not _field_sets_match(data_fields, requested):
        base_status = STATUS_FIELD_MISMATCH
        reason = "parquet fields do not match requested fields"
    elif row_count == 0:
        base_status = STATUS_EMPTY_OR_INCOMPLETE
        reason = "parquet part has zero rows"

    if base_status == STATUS_SCHEMA_MISMATCH:
        return [
            _base_row(
                path=part,
                status=base_status,
                order_book_id=path_symbol,
                trading_date=path_trade_date,
                row_count=row_count,
                fields=data_fields,
                schema_columns=schema_columns,
                compression=compression,
                reason=reason,
            )
        ]

    try:
        identities = _read_identity_frame(part, columns)
    except Exception as exc:
        return [
            _base_row(
                path=part,
                status=STATUS_UNREADABLE,
                order_book_id=path_symbol,
                trading_date=path_trade_date,
                row_count=row_count,
                fields=data_fields,
                schema_columns=schema_columns,
                compression=compression,
                reason=str(exc),
            )
        ]

    if identities.empty:
        return [
            _base_row(
                path=part,
                status=base_status,
                order_book_id=path_symbol,
                trading_date=path_trade_date,
                row_count=row_count,
                fields=data_fields,
                schema_columns=schema_columns,
                compression=compression,
                reason=reason,
            )
        ]

    identities = identities.copy()
    identities["order_book_id"] = identities["order_book_id"].astype("string")
    identities["trading_date"] = identities["trading_date"].astype("string")
    timestamps = pd.to_datetime(identities["datetime"], errors="coerce")
    identities["__timestamp"] = timestamps

    path_identity_conflict = False
    if infer_part_layout(part) == "symbol_date.v1":
        if path_symbol is None or path_trade_date is None:
            path_identity_conflict = True
        elif (
            set(identities["order_book_id"].dropna().astype(str)) != {path_symbol}
            or set(identities["trading_date"].dropna().astype(str)) != {path_trade_date}
        ):
            path_identity_conflict = True
    elif path_trade_date is not None:
        if set(identities["trading_date"].dropna().astype(str)) - {path_trade_date}:
            path_identity_conflict = True

    status = STATUS_IDENTITY_MISMATCH if path_identity_conflict else base_status
    if path_identity_conflict:
        reason = "parquet identity does not match path identity"

    rows: list[dict[str, Any]] = []
    for (symbol, trade_date), group in identities.groupby(
        ["order_book_id", "trading_date"],
        sort=True,
        dropna=False,
    ):
        valid_timestamps = group["__timestamp"].dropna()
        rows.append(
            _base_row(
                path=part,
                status=status,
                order_book_id=str(symbol) if pd.notna(symbol) else None,
                trading_date=str(trade_date) if pd.notna(trade_date) else None,
                row_count=len(group),
                timestamp_start=(
                    valid_timestamps.min().isoformat() if not valid_timestamps.empty else None
                ),
                timestamp_end=(
                    valid_timestamps.max().isoformat() if not valid_timestamps.empty else None
                ),
                fields=data_fields,
                schema_columns=schema_columns,
                compression=compression,
                reason=reason,
            )
        )

    if path_identity_conflict and path_symbol and path_trade_date:
        has_path_row = any(
            row["order_book_id"] == path_symbol and row["trading_date"] == path_trade_date
            for row in rows
        )
        if not has_path_row:
            rows.append(
                _base_row(
                    path=part,
                    status=STATUS_IDENTITY_MISMATCH,
                    order_book_id=path_symbol,
                    trading_date=path_trade_date,
                    row_count=row_count,
                    fields=data_fields,
                    schema_columns=schema_columns,
                    compression=compression,
                    reason=reason,
                )
            )
    return rows


def scan_raw_coverage(
    input_root: str | Path,
    *,
    requested_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for part in discover_parquet_parts(input_root):
        rows.extend(inspect_raw_part(part, requested_fields=requested_fields))
    return rows


def coverage_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    layouts: set[str] = set()
    compressions: set[str] = set()
    valid_units: set[tuple[str, str]] = set()
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if row.get("layout_version"):
            layouts.add(str(row["layout_version"]))
        if row.get("compression"):
            compressions.add(str(row["compression"]))
        if row.get("status") == VALID_STATUS and row.get("trading_date") and row.get(
            "order_book_id"
        ):
            valid_units.add((str(row["trading_date"]), str(row["order_book_id"])))
    return {
        "rows": len(rows),
        "valid_units": len(valid_units),
        "status_counts": status_counts,
        "layout_versions": sorted(layouts),
        "compressions": sorted(compressions),
    }
