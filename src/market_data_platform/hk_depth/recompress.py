"""Raw parquet recompression helpers."""

from __future__ import annotations

import csv
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from market_data_platform.hk_depth.progress import ProgressBar
from market_data_platform.hk_depth.storage import (
    DEFAULT_PARQUET_COMPRESSION,
    DEFAULT_PARQUET_COMPRESSION_LEVEL,
    discover_parquet_parts,
    metadata_path,
    now_stamp,
    validate_parquet_write_options,
    write_json,
)


def _compression(path: Path) -> str | None:
    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata
    if metadata is None or metadata.num_row_groups == 0:
        return None
    row_group = metadata.row_group(0)
    if row_group.num_columns == 0:
        return None
    value = row_group.column(0).compression
    return str(value).lower() if value is not None else None


def _relative_part_path(source_root: Path, part: Path) -> Path:
    if source_root.is_file():
        return Path(part.name)
    try:
        return part.relative_to(source_root)
    except ValueError:
        return Path(part.name)


def _parquet_identity(path: Path) -> dict[str, Any]:
    parquet_file = pq.ParquetFile(path)
    metadata = parquet_file.metadata
    return {
        "schema": parquet_file.schema_arrow,
        "rows": int(metadata.num_rows if metadata else 0),
        "compression": _compression(path),
    }


def _target_matches(
    *,
    source: Path,
    target: Path,
    expected_compression: str | None,
    should_rewrite: bool,
) -> bool:
    if not target.exists():
        return False
    source_identity = _parquet_identity(source)
    target_identity = _parquet_identity(target)
    if source_identity["rows"] != target_identity["rows"]:
        return False
    if source_identity["schema"] != target_identity["schema"]:
        return False
    if should_rewrite and target_identity["compression"] != expected_compression:
        return False
    return True


