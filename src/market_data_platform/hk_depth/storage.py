"""Filesystem helpers for depth snapshot caches and asset outputs."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import pandas as pd

DEFAULT_PARQUET_ENGINE = "pyarrow"
DEFAULT_PARQUET_COMPRESSION = "zstd"
DEFAULT_PARQUET_COMPRESSION_LEVEL = 3
SUPPORTED_PARQUET_ENGINES = frozenset({"pyarrow"})
SUPPORTED_PARQUET_COMPRESSIONS = frozenset({"snappy", "gzip", "brotli", "lz4", "zstd", None})
PARQUET_COMPRESSION_LEVEL_CODECS = frozenset({"gzip", "brotli", "zstd"})


def now_stamp() -> str:
    """Return a stable UTC timestamp for filenames."""
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def batch_part_path(dataset_root: str | Path, trade_date: str, batch_number: int) -> Path:
    return (
        Path(dataset_root)
        / "parts"
        / f"trade_date={trade_date}"
        / f"batch_{batch_number:04d}.parquet"
    )


def encode_order_book_id(order_book_id: str) -> str:
    """Encode an order book id for a single path segment."""
    return quote(order_book_id, safe="-._~")


def decode_order_book_id(value: str) -> str:
    """Decode a path segment created by `encode_order_book_id`."""
    return unquote(value)


def symbol_date_part_path(
    dataset_root: str | Path,
    trade_date: str,
    order_book_id: str,
) -> Path:
    return (
        Path(dataset_root)
        / "parts"
        / f"trade_date={trade_date}"
        / f"order_book_id={encode_order_book_id(order_book_id)}.parquet"
    )


def parse_symbol_date_part_path(path: str | Path) -> tuple[str | None, str | None]:
    part = Path(path)
    if not part.name.startswith("order_book_id=") or part.suffix != ".parquet":
        return None, None
    if not part.parent.name.startswith("trade_date="):
        return None, None
    trade_date = part.parent.name.removeprefix("trade_date=")
    encoded = part.stem.removeprefix("order_book_id=")
    return trade_date, decode_order_book_id(encoded)


def infer_part_layout(path: str | Path) -> str:
    part = Path(path)
    if part.name.startswith("batch_") and part.suffix == ".parquet":
        return "legacy_batch.v1"
    trade_date, order_book_id = parse_symbol_date_part_path(part)
    if trade_date and order_book_id:
        return "symbol_date.v1"
    return "unknown"


def metadata_path(dataset_root: str | Path, kind: str, stamp: str | None = None) -> Path:
    return Path(dataset_root) / "meta" / f"{kind}_{stamp or now_stamp()}.json"


def normalize_parquet_compression(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"", "none", "null", "uncompressed"}:
        return None
    return normalized


def validate_parquet_write_options(
    *,
    engine: str = DEFAULT_PARQUET_ENGINE,
    compression: str | None = DEFAULT_PARQUET_COMPRESSION,
    compression_level: int | None = None,
) -> dict[str, Any]:
    normalized_engine = engine.strip().lower()
    normalized_compression = normalize_parquet_compression(compression)
    if normalized_engine not in SUPPORTED_PARQUET_ENGINES:
        supported = ", ".join(sorted(SUPPORTED_PARQUET_ENGINES))
        raise ValueError(f"Unsupported parquet engine {engine!r}; supported: {supported}.")
    if normalized_compression not in SUPPORTED_PARQUET_COMPRESSIONS:
        supported = ", ".join(sorted(c for c in SUPPORTED_PARQUET_COMPRESSIONS if c))
        raise ValueError(
            f"Unsupported parquet compression {compression!r}; supported: {supported}, none."
        )
    if compression_level is None and normalized_compression == DEFAULT_PARQUET_COMPRESSION:
        compression_level = DEFAULT_PARQUET_COMPRESSION_LEVEL
    if compression_level is not None:
        if normalized_compression not in PARQUET_COMPRESSION_LEVEL_CODECS:
            supported = ", ".join(sorted(PARQUET_COMPRESSION_LEVEL_CODECS))
            raise ValueError(
                "compression_level is only supported for parquet compression codecs: "
                f"{supported}."
            )
        if compression_level < 1:
            raise ValueError("compression_level must be a positive integer.")
    return {
        "engine": normalized_engine,
        "compression": normalized_compression,
        "compression_level": compression_level,
    }


def atomic_write_parquet(
    df: pd.DataFrame,
    path: str | Path,
    *,
    engine: str = DEFAULT_PARQUET_ENGINE,
    compression: str | None = DEFAULT_PARQUET_COMPRESSION,
    compression_level: int | None = None,
) -> Path:
    """Write parquet through a temp file and then replace the target."""
    options = validate_parquet_write_options(
        engine=engine,
        compression=compression,
        compression_level=compression_level,
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        kwargs: dict[str, Any] = {
            "index": False,
            "engine": options["engine"],
            "compression": options["compression"],
        }
        if options["compression_level"] is not None:
            kwargs["compression_level"] = options["compression_level"]
        df.to_parquet(temp, **kwargs)
        temp.replace(target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return repr(value)


def write_json(path: str | Path, data: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def write_yaml(path: str | Path, data: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml

        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    except Exception:
        text = json.dumps(data, indent=2, sort_keys=False, default=_json_default)
    target.write_text(text, encoding="utf-8")
    return target


def discover_parquet_parts(path: str | Path) -> list[Path]:
    """Find parquet files under a raw snapshot cache, deliverable, directory, or file."""
    root = Path(path)
    if root.is_file() and root.suffix == ".parquet":
        return [root]
    if not root.exists():
        return []
    search_root = root / "parts" if (root / "parts").exists() else root
    return sorted(
        part
        for part in search_root.rglob("*.parquet")
        if ".tmp" not in part.name and not part.name.startswith(".")
    )


def load_parquet_parts(path: str | Path) -> pd.DataFrame:
    parts = discover_parquet_parts(path)
    if not parts:
        return pd.DataFrame()
    return pd.concat((pd.read_parquet(part) for part in parts), ignore_index=True)


def copy_parquet_tree(source_root: str | Path, output_data_root: str | Path) -> list[Path]:
    """Copy parquet parts while preserving partition-like relative paths."""
    source = Path(source_root)
    output = Path(output_data_root)
    copied: list[Path] = []
    parts_base = source / "parts"
    for part in discover_parquet_parts(source):
        try:
            rel = part.relative_to(parts_base)
        except ValueError:
            rel = part.name if part.is_file() else part.relative_to(source)
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(part, target)
        copied.append(target)
    return copied
