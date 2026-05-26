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
    files = payload.get("files")
    if isinstance(files, list):
        totals.setdefault("files", len(files))

    output_dir = str(payload.get("output_dir") or "").strip()
    if not output_dir and payload.get("source_path") is not None:
        output_dir = str(manifest_path.parent)

    query_start_date = None
    for key in ("start_date", "start", "from"):
        value = query.get(key)
        if value is not None:
            query_start_date = str(value).strip() or None
            if query_start_date:
                break
    if not query_start_date:
        query_start_date = date_range.get("start")

    query_end_date = None
    for key in ("end_date", "date", "mapping_date", "as_of_date"):
        value = query.get(key)
        if value is not None:
            query_end_date = str(value).strip() or None
            if query_end_date:
                break
    if not query_end_date:
        query_end_date = date_range.get("end")

    return {
        "manifest_path": str(manifest_path),
        "dataset": dataset,
        "provider": str(payload.get("provider") or "").strip() or None,
        "schema_version": schema_version or None,
        "status": str(payload.get("status") or "").strip() or None,
        "output_dir": output_dir or None,
        "snapshot_name": Path(output_dir).name if output_dir else manifest_path.parent.name,
        "query_start_date": query_start_date,
        "query_end_date": query_end_date,
        "totals": totals,
    }
