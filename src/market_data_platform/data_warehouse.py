from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd
import yaml

from .artifacts import (
    default_path_text,
    resolve_artifacts_root,
    resolve_metadata_db_path,
    resolve_repo_path,
    resolve_warehouse_db_path,
    standardized_dir_for,
)
from .symbols import resolve_symbol_series


PRESET_DEFAULTS: dict[str, dict[str, str]] = {
    "rqdata-daily": {
        "dataset": "daily",
        "date_col": "trade_date",
        "symbol_col": "symbol",
    },
    "pit-fundamentals": {
        "dataset": "pit_fundamentals",
        "date_col": "trade_date",
        "symbol_col": "symbol",
    },
    "industry-labels": {
        "dataset": "industry_labels",
        "date_col": "trade_date",
        "symbol_col": "symbol",
    },
    "generic": {
        "dataset": "generic",
        "date_col": "trade_date",
        "symbol_col": "symbol",
    },
}
ARTIFACTS_ROOT_HELP = (
    "Default: DATA_PLATFORM_ROOT, HK_DATA_PLATFORM_ROOT, CSTREE_ARTIFACTS_ROOT, or artifacts/."
)

FREQUENCY_ALIASES = {
    "D": "D",
    "DAY": "D",
    "DAILY": "D",
    "M": "M",
    "MONTH": "M",
    "MONTHLY": "M",
    "Q": "Q",
    "QUARTER": "Q",
    "QUARTERLY": "Q",
}


@dataclass(frozen=True)
class CatalogArtifact:
    artifact_id: str
    layer: str
    dataset: str | None
    market: str | None
    name: str
    path: str
    manifest_path: str
    status: str | None
    output_format: str | None
    created_at: str | None
    start_value: str | None
    end_value: str | None
    row_count: int | None
    symbol_count: int | None
    trade_date_count: int | None
    file_count: int | None
    total_bytes: int | None
    frequency: str | None
    source_asset_dir: str | None
    source_manifest: str | None
    view_name: str | None
    metadata_json: str


@dataclass(frozen=True)
class CatalogLineage:
    artifact_id: str
    relation: str
    source_path: str


@dataclass(frozen=True)
class MaterializeStats:
    input_files: int
    input_rows: int
    output_rows: int
    output_files: int
    symbols: int
    trade_dates: int
    trade_date_min: str | None
    trade_date_max: str | None
    rows_missing_date_dropped: int
    rows_missing_symbol_dropped: int
    duplicate_rows_dropped: int


