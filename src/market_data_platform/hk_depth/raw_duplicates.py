"""Safe duplicate resolution for raw symbol-date parquet parts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _non_null_schema_fields(path: Path) -> int:
    return sum(
        1 for field in pq.ParquetFile(path).schema_arrow if not pa.types.is_null(field.type)
    )


def resolve_safe_duplicate_parts(
    unit: tuple[str, str],
    parts: list[Path],
    *,
    operation: str,
) -> tuple[Path, str]:
    """Select one raw part only when duplicate contents have a safe interpretation."""
    ordered = sorted(parts)
    rows = {part: int(pq.ParquetFile(part).metadata.num_rows) for part in ordered}
    digests = {part: _sha256(part) for part in ordered}
    if len(set(digests.values())) == 1:
        return ordered[0], "byte_identical"
    nonempty = [part for part in ordered if rows[part] > 0]
    if not nonempty:
        selected = min(ordered, key=lambda path: (-_non_null_schema_fields(path), str(path)))
        return selected, "all_empty_schema_or_metadata_diff"
    if len({digests[part] for part in nonempty}) != 1:
        trade_date, order_book_id = unit
        raise ValueError(
            f"{operation} found conflicting non-empty duplicate symbol-date parts for "
            f"{trade_date}/{order_book_id}: {', '.join(str(part) for part in ordered)}"
        )
    return nonempty[0], "nonempty_replaces_empty"
