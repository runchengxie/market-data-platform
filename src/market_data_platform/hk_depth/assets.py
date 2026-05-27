"""Deliverable data directory output helpers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from market_data_platform.hk_depth import __version__
from market_data_platform.hk_depth.coverage import coverage_summary, scan_raw_coverage
from market_data_platform.hk_depth.storage import (
    copy_parquet_tree,
    discover_parquet_parts,
    load_parquet_parts,
    write_json,
    write_yaml,
)


def _date_range(df: pd.DataFrame) -> tuple[str | None, str | None]:
    if "trading_date" not in df.columns or df.empty:
        return None, None
    values = df["trading_date"].dropna().astype(str)
    if values.empty:
        return None, None
    return str(values.min()), str(values.max())


def _date_range_from_values(values: list[str]) -> tuple[str | None, str | None]:
    clean = [str(value) for value in values if value]
    if not clean:
        return None, None
    return min(clean), max(clean)


def _ordered_union(rows: list[dict[str, Any]], key: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for row in rows:
        for value in row.get(key) or []:
            text = str(value)
            if text not in seen:
                seen.add(text)
                values.append(text)
    return values


def _manifest_base(
    *,
    schema_version: str,
    provider: str,
    market: str,
    frequency: str,
    source_path: str | Path,
    row_count: int,
    symbol_count: int,
    date_range: tuple[str | None, str | None],
    fields: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "provider": provider,
        "market": market,
        "frequency": frequency,
        "source_path": str(source_path),
        "row_count": row_count,
        "symbol_count": symbol_count,
        "date_range": {"start": date_range[0], "end": date_range[1]},
        "fields": fields,
        "generator": {"package": "market-data-platform.hk-depth", "version": __version__},
    }


def emit_raw_asset(source_root: str | Path, output_root: str | Path) -> dict[str, Any]:
    source = Path(source_root)
    output = Path(output_root)
    data_root = output / "data"
    copied = copy_parquet_tree(source, data_root)
    identity_columns = {"order_book_id", "datetime", "trading_date"}
    coverage_rows = scan_raw_coverage(source)
    coverage = coverage_summary(coverage_rows)
    non_empty_rows = [row for row in coverage_rows if int(row.get("row_count") or 0) > 0]
    fields = [
        column
        for column in _ordered_union(coverage_rows, "fields")
        if column not in identity_columns
    ]
    symbols = sorted(
        {
            str(row["order_book_id"])
            for row in non_empty_rows
            if row.get("order_book_id") is not None
        }
    )
    dates = [
        str(row["trading_date"])
        for row in non_empty_rows
        if row.get("trading_date") is not None
    ]
    manifest = _manifest_base(
        schema_version="tick_depth_raw.v1",
        provider="rqdata",
        market="hk",
        frequency="tick",
        source_path=source,
        row_count=sum(int(row.get("row_count") or 0) for row in coverage_rows),
        symbol_count=len(symbols),
        date_range=_date_range_from_values(dates),
        fields=fields,
    )
    manifest["files"] = [str(path.relative_to(output)) for path in copied]
    manifest["layout_version"] = (
        coverage["layout_versions"][0]
        if len(coverage["layout_versions"]) == 1
        else "mixed" if coverage["layout_versions"] else None
    )
    manifest["compression"] = (
        coverage["compressions"][0]
        if len(coverage["compressions"]) == 1
        else "mixed" if coverage["compressions"] else None
    )
    manifest["storage"] = {
        "layout_versions": coverage["layout_versions"],
        "compressions": coverage["compressions"],
    }
    manifest["coverage"] = coverage
    write_yaml(output / "manifest.yml", manifest)
    (output / "symbols.txt").write_text(
        "\n".join(symbols) + ("\n" if symbols else ""),
        encoding="utf-8",
    )
    (output / "fields.txt").write_text(
        "\n".join(fields) + ("\n" if fields else ""),
        encoding="utf-8",
    )
    write_json(output / "meta.json", manifest)
    return {"output_root": str(output), "manifest_path": str(output / "manifest.yml"), **manifest}


def emit_daily_asset(source_path: str | Path, output_root: str | Path) -> dict[str, Any]:
    source = Path(source_path)
    output = Path(output_root)
    data_root = output / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    if source.is_file():
        target = data_root / "data.parquet"
        shutil.copy2(source, target)
        files = [target]
        df = pd.read_parquet(source)
    else:
        files = copy_parquet_tree(source, data_root)
        df = load_parquet_parts(source)

    symbols = (
        sorted(df["order_book_id"].dropna().astype(str).unique()) if "order_book_id" in df else []
    )
    fields = list(df.columns)
    manifest = _manifest_base(
        schema_version="tick_depth_daily.v1",
        provider="rqdata",
        market="hk",
        frequency="daily",
        source_path=source,
        row_count=int(len(df)),
        symbol_count=len(symbols),
        date_range=_date_range(df),
        fields=fields,
    )
    manifest["files"] = [
        str(path.relative_to(output)) for path in discover_parquet_parts(data_root)
    ]
    if not manifest["files"]:
        manifest["files"] = [str(path.relative_to(output)) for path in files]
    write_yaml(output / "manifest.yml", manifest)
    (output / "symbols.txt").write_text(
        "\n".join(symbols) + ("\n" if symbols else ""),
        encoding="utf-8",
    )
    (output / "fields.txt").write_text(
        "\n".join(fields) + ("\n" if fields else ""),
        encoding="utf-8",
    )
    write_json(output / "meta.json", manifest)
    return {"output_root": str(output), "manifest_path": str(output / "manifest.yml"), **manifest}
