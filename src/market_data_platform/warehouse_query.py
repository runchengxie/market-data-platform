from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


def import_duckdb():
    try:
        return import_module("duckdb")
    except ImportError as exc:  # pragma: no cover - exercised via CLI/SystemExit
        raise SystemExit("duckdb is not installed. Install with: uv sync --extra duckdb") from exc


def duckdb_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML payload is not an object: {path}")
    return payload


def standardized_manifests(root: Path) -> list[Path]:
    manifests: list[Path] = []
    for path in sorted(root.glob("**/manifest.yml")):
        if not path.is_file():
            continue
        payload = load_yaml(path)
        if payload.get("layer") == "standardized":
            manifests.append(path)
    return manifests


def register_standardized_views(conn, *, standardized_root: Path) -> int:
    manifests = standardized_manifests(standardized_root)
    conn.execute("CREATE SCHEMA IF NOT EXISTS standardized")
    registered = 0
    for manifest_path in manifests:
        payload = load_yaml(manifest_path)
        output_glob = payload.get("output_glob")
        if not output_glob:
            continue
        view_name = str(
            payload.get("view_name") or payload.get("name") or manifest_path.parent.name
        )
        query = (
            f'CREATE OR REPLACE VIEW standardized."{view_name}" AS '
            "SELECT * FROM read_parquet("
            f"{duckdb_sql_literal(str(output_glob))}, union_by_name = true)"
        )
        conn.execute(query)
        registered += 1
    return registered


def execute_standardized_query(
    sql_text: str,
    *,
    db_path: Path,
    standardized_root: Path,
) -> tuple[pd.DataFrame, int]:
    duckdb = import_duckdb()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        registered = register_standardized_views(conn, standardized_root=standardized_root)
        result = conn.execute(sql_text).df()
    finally:
        conn.close()
    return result, registered
