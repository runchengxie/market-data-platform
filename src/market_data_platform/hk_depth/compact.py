"""Cold-storage compaction for symbol-date raw parquet parts."""

from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from market_data_platform.hk_depth.progress import ProgressBar
from market_data_platform.hk_depth.raw_duplicates import resolve_safe_duplicate_parts
from market_data_platform.hk_depth.storage import (
    DEFAULT_PARQUET_COMPRESSION,
    DEFAULT_PARQUET_COMPRESSION_LEVEL,
    discover_parquet_parts,
    encode_order_book_id,
    metadata_path,
    now_stamp,
    parse_symbol_date_part_path,
    validate_parquet_write_options,
    write_json,
)

COMPACT_GROUPINGS = ("symbol-quarter", "symbol-year")
DUPLICATE_POLICIES = ("error", "prefer-nonempty-identical")


@dataclass(frozen=True)
class CompactGroup:
    """Source symbol-date parts contributing to one compact parquet file."""

    order_book_id: str
    period: str
    target: Path
    source_parts: tuple[Path, ...]


@dataclass(frozen=True)
class SourceStats:
    """Compact source-group statistics required for writing and resume."""

    schema: pa.Schema
    source_rows: int
    source_bytes: int
    nonempty_parts: int
    schema_variants: int