def _timestamp_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_git_value(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def _git_metadata(repo_root: Path) -> dict | None:
    commit = _read_git_value(repo_root, "rev-parse", "HEAD")
    if not commit:
        return None
    short_commit = _read_git_value(repo_root, "rev-parse", "--short", "HEAD")
    branch = _read_git_value(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _read_git_value(repo_root, "status", "--short")
    return {
        "commit": commit,
        "short_commit": short_commit,
        "branch": branch,
        "is_dirty": bool(status),
    }


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise SystemExit(f"Manifest root must be a mapping: {path}")
    return payload


def _maybe_int(value) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _coerce_frequency(value: str | None) -> str:
    text = str(value or "D").strip().upper()
    if text not in FREQUENCY_ALIASES:
        raise SystemExit("frequency must be one of D, M, or Q.")
    return FREQUENCY_ALIASES[text]


def _sanitize_identifier(text: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", str(text or "").strip()).strip("_").lower()
    if not value:
        value = "dataset"
    if value[0].isdigit():
        value = f"v_{value}"
    return value


def _duckdb_sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _source_manifest_for_file(path: Path) -> Path | None:
    candidate = path.with_name(f"{path.stem}.manifest.yml")
    if candidate.exists():
        return candidate
    return None


def _infer_source_manifest(*, asset_dir: Path | None, file_path: Path | None) -> Path | None:
    if asset_dir is not None:
        candidate = asset_dir / "manifest.yml"
        if candidate.exists():
            return candidate
    if file_path is not None:
        return _source_manifest_for_file(file_path)
    return None


def _collect_input_files(*, asset_dir: Path | None, file_path: Path | None) -> tuple[list[Path], str]:
    if asset_dir is not None:
        data_dir = asset_dir / "data"
        if not data_dir.exists():
            raise SystemExit(f"Asset directory is missing data/: {asset_dir}")
        files = sorted(path for path in data_dir.glob("*.parquet") if path.is_file())
        if not files:
            raise SystemExit(f"No parquet files found under {data_dir}")
        return files, "asset_dir"
    if file_path is not None:
        if not file_path.exists():
            raise SystemExit(f"Input file not found: {file_path}")
        return [file_path], "file"
    raise SystemExit("Provide either --asset-dir or --file.")


def _read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    raise SystemExit(f"Unsupported input file type: {path}")


def _parse_trade_date(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    fallback_mask = parsed.isna()
    if fallback_mask.any():
        parsed.loc[fallback_mask] = pd.to_datetime(text.loc[fallback_mask], errors="coerce")
    return parsed.dt.normalize()


def _resample_frequency(frame: pd.DataFrame, frequency: str) -> pd.DataFrame:
    if frequency == "D" or frame.empty:
        return frame
    work = frame.sort_values(["symbol", "trade_date"]).copy()
    if frequency == "M":
        work["_bucket"] = work["trade_date"].dt.to_period("M")
    elif frequency == "Q":
        work["_bucket"] = work["trade_date"].dt.to_period("Q")
    else:  # pragma: no cover - guarded by _coerce_frequency
        raise SystemExit(f"Unsupported frequency: {frequency}")
    work = (
        work.groupby(["symbol", "_bucket"], sort=False, group_keys=False)
        .tail(1)
        .drop(columns=["_bucket"])
        .reset_index(drop=True)
    )
    return work


def _normalize_frame(
    frame: pd.DataFrame,
    *,
    source_path: Path,
    date_col: str,
    symbol_col: str,
    frequency: str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    if date_col not in frame.columns:
        raise SystemExit(f"Missing date column {date_col!r} in {source_path}")

    work = frame.copy()
    resolved_symbol_col = symbol_col if symbol_col in work.columns else None
    temp_symbol_col: str | None = None
    if resolved_symbol_col is None:
        if symbol_col != "symbol":
            raise SystemExit(f"Missing symbol column {symbol_col!r} in {source_path}")
        temp_symbol_col = "__resolved_symbol__"
        work[temp_symbol_col] = resolve_symbol_series(
            work,
            context=f"Materialize input {source_path}",
        )
        resolved_symbol_col = temp_symbol_col
    for reserved, alias in (
        ("trade_date", "source_trade_date"),
        ("symbol", "source_symbol"),
        ("trade_date_key", "source_trade_date_key"),
        ("_source_file", "source_source_file"),
        ("trade_year", "source_trade_year"),
    ):
        if reserved in work.columns and reserved not in {date_col, symbol_col}:
            work = work.rename(columns={reserved: alias})
    source_date_col = date_col
    source_symbol_col = resolved_symbol_col
    if date_col == "trade_date":
        work = work.rename(columns={"trade_date": "source_trade_date"})
        source_date_col = "source_trade_date"
    if resolved_symbol_col == "symbol":
        work = work.rename(columns={"symbol": "source_symbol"})
        source_symbol_col = "source_symbol"

    parsed_dates = _parse_trade_date(work[source_date_col])
    rows_missing_date_dropped = int(parsed_dates.isna().sum())
    work.insert(0, "trade_date", parsed_dates)
    work = work[work["trade_date"].notna()].copy()

    normalized_symbol = work[source_symbol_col].astype(str).str.strip()
    rows_missing_symbol_dropped = int((normalized_symbol == "").sum())
    work.insert(1, "trade_date_key", work["trade_date"].dt.strftime("%Y%m%d"))
    work.insert(2, "symbol", normalized_symbol)
    if temp_symbol_col is not None:
        work = work.drop(columns=[temp_symbol_col], errors="ignore")
    work = work[work["symbol"] != ""].copy()
    work.insert(3, "_source_file", str(source_path))
    work = work.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    duplicate_rows_dropped = int(
        work.duplicated(subset=["trade_date", "symbol"], keep="last").sum()
    )
    work = work.drop_duplicates(subset=["trade_date", "symbol"], keep="last").reset_index(drop=True)
    work = _resample_frequency(work, frequency)
    work["trade_year"] = work["trade_date"].dt.strftime("%Y")
    return work, {
        "rows_missing_date_dropped": rows_missing_date_dropped,
        "rows_missing_symbol_dropped": rows_missing_symbol_dropped,
        "duplicate_rows_dropped": duplicate_rows_dropped,
    }


def _write_partitioned_parquet(frame: pd.DataFrame, *, output_data_dir: Path, part_index: int) -> int:
    if frame.empty:
        empty_dir = output_data_dir / "trade_year=empty"
        empty_dir.mkdir(parents=True, exist_ok=True)
        empty_path = empty_dir / f"part-{part_index:05d}.parquet"
        frame.drop(columns=["trade_year"], errors="ignore").to_parquet(empty_path, index=False)
        return 1

    files_written = 0
    for trade_year, part in frame.groupby("trade_year", sort=True):
        year_dir = output_data_dir / f"trade_year={trade_year}"
        year_dir.mkdir(parents=True, exist_ok=True)
        part_path = year_dir / f"part-{part_index + files_written:05d}.parquet"
        part.drop(columns=["trade_year"], errors="ignore").to_parquet(part_path, index=False)
        files_written += 1
    return files_written


def _build_materialize_manifest(
    *,
    name: str,
    dataset: str,
    market: str,
    preset: str,
    frequency: str,
    source_mode: str,
    asset_dir: Path | None,
    file_path: Path | None,
    source_manifest: Path | None,
    output_dir: Path,
    output_data_dir: Path,
    view_name: str,
    date_col: str,
    symbol_col: str,
    column_dtypes: Mapping[str, str],
    stats: MaterializeStats,
) -> dict:
    return {
        "name": name,
        "created_at": _timestamp_now(),
        "status": "completed",
        "layer": "standardized",
        "dataset": dataset,
        "market": market,
        "view_name": view_name,
        "frequency": frequency,
        "source_asset_dir": str(asset_dir) if asset_dir is not None else None,
        "source_file": str(file_path) if file_path is not None else None,
        "source_manifest": str(source_manifest) if source_manifest is not None else None,
        "source": {
            "mode": source_mode,
            "preset": preset,
            "asset_dir": str(asset_dir) if asset_dir is not None else None,
            "file": str(file_path) if file_path is not None else None,
            "source_manifest": str(source_manifest) if source_manifest is not None else None,
            "date_col": date_col,
            "symbol_col": symbol_col,
        },
        "output_root": str(output_dir),
        "output_glob": str((output_data_dir / "**" / "*.parquet").resolve()),
        "partitioning": {"columns": ["trade_year"]},
        "columns": list(column_dtypes.keys()),
        "column_dtypes": dict(column_dtypes),
        "totals": {
            "input_files": stats.input_files,
            "input_rows": stats.input_rows,
            "output_rows": stats.output_rows,
            "output_files": stats.output_files,
            "symbols": stats.symbols,
            "trade_dates": stats.trade_dates,
            "trade_date_min": stats.trade_date_min,
            "trade_date_max": stats.trade_date_max,
        },
        "quality": {
            "rows_missing_date_dropped": stats.rows_missing_date_dropped,
            "rows_missing_symbol_dropped": stats.rows_missing_symbol_dropped,
            "duplicate_rows_dropped": stats.duplicate_rows_dropped,
        },
        "git": _git_metadata(Path.cwd().resolve()),
    }


def _manifest_paths(artifacts_root: Path) -> Iterable[Path]:
    patterns = ("**/manifest.yml", "**/*.manifest.yml")
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(artifacts_root.glob(pattern)):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield resolved


def _infer_layer(path: Path, payload: Mapping) -> str:
    if payload.get("layer") == "standardized":
        return "standardized"
    dataset = str(payload.get("dataset") or "").strip()
    if payload.get("source_asset_dir") or dataset.endswith("_file"):
        return "derived"
    if payload.get("entries") and payload.get("repo_root"):
        return "snapshot"
    if "/assets/rqdata/" in path.as_posix():
        return "raw_asset"
    return "manifest"


def _artifact_path(path: Path, payload: Mapping) -> str:
    if payload.get("output_file"):
        return str(resolve_repo_path(payload.get("output_file")))
    if payload.get("output_root"):
        return str(resolve_repo_path(payload.get("output_root")))
    return str(path.parent.resolve())


def _extract_counts(payload: Mapping) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    totals = payload.get("totals") if isinstance(payload.get("totals"), Mapping) else {}
    row_count = _maybe_int(totals.get("output_rows"))
    if row_count is None:
        row_count = _maybe_int(totals.get("rows"))
    symbol_count = _maybe_int(totals.get("symbols"))
    trade_date_count = _maybe_int(totals.get("trade_dates"))
    file_count = _maybe_int(totals.get("output_files"))
    if file_count is None:
        file_count = _maybe_int(totals.get("files"))
    total_bytes = _maybe_int(totals.get("bytes"))
    return row_count, symbol_count, trade_date_count, file_count, total_bytes


def _extract_range(payload: Mapping) -> tuple[str | None, str | None, str | None]:
    query = payload.get("query") if isinstance(payload.get("query"), Mapping) else {}
    grid = payload.get("grid") if isinstance(payload.get("grid"), Mapping) else {}
    start_value = (
        query.get("start_date")
        or query.get("start_quarter")
        or grid.get("start_date")
        or payload.get("start_date")
    )
    end_value = (
        query.get("end_date")
        or query.get("end_quarter")
        or grid.get("end_date")
        or payload.get("end_date")
    )
    frequency = query.get("frequency") or payload.get("frequency")
    return (
        str(start_value) if start_value not in {None, ""} else None,
        str(end_value) if end_value not in {None, ""} else None,
        str(frequency) if frequency not in {None, ""} else None,
    )


def _catalog_artifact_from_manifest(path: Path, payload: Mapping) -> tuple[CatalogArtifact, list[str], list[CatalogLineage]]:
    dataset = str(payload.get("dataset") or "").strip() or None
    layer = _infer_layer(path, payload)
    created_at = payload.get("created_at")
    row_count, symbol_count, trade_date_count, file_count, total_bytes = _extract_counts(payload)
    start_value, end_value, frequency = _extract_range(payload)
    columns = list(payload.get("columns") or [])
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    metadata = {
        "query": payload.get("query"),
        "totals": payload.get("totals"),
        "quality": payload.get("quality"),
        "column_dtypes": payload.get("column_dtypes"),
    }
    artifact = CatalogArtifact(
        artifact_id=str(path.resolve()),
        layer=layer,
        dataset=dataset,
        market=str(payload.get("market") or "").strip() or None,
        name=str(payload.get("name") or path.parent.name),
        path=_artifact_path(path, payload),
        manifest_path=str(path.resolve()),
        status=str(payload.get("status") or "").strip() or None,
        output_format=str(payload.get("output_format") or "").strip() or None,
        created_at=str(created_at) if created_at not in {None, ""} else None,
        start_value=start_value,
        end_value=end_value,
        row_count=row_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        file_count=file_count,
        total_bytes=total_bytes,
        frequency=frequency,
        source_asset_dir=str(payload.get("source_asset_dir") or source.get("asset_dir") or "").strip() or None,
        source_manifest=str(payload.get("source_manifest") or source.get("source_manifest") or "").strip() or None,
        view_name=str(payload.get("view_name") or "").strip() or None,
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
    )
    lineages: list[CatalogLineage] = []
    if artifact.source_asset_dir:
        lineages.append(
            CatalogLineage(
                artifact_id=artifact.artifact_id,
                relation="source_asset_dir",
                source_path=artifact.source_asset_dir,
            )
        )
    if artifact.source_manifest:
        lineages.append(
            CatalogLineage(
                artifact_id=artifact.artifact_id,
                relation="source_manifest",
                source_path=artifact.source_manifest,
            )
        )
    source_file = str(payload.get("source_file") or source.get("file") or "").strip()
    if source_file:
        lineages.append(
            CatalogLineage(
                artifact_id=artifact.artifact_id,
                relation="source_file",
                source_path=source_file,
            )
        )
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        source = entry.get("source")
        if source:
            lineages.append(
                CatalogLineage(
                    artifact_id=artifact.artifact_id,
                    relation="snapshot_entry",
                    source_path=str(source),
                )
            )
    return artifact, columns, lineages


def _init_catalog_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            layer TEXT,
            dataset TEXT,
            market TEXT,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            status TEXT,
            output_format TEXT,
            created_at TEXT,
            start_value TEXT,
            end_value TEXT,
            row_count INTEGER,
            symbol_count INTEGER,
            trade_date_count INTEGER,
            file_count INTEGER,
            total_bytes INTEGER,
            frequency TEXT,
            source_asset_dir TEXT,
            source_manifest TEXT,
            view_name TEXT,
            metadata_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifact_columns (
            artifact_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            column_name TEXT NOT NULL,
            PRIMARY KEY (artifact_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS artifact_lineage (
            artifact_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            source_path TEXT NOT NULL
        );
        """
    )


def _write_catalog_summary_csv(conn: sqlite3.Connection, out_path: Path) -> None:
    rows = conn.execute(
        """
        SELECT
            artifact_id,
            layer,
            dataset,
            market,
            name,
            path,
            manifest_path,
            status,
            output_format,
            created_at,
            start_value,
            end_value,
            row_count,
            symbol_count,
            trade_date_count,
            file_count,
            total_bytes,
            frequency,
            source_asset_dir,
            source_manifest,
            view_name
        FROM artifacts
        ORDER BY layer, dataset, name
        """
    ).fetchall()
    headers = [
        "artifact_id",
        "layer",
        "dataset",
        "market",
        "name",
        "path",
        "manifest_path",
        "status",
        "output_format",
        "created_at",
        "start_value",
        "end_value",
        "row_count",
        "symbol_count",
        "trade_date_count",
        "file_count",
        "total_bytes",
        "frequency",
        "source_asset_dir",
        "source_manifest",
        "view_name",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=headers).to_csv(out_path, index=False)


def refresh_catalog(args) -> int:
    artifacts_root = resolve_artifacts_root(getattr(args, "artifacts_root", None))
    db_path = resolve_metadata_db_path(
        getattr(args, "db_path", None),
        artifacts_root=artifacts_root,
    )
    summary_out = resolve_repo_path(
        getattr(args, "summary_out", None)
        or (db_path.parent / "catalog_summary.csv")
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)

    manifests = list(_manifest_paths(artifacts_root))
    artifacts: list[CatalogArtifact] = []
    artifact_columns: list[tuple[str, int, str]] = []
    lineages: list[CatalogLineage] = []
    for manifest_path in manifests:
        payload = _load_yaml(manifest_path)
        artifact, columns, artifact_lineages = _catalog_artifact_from_manifest(manifest_path, payload)
        artifacts.append(artifact)
        artifact_columns.extend(
            (artifact.artifact_id, idx, str(column))
            for idx, column in enumerate(columns, start=1)
        )
        lineages.extend(artifact_lineages)

    with sqlite3.connect(db_path) as conn:
        _init_catalog_db(conn)
        conn.execute("DELETE FROM artifact_lineage")
        conn.execute("DELETE FROM artifact_columns")
        conn.execute("DELETE FROM artifacts")
        conn.executemany(
            """
            INSERT INTO artifacts (
                artifact_id, layer, dataset, market, name, path, manifest_path,
                status, output_format, created_at, start_value, end_value,
                row_count, symbol_count, trade_date_count, file_count, total_bytes,
                frequency, source_asset_dir, source_manifest, view_name, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.artifact_id,
                    item.layer,
                    item.dataset,
                    item.market,
                    item.name,
                    item.path,
                    item.manifest_path,
                    item.status,
                    item.output_format,
                    item.created_at,
                    item.start_value,
                    item.end_value,
                    item.row_count,
                    item.symbol_count,
                    item.trade_date_count,
                    item.file_count,
                    item.total_bytes,
                    item.frequency,
                    item.source_asset_dir,
                    item.source_manifest,
                    item.view_name,
                    item.metadata_json,
                )
                for item in artifacts
            ],
        )
        conn.executemany(
            "INSERT INTO artifact_columns (artifact_id, ordinal, column_name) VALUES (?, ?, ?)",
            artifact_columns,
        )
        conn.executemany(
            "INSERT INTO artifact_lineage (artifact_id, relation, source_path) VALUES (?, ?, ?)",
            [(item.artifact_id, item.relation, item.source_path) for item in lineages],
        )
        _write_catalog_summary_csv(conn, summary_out)
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    print(
        f"Catalog refreshed: {artifact_count} artifacts -> {db_path} "
        f"(summary: {summary_out})"
    )
    return 0


def _materialize_column_defaults(args) -> tuple[str, str, str]:
    preset = str(getattr(args, "preset", "generic") or "generic").strip().lower()
    if preset not in PRESET_DEFAULTS:
        raise SystemExit(f"Unsupported preset: {preset}")
    defaults = PRESET_DEFAULTS[preset]
    dataset = str(getattr(args, "dataset_name", None) or defaults["dataset"]).strip()
    date_col = str(getattr(args, "date_col", None) or defaults["date_col"]).strip()
    symbol_col = str(getattr(args, "symbol_col", None) or defaults["symbol_col"]).strip()
    if not dataset:
        raise SystemExit("dataset-name must not be empty.")
    return dataset, date_col, symbol_col


def materialize_standardized(args) -> int:
    preset = str(getattr(args, "preset", "generic") or "generic").strip().lower()
    dataset, date_col, symbol_col = _materialize_column_defaults(args)
    frequency = _coerce_frequency(getattr(args, "frequency", "D"))
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise SystemExit("--name is required.")

    asset_dir = resolve_repo_path(args.asset_dir) if getattr(args, "asset_dir", None) else None
    file_path = resolve_repo_path(args.file) if getattr(args, "file", None) else None
    input_files, source_mode = _collect_input_files(asset_dir=asset_dir, file_path=file_path)
    source_manifest = _infer_source_manifest(asset_dir=asset_dir, file_path=file_path)

    artifacts_root = resolve_artifacts_root(getattr(args, "artifacts_root", None))
    out_root = resolve_repo_path(
        getattr(args, "out_root", None)
        or standardized_dir_for(artifacts_root)
    )
    market = str(getattr(args, "market", "hk") or "hk").strip().lower()
    output_dir = out_root / market / dataset / name
    output_data_dir = output_dir / "data"
    if output_dir.exists():
        if not getattr(args, "force", False):
            raise SystemExit(f"Refusing to overwrite existing output: {output_dir}")
        shutil.rmtree(output_dir)
    output_data_dir.mkdir(parents=True, exist_ok=True)

    total_input_rows = 0
    total_output_rows = 0
    output_files = 0
    symbols_seen: set[str] = set()
    trade_date_keys: set[str] = set()
    date_min: str | None = None
    date_max: str | None = None
    rows_missing_date_dropped = 0
    rows_missing_symbol_dropped = 0
    duplicate_rows_dropped = 0
    column_dtypes: dict[str, str] = {}

    for index, input_path in enumerate(input_files):
        frame = _read_table(input_path)
        total_input_rows += int(len(frame))
        normalized, quality = _normalize_frame(
            frame,
            source_path=input_path,
            date_col=date_col,
            symbol_col=symbol_col,
            frequency=frequency,
        )
        rows_missing_date_dropped += quality["rows_missing_date_dropped"]
        rows_missing_symbol_dropped += quality["rows_missing_symbol_dropped"]
        duplicate_rows_dropped += quality["duplicate_rows_dropped"]
        if normalized.empty:
            continue

        if not column_dtypes:
            column_dtypes = {column: str(dtype) for column, dtype in normalized.drop(columns=["trade_year"]).dtypes.items()}

        total_output_rows += int(len(normalized))
        symbols_seen.update(normalized["symbol"].astype(str).unique().tolist())
        trade_date_keys.update(normalized["trade_date_key"].astype(str).unique().tolist())
        file_date_min = normalized["trade_date_key"].min()
        file_date_max = normalized["trade_date_key"].max()
        if file_date_min is not None and (date_min is None or file_date_min < date_min):
            date_min = str(file_date_min)
        if file_date_max is not None and (date_max is None or file_date_max > date_max):
            date_max = str(file_date_max)
        files_written = _write_partitioned_parquet(
            normalized,
            output_data_dir=output_data_dir,
            part_index=output_files + index,
        )
        output_files += files_written

    if not column_dtypes:
        empty_frame = pd.DataFrame(
            columns=["trade_date", "trade_date_key", "symbol", "_source_file"]
        )
        column_dtypes = {column: str(dtype) for column, dtype in empty_frame.dtypes.items()}
        _write_partitioned_parquet(
            empty_frame.assign(trade_year="empty"),
            output_data_dir=output_data_dir,
            part_index=0,
        )
        output_files = 1

    view_name = _sanitize_identifier(name)
    stats = MaterializeStats(
        input_files=len(input_files),
        input_rows=total_input_rows,
        output_rows=total_output_rows,
        output_files=output_files,
        symbols=len(symbols_seen),
        trade_dates=len(trade_date_keys),
        trade_date_min=date_min,
        trade_date_max=date_max,
        rows_missing_date_dropped=rows_missing_date_dropped,
        rows_missing_symbol_dropped=rows_missing_symbol_dropped,
        duplicate_rows_dropped=duplicate_rows_dropped,
    )
    manifest = _build_materialize_manifest(
        name=name,
        dataset=dataset,
        market=market,
        preset=preset,
        frequency=frequency,
        source_mode=source_mode,
        asset_dir=asset_dir,
        file_path=file_path,
        source_manifest=source_manifest,
        output_dir=output_dir,
        output_data_dir=output_data_dir,
        view_name=view_name,
        date_col=date_col,
        symbol_col=symbol_col,
        column_dtypes=column_dtypes,
        stats=stats,
    )
    manifest_path = output_dir / "manifest.yml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    print(
        f"Materialized standardized layer to {output_dir} "
        f"({stats.output_rows} rows, {stats.output_files} files, view={view_name})"
    )
    return 0


def _import_duckdb():
    try:
        return import_module("duckdb")
    except ImportError as exc:  # pragma: no cover - exercised via CLI/SystemExit
        raise SystemExit(
            "duckdb is not installed. Install with: uv sync --extra duckdb"
        ) from exc


def _standardized_manifests(root: Path) -> list[Path]:
    manifests: list[Path] = []
    for path in sorted(root.glob("**/manifest.yml")):
        if not path.is_file():
            continue
        payload = _load_yaml(path)
        if payload.get("layer") == "standardized":
            manifests.append(path)
    return manifests


def _refresh_duckdb_views(conn, *, standardized_root: Path) -> int:
    manifests = _standardized_manifests(standardized_root)
    conn.execute("CREATE SCHEMA IF NOT EXISTS standardized")
    registered = 0
    for manifest_path in manifests:
        payload = _load_yaml(manifest_path)
        output_glob = payload.get("output_glob")
        if not output_glob:
            continue
        view_name = _sanitize_identifier(str(payload.get("view_name") or payload.get("name") or manifest_path.parent.name))
        query = (
            f'CREATE OR REPLACE VIEW standardized."{view_name}" AS '
            f"SELECT * FROM read_parquet({_duckdb_sql_literal(str(output_glob))}, union_by_name = true)"
        )
        conn.execute(query)
        registered += 1
    return registered


def _read_sql(args) -> str:
    sql_text = str(getattr(args, "sql", "") or "").strip()
    sql_file = getattr(args, "sql_file", None)
    if sql_text and sql_file:
        raise SystemExit("Use either --sql or --sql-file, not both.")
    if sql_text:
        return sql_text
    if sql_file:
        return resolve_repo_path(sql_file).read_text(encoding="utf-8")
    raise SystemExit("Provide --sql or --sql-file.")


def query_standardized(args) -> int:
    duckdb = _import_duckdb()
    artifacts_root = resolve_artifacts_root(getattr(args, "artifacts_root", None))
    db_path = resolve_warehouse_db_path(
        getattr(args, "db_path", None),
        artifacts_root=artifacts_root,
    )
    standardized_root = resolve_repo_path(
        getattr(args, "standardized_root", None)
        or standardized_dir_for(artifacts_root)
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)

    sql_text = _read_sql(args)
    conn = duckdb.connect(str(db_path))
    try:
        registered = _refresh_duckdb_views(conn, standardized_root=standardized_root)
        result = conn.execute(sql_text).df()
    finally:
        conn.close()

    output_format = str(getattr(args, "format", "text") or "text").strip().lower()
    out_path = resolve_repo_path(args.out) if getattr(args, "out", None) else None
    if output_format == "json":
        rendered = json.dumps(
            result.to_dict(orient="records"),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
        else:
            print(rendered)
    elif output_format == "csv":
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(out_path, index=False)
        else:
            print(result.to_csv(index=False), end="")
    elif output_format == "parquet":
        if out_path is None:
            raise SystemExit("--out is required when --format parquet.")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_parquet(out_path, index=False)
    else:
        rendered = result.to_string(index=False)
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)

    if out_path:
        print(f"Wrote query result to {out_path} (registered {registered} standardized view(s))")
    return 0


def add_catalog_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        default=None,
        help=f"Artifacts root to scan. {ARTIFACTS_ROOT_HELP}",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional SQLite catalog output path. Default: <artifacts_root>/metadata/catalog.sqlite.",
    )
    parser.add_argument(
        "--summary-out",
        default=None,
        help="Optional CSV export of catalog rows. Default: <db_path parent>/catalog_summary.csv.",
    )


def add_materialize_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        default=None,
        help=f"Artifacts root used for default outputs. {ARTIFACTS_ROOT_HELP}",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Logical materialization name; also used for the DuckDB view name.",
    )
    parser.add_argument(
        "--market",
        default="hk",
        help="Market tag written into the standardized manifest. Default: hk.",
    )
    parser.add_argument(
        "--preset",
        default="generic",
        choices=sorted(PRESET_DEFAULTS),
        help="Column default preset. Default: generic.",
    )
    parser.add_argument(
        "--dataset-name",
        help="Logical dataset group under artifacts/standardized/<market>/. Default comes from --preset.",
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--asset-dir",
        help="Mirror asset directory that contains data/*.parquet.",
    )
    source_group.add_argument(
        "--file",
        help="Input flat file (.parquet or .csv).",
    )
    parser.add_argument(
        "--date-col",
        help="Input date column name. Default comes from --preset.",
    )
    parser.add_argument(
        "--symbol-col",
        help="Input symbol column name. Default comes from --preset.",
    )
    parser.add_argument(
        "--frequency",
        default="D",
        help="Output sampling frequency: D, M, or Q. Default: D.",
    )
    parser.add_argument(
        "--out-root",
        default=None,
        help="Standardized layer root. Default: <artifacts_root>/standardized.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing standardized output directory.",
    )


def add_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts-root",
        default=None,
        help=(
            "Artifacts root used for default metadata and standardized paths. "
            f"{ARTIFACTS_ROOT_HELP}"
        ),
    )
    parser.add_argument(
        "--sql",
        help="SQL statement executed against DuckDB after standardized views are refreshed.",
    )
    parser.add_argument(
        "--sql-file",
        help="Path to a .sql file executed against DuckDB.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="DuckDB database path. Default: <artifacts_root>/metadata/warehouse.duckdb.",
    )
    parser.add_argument(
        "--standardized-root",
        default=None,
        help="Standardized layer root scanned for manifest-backed views. Default: <artifacts_root>/standardized.",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "json", "csv", "parquet"],
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--out",
        help="Optional output file. Required for --format parquet.",
    )
