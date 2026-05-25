from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def load_manifest_summary(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"Manifest is not a mapping: {manifest_path}")

    query = _mapping(payload.get("query"))
    totals = _mapping(payload.get("totals"))
    date_range = _mapping(payload.get("date_range"))
    schema_version = str(payload.get("schema_version") or "").strip()
    dataset = str(payload.get("dataset") or "").strip() or None
    if dataset is None and schema_version:
        dataset = schema_version.split(".", 1)[0]

    row_count = payload.get("row_count")
    if row_count is not None and str(row_count).isdigit():
        totals.setdefault("rows", int(row_count))
    symbol_count = payload.get("symbol_count")
    if symbol_count is not None and str(symbol_count).isdigit():
        totals.setdefault("symbols", int(symbol_count))

    return {
        "manifest_path": str(manifest_path),
        "dataset": dataset,
        "schema_version": schema_version or None,
        "status": str(payload.get("status") or "").strip() or None,
        "query_start_date": query.get("start_date") or date_range.get("start"),
        "query_end_date": query.get("end_date") or date_range.get("end"),
        "totals": totals,
    }