def _compact_target(
    output: Path,
    *,
    grouping: str,
    order_book_id: str,
    trade_date: str,
) -> tuple[str, Path]:
    year = trade_date[:4]
    base = output / "parts" / f"order_book_id={encode_order_book_id(order_book_id)}"
    if grouping == "symbol-year":
        return year, base / f"year={year}.parquet"
    quarter = ((int(trade_date[4:6]) - 1) // 3) + 1
    return f"{year}-Q{quarter}", base / f"year={year}" / f"quarter=Q{quarter}.parquet"


def _build_groups(
    source: Path,
    output: Path,
    grouping: str,
    duplicate_policy: str,
) -> tuple[list[CompactGroup], dict[str, Any]]:
    grouped: dict[tuple[str, str, Path], list[tuple[str, Path]]] = {}
    seen_units: dict[tuple[str, str], Path] = {}
    duplicate_parts: dict[tuple[str, str], list[Path]] = {}
    candidate_source_parts = 0
    candidate_source_bytes = 0
    for part in discover_parquet_parts(source):
        trade_date, order_book_id = parse_symbol_date_part_path(part)
        if trade_date is None or order_book_id is None:
            raise ValueError(
                "compact-raw requires symbol-date input parts; unrecognized part: "
                f"{part}"
            )
        candidate_source_parts += 1
        candidate_source_bytes += part.stat().st_size
        unit = (trade_date, order_book_id)
        if unit in seen_units:
            if duplicate_policy == "error":
                raise ValueError(
                    "compact-raw input contains duplicate symbol-date parts: "
                    f"{seen_units[unit]} and {part}"
                )
            duplicate_parts.setdefault(unit, [seen_units[unit]]).append(part)
            continue
        seen_units[unit] = part
    resolutions: dict[str, int] = {
        "byte_identical": 0,
        "all_empty_schema_or_metadata_diff": 0,
        "nonempty_replaces_empty": 0,
    }
    selection_samples: list[dict[str, Any]] = []
    for unit, candidates in sorted(duplicate_parts.items()):
        selected, resolution = resolve_safe_duplicate_parts(
            unit,
            candidates,
            operation="compact-raw",
        )
        seen_units[unit] = selected
        resolutions[resolution] += 1
        if len(selection_samples) < 10:
            selection_samples.append(
                {
                    "trade_date": unit[0],
                    "order_book_id": unit[1],
                    "resolution": resolution,
                    "selected": str(selected),
                    "dropped": [
                        str(part) for part in candidates if part != selected
                    ],
                }
            )
    for (trade_date, order_book_id), part in seen_units.items():
        period, target = _compact_target(
            output,
            grouping=grouping,
            order_book_id=order_book_id,
            trade_date=trade_date,
        )
        grouped.setdefault((order_book_id, period, target), []).append((trade_date, part))
    groups = [
        CompactGroup(
            order_book_id=key[0],
            period=key[1],
            target=key[2],
            source_parts=tuple(part for _, part in sorted(parts)),
        )
        for key, parts in sorted(grouped.items())
    ]
    selected_bytes = sum(part.stat().st_size for part in seen_units.values())
    duplicate_summary = {
        "policy": duplicate_policy,
        "candidate_source_parts": candidate_source_parts,
        "selected_symbol_date_parts": len(seen_units),
        "duplicate_symbol_date_units": len(duplicate_parts),
        "dropped_duplicate_parts": candidate_source_parts - len(seen_units),
        "candidate_source_bytes": candidate_source_bytes,
        "selected_source_bytes": selected_bytes,
        "dropped_duplicate_bytes": candidate_source_bytes - selected_bytes,
        "resolutions": resolutions,
        "selection_samples": selection_samples,
    }
    return groups, duplicate_summary


def _source_stats(group: CompactGroup) -> SourceStats:
    schemas: list[pa.Schema] = []
    rows = 0
    bytes_ = 0
    nonempty_parts = 0
    for part in group.source_parts:
        parquet_file = pq.ParquetFile(part)
        schema = parquet_file.schema_arrow
        if not any(schema.equals(existing, check_metadata=True) for existing in schemas):
            schemas.append(schema)
        part_rows = int(parquet_file.metadata.num_rows if parquet_file.metadata else 0)
        rows += part_rows
        bytes_ += part.stat().st_size
        if part_rows:
            nonempty_parts += 1
    if not schemas:
        raise ValueError(f"Compact group has no input parts: {group.target}")
    schema = pa.unify_schemas(schemas, promote_options="permissive")
    return SourceStats(
        schema=schema,
        source_rows=rows,
        source_bytes=bytes_,
        nonempty_parts=nonempty_parts,
        schema_variants=len(schemas),
    )


def _compression(path: Path) -> str | None:
    metadata = pq.ParquetFile(path).metadata
    if metadata is None or metadata.num_row_groups == 0:
        return None
    value = metadata.row_group(0).column(0).compression
    return str(value).lower() if value is not None else None


def _target_matches(
    target: Path,
    *,
    stats: SourceStats,
    expected_compression: str | None,
    expected_row_groups: int,
) -> bool:
    if not target.exists():
        return False
    parquet_file = pq.ParquetFile(target)
    metadata = parquet_file.metadata
    if parquet_file.schema_arrow != stats.schema or metadata is None:
        return False
    if int(metadata.num_rows) != stats.source_rows:
        return False
    if int(metadata.num_row_groups) != expected_row_groups:
        return False
    return expected_row_groups == 0 or _compression(target) == expected_compression


def _flush_tables(writer: pq.ParquetWriter, tables: list[pa.Table]) -> None:
    if not tables:
        return
    table = tables[0] if len(tables) == 1 else pa.concat_tables(tables)
    writer.write_table(table, row_group_size=max(1, table.num_rows))


def _align_table(table: pa.Table, schema: pa.Schema) -> pa.Table:
    arrays: list[pa.Array | pa.ChunkedArray] = []
    available = set(table.column_names)
    for field in schema:
        if field.name in available:
            column = table[field.name]
            arrays.append(column if column.type == field.type else column.cast(field.type))
        else:
            arrays.append(pa.nulls(table.num_rows, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def _write_group(
    group: CompactGroup,
    *,
    stats: SourceStats,
    compression: str | None,
    compression_level: int | None,
    row_group_days: int,
) -> None:
    group.target.parent.mkdir(parents=True, exist_ok=True)
    temp = group.target.with_name(f".{group.target.name}.{os.getpid()}.tmp")
    kwargs: dict[str, Any] = {"compression": compression}
    if compression_level is not None:
        kwargs["compression_level"] = compression_level
    writer: pq.ParquetWriter | None = None
    pending: list[pa.Table] = []
    try:
        writer = pq.ParquetWriter(temp, stats.schema, **kwargs)
        for part in group.source_parts:
            table = _align_table(pq.ParquetFile(part).read(), stats.schema)
            if table.num_rows == 0:
                continue
            pending.append(table)
            if len(pending) == row_group_days:
                _flush_tables(writer, pending)
                pending = []
        _flush_tables(writer, pending)
        writer.close()
        writer = None
        temp.replace(group.target)
    finally:
        if writer is not None:
            writer.close()
        if temp.exists():
            temp.unlink()


def _write_units(path: Path, rows: list[dict[str, Any]]) -> Path:
    columns = [
        "order_book_id",
        "period",
        "output_path",
        "action",
        "source_parts",
        "source_rows",
        "source_bytes",
        "output_bytes",
        "row_groups",
        "schema_variants",
        "elapsed_seconds",
        "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
    return path


def _process_group(
    group: CompactGroup,
    *,
    writer_options: dict[str, Any],
    row_group_days: int,
    resume: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    stats = _source_stats(group)
    expected_row_groups = (stats.nonempty_parts + row_group_days - 1) // row_group_days
    action = "compacted"
    if resume and _target_matches(
        group.target,
        stats=stats,
        expected_compression=writer_options["compression"],
        expected_row_groups=expected_row_groups,
    ):
        action = "skipped_existing"
    else:
        _write_group(
            group,
            stats=stats,
            compression=writer_options["compression"],
            compression_level=writer_options["compression_level"],
            row_group_days=row_group_days,
        )
        if not _target_matches(
            group.target,
            stats=stats,
            expected_compression=writer_options["compression"],
            expected_row_groups=expected_row_groups,
        ):
            raise RuntimeError(f"Compact output validation failed: {group.target}")
    output_metadata = pq.ParquetFile(group.target).metadata
    return {
        "order_book_id": group.order_book_id,
        "period": group.period,
        "output_path": str(group.target),
        "action": action,
        "source_parts": len(group.source_parts),
        "source_rows": stats.source_rows,
        "source_bytes": stats.source_bytes,
        "output_bytes": group.target.stat().st_size,
        "row_groups": int(output_metadata.num_row_groups if output_metadata else 0),
        "schema_variants": stats.schema_variants,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "error": None,
    }


def compact_raw_cache(
    input_root: str | Path,
    output_root: str | Path,
    *,
    grouping: str = "symbol-quarter",
    parquet_compression: str | None = DEFAULT_PARQUET_COMPRESSION,
    parquet_compression_level: int | None = DEFAULT_PARQUET_COMPRESSION_LEVEL,
    row_group_days: int = 1,
    duplicate_policy: str = "error",
    resume: bool = True,
    continue_on_error: bool = False,
    meta_output: str | Path | None = None,
    units_output: str | Path | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Compact symbol-date raw snapshot parts into cold-storage parquet parts."""
    source = Path(input_root)
    output = Path(output_root)
    if source.resolve() == output.resolve():
        raise ValueError("input_root and output_root must be different for raw compaction.")
    if grouping not in COMPACT_GROUPINGS:
        raise ValueError(f"Unsupported compact grouping: {grouping}")
    if duplicate_policy not in DUPLICATE_POLICIES:
        raise ValueError(f"Unsupported compact duplicate policy: {duplicate_policy}")
    if row_group_days < 1:
        raise ValueError("row_group_days must be a positive integer.")
    writer_options = validate_parquet_write_options(
        compression=parquet_compression,
        compression_level=parquet_compression_level,
    )
    groups, duplicate_summary = _build_groups(
        source,
        output,
        grouping,
        duplicate_policy,
    )
    source_parts = sum(len(group.source_parts) for group in groups)
    source_bytes = sum(part.stat().st_size for group in groups for part in group.source_parts)
    stamp = now_stamp()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    started = time.perf_counter()
    progress_bar = ProgressBar(
        label="compact-raw",
        total_units=source_parts,
        total_bytes=source_bytes,
        enabled=progress,
    )
    for group in groups:
        try:
            row = _process_group(
                group,
                writer_options=writer_options,
                row_group_days=row_group_days,
                resume=resume,
            )
        except Exception as exc:
            row = {
                "order_book_id": group.order_book_id,
                "period": group.period,
                "output_path": str(group.target),
                "action": "failed",
                "source_parts": len(group.source_parts),
                "source_rows": 0,
                "source_bytes": sum(part.stat().st_size for part in group.source_parts),
                "output_bytes": 0,
                "row_groups": 0,
                "schema_variants": 0,
                "elapsed_seconds": 0.0,
                "error": str(exc),
            }
            failures.append({"output_path": str(group.target), "error": str(exc)})
        rows.append(row)
        progress_bar.update(
            units=len(group.source_parts),
            bytes_done=int(row["source_bytes"]),
            suffix=f"{group.order_book_id} {group.period} {row['action']}",
        )
        if failures and not continue_on_error:
            break
    status = "pass" if not failures and len(rows) == len(groups) else "fail"
    progress_bar.close(suffix=status)
    audit_path = (
        Path(units_output)
        if units_output
        else output / "audit" / f"compact_raw_{stamp}.csv"
    )
    _write_units(audit_path, rows)
    output_bytes = sum(int(row["output_bytes"]) for row in rows if row["action"] != "failed")
    report: dict[str, Any] = {
        "kind": "compact_raw",
        "status": status,
        "source_path": str(source),
        "output_path": str(output),
        "layout_version": f"compact_{grouping.replace('-', '_')}.v1",
        "operational_raw_layout": False,
        "grouping": grouping,
        "row_group_days": row_group_days,
        "duplicate_resolution": duplicate_summary,
        "source_parts": source_parts,
        "processed_parts": sum(int(row["source_parts"]) for row in rows),
        "compact_parts": len(groups),
        "processed_compact_parts": len(rows),
        "failed_compact_parts": len(failures),
        "source_rows": sum(int(row["source_rows"]) for row in rows),
        "source_bytes": sum(int(row["source_bytes"]) for row in rows),
        "output_bytes": output_bytes,
        "bytes_saved": sum(int(row["source_bytes"]) for row in rows) - output_bytes,
        "compression_ratio": (
            float(output_bytes / sum(int(row["source_bytes"]) for row in rows))
            if rows and sum(int(row["source_bytes"]) for row in rows)
            else None
        ),
        "parquet": writer_options,
        "schema_variant_compact_parts": sum(
            1 for row in rows if int(row["schema_variants"]) > 1
        ),
        "schema_unification": "permissive within each compact part",
        "resume": resume,
        "continue_on_error": continue_on_error,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "failures": failures,
        "unit_diagnostics_path": str(audit_path),
    }
    meta_path = Path(meta_output) if meta_output else metadata_path(output, "compact_raw", stamp)
    write_json(meta_path, report)
    report["metadata_path"] = str(meta_path)
    return report