def _rewrite_parquet(
    source: Path,
    target: Path,
    *,
    compression: str | None,
    compression_level: int | None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    parquet_file = pq.ParquetFile(source)
    writer: pq.ParquetWriter | None = None
    try:
        if parquet_file.num_row_groups == 0:
            kwargs: dict[str, Any] = {"compression": compression}
            if compression_level is not None:
                kwargs["compression_level"] = compression_level
            pq.write_table(
                pa.Table.from_batches([], schema=parquet_file.schema_arrow),
                temp,
                **kwargs,
            )
        else:
            for row_group_index in range(parquet_file.num_row_groups):
                table = parquet_file.read_row_group(row_group_index)
                if writer is None:
                    kwargs = {"compression": compression}
                    if compression_level is not None:
                        kwargs["compression_level"] = compression_level
                    writer = pq.ParquetWriter(temp, table.schema, **kwargs)
                writer.write_table(table)
        if writer is not None:
            writer.close()
            writer = None
        temp.replace(target)
    finally:
        if writer is not None:
            writer.close()
        if temp.exists():
            temp.unlink()


def _copy_parquet(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _write_units(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "source_path",
        "output_path",
        "relative_path",
        "action",
        "row_count",
        "source_bytes",
        "output_bytes",
        "source_compression",
        "output_compression",
        "elapsed_seconds",
        "error",
    ]
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
    return target


def recompress_raw_cache(
    input_root: str | Path,
    output_root: str | Path,
    *,
    parquet_compression: str | None = DEFAULT_PARQUET_COMPRESSION,
    parquet_compression_level: int | None = DEFAULT_PARQUET_COMPRESSION_LEVEL,
    min_rewrite_bytes: int = 0,
    resume: bool = True,
    continue_on_error: bool = False,
    meta_output: str | Path | None = None,
    units_output: str | Path | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Rewrite raw parquet parts to a new cache using the requested compression."""
    source = Path(input_root)
    output = Path(output_root)
    if source.resolve() == output.resolve():
        raise ValueError("input_root and output_root must be different for raw recompression.")
    if min_rewrite_bytes < 0:
        raise ValueError("min_rewrite_bytes must be non-negative.")

    writer_options = validate_parquet_write_options(
        compression=parquet_compression,
        compression_level=parquet_compression_level,
    )
    parts = discover_parquet_parts(source)
    total_source_bytes = sum(part.stat().st_size for part in parts)
    stamp = now_stamp()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    source_bytes = 0
    output_bytes = 0
    source_rows = 0
    rewritten_parts = 0
    copied_parts = 0
    skipped_existing_parts = 0
    started = time.perf_counter()
    progress_bar = ProgressBar(
        label="recompress-raw",
        total_units=len(parts),
        total_bytes=total_source_bytes,
        enabled=progress,
    )

    for part in parts:
        relative = _relative_part_path(source, part)
        target = output / relative
        source_size = part.stat().st_size
        source_bytes += source_size
        should_rewrite = source_size >= min_rewrite_bytes
        unit_started = time.perf_counter()
        row: dict[str, Any] = {
            "source_path": str(part),
            "output_path": str(target),
            "relative_path": str(relative),
            "action": "pending",
            "source_bytes": source_size,
            "output_bytes": 0,
            "source_compression": None,
            "output_compression": None,
            "row_count": 0,
            "elapsed_seconds": 0.0,
            "error": None,
        }
        try:
            source_identity = _parquet_identity(part)
            row["source_compression"] = source_identity["compression"]
            row["row_count"] = int(source_identity["rows"])
            source_rows += int(source_identity["rows"])
            if resume and _target_matches(
                source=part,
                target=target,
                expected_compression=writer_options["compression"],
                should_rewrite=should_rewrite,
            ):
                row["action"] = "skipped_existing"
                skipped_existing_parts += 1
            elif should_rewrite:
                _rewrite_parquet(
                    part,
                    target,
                    compression=writer_options["compression"],
                    compression_level=writer_options["compression_level"],
                )
                row["action"] = "rewritten"
                rewritten_parts += 1
            else:
                _copy_parquet(part, target)
                row["action"] = "copied"
                copied_parts += 1
            row["output_bytes"] = target.stat().st_size
            row["output_compression"] = _compression(target)
            output_bytes += int(row["output_bytes"])
        except Exception as exc:
            row["action"] = "failed"
            row["error"] = str(exc)
            failures.append(
                {
                    "source_path": str(part),
                    "relative_path": str(relative),
                    "error": str(exc),
                }
            )
            if not continue_on_error:
                row["elapsed_seconds"] = round(time.perf_counter() - unit_started, 6)
                rows.append(row)
                progress_bar.update(
                    bytes_done=source_size,
                    suffix=row["action"],
                    force=True,
                )
                break
        row["elapsed_seconds"] = round(time.perf_counter() - unit_started, 6)
        rows.append(row)
        progress_bar.update(bytes_done=source_size, suffix=row["action"])

    elapsed = time.perf_counter() - started
    status = "pass" if not failures and len(parts) == len(rows) else "fail"
    progress_bar.close(suffix=status)
    audit_path = (
        Path(units_output)
        if units_output
        else output / "audit" / f"recompress_raw_{stamp}.csv"
    )
    audit_path = _write_units(audit_path, rows)
    report = {
        "kind": "recompress_raw",
        "status": status,
        "source_path": str(source),
        "output_path": str(output),
        "source_parts": len(parts),
        "processed_parts": len(rows),
        "rewritten_parts": rewritten_parts,
        "copied_parts": copied_parts,
        "skipped_existing_parts": skipped_existing_parts,
        "failed_parts": len(failures),
        "source_rows": source_rows,
        "source_bytes": source_bytes,
        "output_bytes": output_bytes,
        "bytes_saved": source_bytes - output_bytes if output_bytes else 0,
        "compression_ratio": float(output_bytes / source_bytes) if source_bytes else None,
        "parquet": writer_options,
        "min_rewrite_bytes": min_rewrite_bytes,
        "resume": resume,
        "continue_on_error": continue_on_error,
        "elapsed_seconds": round(elapsed, 6),
        "failures": failures,
        "unit_diagnostics_path": str(audit_path),
    }
    meta_path = (
        Path(meta_output) if meta_output else metadata_path(output, "recompress_raw", stamp)
    )
    write_json(meta_path, report)
    report["metadata_path"] = str(meta_path)
    return report
