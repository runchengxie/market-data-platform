from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol
from .asset_io import (
    _audit_record,
    _chunked,
    _ensure_requested_fields,
    _field_coverage_template,
    _load_existing_entry,
    _prepare_asset_frame,
    _update_field_coverage,
    _write_audit_csv,
    _write_symbol_frame,
)
from .fetch_runtime import (
    _ensure_rqdatac_hk_plugin as _ensure_rqdatac_hk_plugin_runtime,
    _retry_fetch,
)
from .manifest_ops import _build_manifest
from .mirror_workflow import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MIRROR_BACKOFF_SECONDS,
    DEFAULT_MIRROR_MAX_ATTEMPTS,
    DEFAULT_MIRROR_MAX_BACKOFF_SECONDS,
    DEFAULT_OUT_ROOT,
    _mirror_dataset,
)
from .models import MirrorFetchError, MirrorQuotaError
from .package_api import _package_attr
from .request_groups import (
    _default_hk_instruments_out_path,
    _resolve_instrument_symbol_filter,
    _resolve_symbols,
)
from .shared import (
    _dedupe_preserve_order,
    _git_metadata,
    _load_existing_text_list,
    _load_hk_financial_fields as _load_hk_financial_fields_shared,
    _load_manifest,
    _load_text_list,
    _normalize_absolute_date,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _path_mtime_iso,
    _prepare_output_dir,
    _resolve_fields_with_overrides,
    _resolve_path,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)


def _ensure_rqdatac_hk_plugin() -> None:
    ensure_plugin = _package_attr(
        "_ensure_rqdatac_hk_plugin",
        default=_ensure_rqdatac_hk_plugin_runtime,
    )
    ensure_plugin()


def _is_open_ended_hk_delisted_date(value: object) -> bool:
    text = str(value or "").strip()
    return text == "" or text.lower() in {"none", "nan", "nat"} or text == "0000-00-00"


def _is_active_hk_instrument(row: pd.Series) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status == "active":
        return True
    if status in {"delisted", "de_listed", "inactive"}:
        return False
    return _is_open_ended_hk_delisted_date(row.get("de_listed_date"))


def _hk_date_sort_key(value: object) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.Timestamp.min
    return parsed


def _disambiguate_hk_instrument_unique_ids(instruments: pd.DataFrame) -> pd.DataFrame:
    if "unique_id" not in instruments.columns:
        return instruments
    duplicate_symbols = instruments["symbol"].duplicated(keep=False)
    if not duplicate_symbols.any():
        return instruments

    normalized = instruments.copy()
    for _symbol, group in normalized[duplicate_symbols].groupby("symbol", sort=False):
        if len(group) <= 1:
            continue

        active_indices = [
            index for index, row in group.iterrows() if _is_active_hk_instrument(row)
        ]
        plain_index_candidates = active_indices or list(group.index)
        plain_index = max(
            plain_index_candidates,
            key=lambda index: _hk_date_sort_key(normalized.at[index, "listed_date"])
            if "listed_date" in normalized.columns
            else pd.Timestamp.min,
        )

        for index, row in group.iterrows():
            if index == plain_index:
                continue
            unique_id = str(row.get("unique_id") or "").strip()
            unique_symbol = _normalize_hk_symbol(unique_id)
            if not unique_symbol or unique_symbol == row["symbol"]:
                continue
            normalized.at[index, "symbol"] = unique_symbol
            normalized.at[index, "order_book_id"] = unique_id

    return normalized


def _load_hk_financial_fields() -> list[str]:
    loader = _package_attr(
        "_load_hk_financial_fields",
        default=_load_hk_financial_fields_shared,
    )
    return loader()


def list_hk_financial_fields(args) -> int:
    fields = _load_hk_financial_fields()
    contains = [
        str(item).strip().lower()
        for item in (getattr(args, "contains", None) or [])
        if str(item).strip()
    ]
    if contains:
        fields = [
            field for field in fields if all(token in field.lower() for token in contains)
        ]
    limit = getattr(args, "limit", None)
    if limit is not None:
        if limit <= 0:
            raise SystemExit("--limit must be > 0.")
        fields = fields[:limit]

    output = "\n".join(fields)
    if output:
        output += "\n"
    out = getattr(args, "out", None)
    if out:
        out_path = _resolve_path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"Wrote {len(fields)} HK financial fields to {out_path}")
    else:
        print(output, end="")
    return 0


def export_hk_instruments(args, rqdatac) -> int:
    _ensure_rqdatac_hk_plugin()
    symbol_filter, symbol_metadata = _resolve_instrument_symbol_filter(args)
    instrument_type = str(getattr(args, "instrument_type", "CS") or "CS").strip().upper()
    if not instrument_type:
        raise SystemExit("--instrument-type must not be empty.")
    try:
        frame = rqdatac.all_instruments(instrument_type, market="hk")
    except TypeError:
        frame = rqdatac.all_instruments(instrument_type)
    if frame is None or frame.empty:
        raise SystemExit(
            f"rqdatac.all_instruments returned no HK instruments for instrument_type={instrument_type!r}."
        )

    instruments = _normalize_frame_columns(frame.copy())
    if "order_book_id" not in instruments.columns:
        raise SystemExit("HK instruments payload is missing order_book_id.")
    if "symbol" in instruments.columns and "name" not in instruments.columns:
        instruments = instruments.rename(columns={"symbol": "name"})
    elif "symbol" in instruments.columns:
        instruments = instruments.rename(columns={"symbol": "instrument_symbol"})
    instruments["order_book_id"] = instruments["order_book_id"].astype(str).str.strip()
    instruments["symbol"] = instruments["order_book_id"].map(_normalize_hk_symbol)
    instruments = _disambiguate_hk_instrument_unique_ids(instruments)
    instruments = instruments[instruments["symbol"] != ""].copy()

    if symbol_filter is not None:
        instruments = instruments[instruments["symbol"].isin(symbol_filter)].copy()
    elif getattr(args, "limit", None) is not None:
        instruments = (
            instruments.sort_values(["symbol", "order_book_id"], kind="mergesort")
            .head(args.limit)
            .copy()
        )

    if instruments.empty:
        raise SystemExit("No HK instruments matched the requested filter.")

    preferred_columns = [
        column
        for column in (
            "symbol",
            "order_book_id",
            "name",
            "instrument_symbol",
            "listed_date",
            "de_listed_date",
            "round_lot",
            "board_type",
            "status",
        )
        if column in instruments.columns
    ]
    remaining_columns = [
        column for column in instruments.columns if column not in preferred_columns
    ]
    instruments = instruments[preferred_columns + remaining_columns].copy()
    instruments.sort_values(["symbol", "order_book_id"], kind="mergesort", inplace=True)
    instruments.reset_index(drop=True, inplace=True)

    out_arg = getattr(args, "out", None)
    out_path = _resolve_path(out_arg) if out_arg else _default_hk_instruments_out_path()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".parquet")
    if out_path.exists() and not getattr(args, "force", False):
        raise SystemExit(f"Refusing to overwrite existing output: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.suffix.lower() == ".csv":
        instruments.to_csv(out_path, index=False)
    else:
        instruments.to_parquet(out_path, index=False)
    symbols_out_arg = getattr(args, "symbols_out", None)
    symbols_out_path = _resolve_path(symbols_out_arg) if symbols_out_arg else None
    if symbols_out_path is not None:
        symbols_out_path.parent.mkdir(parents=True, exist_ok=True)
        symbols_out = "\n".join(instruments["symbol"].astype(str).tolist())
        symbols_out_path.write_text(symbols_out + ("\n" if symbols_out else ""), encoding="utf-8")

    manifest_path = Path(f"{out_path}.manifest.yml")
    symbol_metadata = dict(symbol_metadata)
    symbol_metadata["count"] = int(instruments["symbol"].nunique())
    manifest = {
        "name": out_path.stem,
        "created_at": _timestamp_now(),
        "dataset": "hk_instruments",
        "api": "rqdatac.all_instruments",
        "market": "hk",
        "instrument_type": instrument_type,
        "config_ref": getattr(args, "config", None),
        "output_file": str(out_path),
        "format": out_path.suffix.lstrip(".").lower(),
        "symbols_file": str(symbols_out_path) if symbols_out_path is not None else None,
        "symbol_source": symbol_metadata,
        "columns": instruments.columns.tolist(),
        "totals": {
            "rows": int(len(instruments)),
            "symbols": int(instruments["symbol"].nunique()),
            "round_lot_nonnull": int(instruments["round_lot"].notna().sum())
            if "round_lot" in instruments.columns
            else 0,
        },
        "git": _git_metadata(Path.cwd().resolve()),
    }
    _write_manifest(manifest_path, manifest)
    print(
        f"Wrote {len(instruments)} HK instruments "
        f"(instrument_type={instrument_type}) to {out_path} "
        f"(manifest: {manifest_path})"
    )
    if symbols_out_path is not None:
        print(f"Wrote {len(instruments)} HK instrument symbols to {symbols_out_path}")
    return 0


_QUARTER_RE = re.compile(r"^(\d{4})[qQ]([1-4])$")


def _quarter_key(value: object) -> tuple[int, int] | None:
    text = str(value or "").strip()
    match = _QUARTER_RE.match(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _normalize_quarter_arg(value: object, *, label: str) -> str:
    key = _quarter_key(value)
    if key is None:
        raise SystemExit(f"{label} must use YYYYqN format, for example 2025q4.")
    return f"{key[0]}q{key[1]}"


def _quarter_text(key: tuple[int, int]) -> str:
    return f"{key[0]}q{key[1]}"


def _quarter_index(key: tuple[int, int]) -> int:
    return key[0] * 4 + key[1] - 1


def _quarter_from_index(index: int) -> tuple[int, int]:
    year, offset = divmod(index, 4)
    return year, offset + 1


def _quarter_sort_value(value: object) -> int:
    key = _quarter_key(value)
    if key is None:
        return -1
    return _quarter_index(key)


def _quarter_in_range(value: object, start_key: tuple[int, int], end_key: tuple[int, int]) -> bool:
    key = _quarter_key(value)
    return key is not None and start_key <= key <= end_key


def _sort_pit_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "quarter" not in frame.columns:
        return frame.reset_index(drop=True)
    work = frame.copy()
    work["__quarter_order"] = work["quarter"].map(_quarter_sort_value)
    sort_columns = [
        column
        for column in ("symbol", "__quarter_order", "info_date", "rice_create_tm")
        if column in work.columns
    ]
    if sort_columns:
        work = work.sort_values(sort_columns, kind="mergesort")
    return work.drop(columns=["__quarter_order"]).reset_index(drop=True)


def _iter_quarter_chunks(
    start_quarter: str,
    end_quarter: str,
    *,
    chunk_size: int,
) -> list[tuple[str, str]]:
    if chunk_size <= 0:
        raise SystemExit("--quarter-chunk-size must be > 0.")
    start_key = _normalize_quarter_arg(start_quarter, label="--start-quarter")
    end_key = _normalize_quarter_arg(end_quarter, label="--end-quarter")
    start = _quarter_index(_quarter_key(start_key) or (0, 1))
    end = _quarter_index(_quarter_key(end_key) or (0, 1))
    if start > end:
        raise SystemExit("--start-quarter must be <= --end-quarter.")

    chunks: list[tuple[str, str]] = []
    current = start
    while current <= end:
        chunk_end = min(current + chunk_size - 1, end)
        chunks.append(
            (
                _quarter_text(_quarter_from_index(current)),
                _quarter_text(_quarter_from_index(chunk_end)),
            )
        )
        current = chunk_end + 1
    return chunks


def _load_base_pit_asset(base_dir: Path) -> tuple[dict, list[str], list[str]]:
    if not base_dir.exists() or not base_dir.is_dir():
        raise SystemExit(f"--base-asset-dir is not a directory: {base_dir}")
    data_dir = base_dir / "data"
    if not data_dir.exists() or not data_dir.is_dir():
        raise SystemExit(f"Base PIT asset is missing data/: {base_dir}")

    manifest = _load_manifest(base_dir / "manifest.yml")
    if manifest is None:
        raise SystemExit(f"Base PIT asset is missing manifest.yml: {base_dir}")
    if manifest.get("dataset") not in {None, "pit_financials"}:
        raise SystemExit(
            f"Base asset dataset mismatch: expected 'pit_financials', got {manifest.get('dataset')!r}."
        )

    fields = _load_existing_text_list(base_dir / "fields.txt", strip=False)
    query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
    if not fields and isinstance(query, Mapping):
        query_fields = query.get("fields")
        if isinstance(query_fields, Sequence) and not isinstance(query_fields, (str, bytes)):
            fields = [str(field) for field in query_fields if str(field).strip()]
    if not fields:
        raise SystemExit(f"Base PIT asset has no fields.txt or manifest query.fields: {base_dir}")

    symbols = _load_existing_text_list(base_dir / "symbols.txt")
    if not symbols:
        entries = manifest.get("entries")
        if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
            symbols = [
                _normalize_hk_symbol(item.get("symbol"))
                for item in entries
                if isinstance(item, Mapping) and item.get("symbol")
            ]
    if not symbols:
        symbols = [_normalize_hk_symbol(path.stem) for path in sorted(data_dir.glob("*.parquet"))]
    symbols = _dedupe_preserve_order(_normalize_hk_symbol(symbol) for symbol in symbols)
    if not symbols:
        raise SystemExit(f"Base PIT asset has no symbols: {base_dir}")

    return manifest, fields, symbols


def _resolve_pit_patch_symbols(args, *, base_symbols: Sequence[str]) -> tuple[list[str], dict]:
    explicit_symbols: list[str] = []
    symbols_file = getattr(args, "symbols_file", None)
    if symbols_file:
        explicit_symbols.extend(_load_text_list(symbols_file, label="Symbols file"))
    explicit_symbols.extend(str(item) for item in (getattr(args, "symbol", None) or []))

    if explicit_symbols:
        symbols = [_normalize_hk_symbol(symbol) for symbol in explicit_symbols]
        source_mode = "explicit"
    else:
        symbols = list(base_symbols)
        source_mode = "base_symbols"
    symbols = _dedupe_preserve_order(symbols)

    limit = getattr(args, "limit", None)
    if limit is not None:
        if limit <= 0:
            raise SystemExit("--limit must be > 0.")
        symbols = symbols[:limit]
    if not symbols:
        raise SystemExit("No HK symbols selected for PIT patch.")

    base_set = set(base_symbols)
    symbols_not_in_base = [symbol for symbol in symbols if symbol not in base_set]
    metadata = {
        "mode": source_mode,
        "count": len(symbols),
        "base_count": len(base_symbols),
        "symbols_file": str(_resolve_path(symbols_file)) if symbols_file else None,
        "symbols_not_in_base_count": len(symbols_not_in_base),
        "symbols_not_in_base_sample": symbols_not_in_base[:20],
    }
    return symbols, metadata


def _default_pit_patch_name(
    *,
    base_dir: Path,
    target_date: str,
    patch_start_quarter: str,
    patch_end_quarter: str,
) -> str:
    return f"{base_dir.name}_patch_{patch_start_quarter}_{patch_end_quarter}_asof_{target_date}"


def _link_or_copy_file(source: Path, dest: Path) -> str:
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)
        return "copy"
    return "hardlink"


def _pit_merge_columns(base_columns: Sequence[str], patch_columns: Sequence[str]) -> list[str]:
    preferred = ["symbol", "order_book_id", "quarter"]
    ordered = [*preferred, *base_columns, *patch_columns]
    return _dedupe_preserve_order(ordered, strip=False)


def _merge_pit_patch_frame(
    *,
    base_frame: pd.DataFrame,
    patch_frame: pd.DataFrame,
    fields: Sequence[str],
    patch_start_key: tuple[int, int],
    patch_end_key: tuple[int, int],
) -> pd.DataFrame:
    if patch_frame.empty:
        return _sort_pit_frame(_ensure_requested_fields(base_frame, fields))
    if "quarter" not in patch_frame.columns:
        raise ValueError("PIT patch payload is missing quarter.")

    patch = _ensure_requested_fields(patch_frame.copy(), fields)
    patch["quarter"] = patch["quarter"].astype(str)
    if base_frame.empty and len(base_frame.columns) == 0:
        merged = patch
        base_columns: list[str] = []
    else:
        if "quarter" not in base_frame.columns:
            raise ValueError("Base PIT frame is missing quarter.")
        base = _ensure_requested_fields(base_frame.copy(), fields)
        base["quarter"] = base["quarter"].astype(str)
        keep_mask = ~base["quarter"].map(
            lambda value: _quarter_in_range(value, patch_start_key, patch_end_key)
        )
        merged = pd.concat([base.loc[keep_mask].copy(), patch], ignore_index=True, sort=False)
        base_columns = base.columns.tolist()

    columns = _pit_merge_columns(base_columns, patch.columns.tolist())
    merged = merged.reindex(columns=columns)
    return _sort_pit_frame(merged)


def _resolve_pit_mirror_fields(args) -> tuple[list[str], dict]:
    return _resolve_fields_with_overrides(
        args,
        load_hk_financial_fields_override=_load_hk_financial_fields,
    )


def _copy_args_with(args, **overrides) -> SimpleNamespace:
    values = dict(vars(args))
    values.update(overrides)
    return SimpleNamespace(**values)


def _mirror_hk_pit_financials_unpartitioned(args, rqdatac) -> int:
    return _mirror_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="pit_financials",
        api_name="rqdatac.get_pit_financials_ex",
        fetch_batch=lambda order_book_ids, fields, start_quarter, end_quarter, **kwargs: rqdatac.get_pit_financials_ex(
            order_book_ids=order_book_ids,
            fields=list(fields),
            start_quarter=start_quarter,
            end_quarter=end_quarter,
            market="hk",
            **kwargs,
        ),
    )


def _compose_partitioned_pit_snapshot(
    *,
    args,
    output_dir: Path,
    part_dirs: Sequence[Path],
    partition_batches: Sequence[Mapping[str, object]],
    fields: Sequence[str],
    field_metadata: Mapping[str, object],
    symbols: Sequence[str],
    symbol_metadata: Mapping[str, object],
    status: str,
    error: str | None,
    result_code: int,
    started_at: str,
) -> int:
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "audit.csv"

    entries_by_symbol = {}
    audit_by_symbol = {}
    columns: list[str] = []
    field_coverage = _field_coverage_template(fields)
    compose_started_at = _timestamp_now()

    def _record_written(symbol: str, entry, symbol_frame: pd.DataFrame) -> None:
        nonlocal columns
        entries_by_symbol[symbol] = entry
        if not columns and not symbol_frame.empty:
            columns = symbol_frame.columns.tolist()
        _update_field_coverage(field_coverage, symbol_frame, fields=fields)
        audit_by_symbol[symbol] = _audit_record(
            symbol=symbol,
            order_book_id=entry.order_book_id,
            status="written",
            attempts=0,
            started_at=compose_started_at,
            finished_at=_timestamp_now(),
            file_mtime=_path_mtime_iso(entry.path),
            error=None,
            entry=entry,
        )

    def _record_missing(symbol: str) -> None:
        audit_by_symbol[symbol] = _audit_record(
            symbol=symbol,
            order_book_id=_to_rqdata_symbol("hk", symbol),
            status="missing_remote",
            attempts=0,
            started_at=compose_started_at,
            finished_at=_timestamp_now(),
            file_mtime=None,
            error=None,
            entry=None,
        )

    for symbol in symbols:
        frames: list[pd.DataFrame] = []
        for part_dir in part_dirs:
            part_path = part_dir / "data" / f"{symbol}.parquet"
            if not part_path.exists():
                continue
            _, frame = _load_existing_entry(part_path, fields=fields)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            _record_missing(symbol)
            continue

        combined = pd.concat(frames, ignore_index=True, sort=False)
        combined = _sort_pit_frame(_ensure_requested_fields(combined, fields))
        entry = _write_symbol_frame(data_dir, combined)
        _record_written(symbol, entry, combined)

    finished_at = _timestamp_now()
    audit_records = [audit_by_symbol[symbol] for symbol in symbols]
    _write_audit_csv(audit_path, audit_records)

    manifest = _build_manifest(
        dataset_name="pit_financials",
        api_name="rqdatac.get_pit_financials_ex + quarter_partition_compose",
        output_dir=output_dir,
        fields=fields,
        field_metadata=field_metadata,
        symbol_metadata=symbol_metadata,
        symbols_requested=symbols,
        entries=[entries_by_symbol[symbol] for symbol in symbols if symbol in entries_by_symbol],
        missing_symbols=[item.symbol for item in audit_records if item.status == "missing_remote"],
        query_date=getattr(args, "date", None),
        start_quarter=args.start_quarter,
        end_quarter=args.end_quarter,
        statements=args.statements,
        batches=partition_batches,
        columns=columns,
        audit_file=audit_path,
        audit_records=audit_records,
        field_coverage=list(field_coverage.values()),
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        config_ref=getattr(args, "config", None),
    )
    manifest["partitioning"] = {
        "strategy": "quarter_window_parts",
        "strict_full_snapshot": status == "completed",
        "quarter_chunk_size": int(getattr(args, "quarter_chunk_size")),
        "parts_root": str(output_dir / ".parts"),
        "parts": [
            {
                "name": item.get("name"),
                "start_quarter": item.get("start_quarter"),
                "end_quarter": item.get("end_quarter"),
                "status": item.get("status"),
                "path": item.get("path"),
                "return_code": item.get("return_code"),
            }
            for item in partition_batches
        ],
    }
    _write_manifest(output_dir / "manifest.yml", manifest)
    print(
        f"Wrote partitioned pit_financials mirror to {output_dir} "
        f"({manifest['totals']['symbols_written']} symbols, {manifest['totals']['rows']} rows, "
        f"{len(partition_batches)} quarter parts, status={status})"
    )
    return result_code


def _mirror_hk_pit_financials_partitioned(args, rqdatac) -> int:
    chunk_size = int(getattr(args, "quarter_chunk_size", 0) or 0)
    chunks = _iter_quarter_chunks(
        args.start_quarter,
        args.end_quarter,
        chunk_size=chunk_size,
    )
    fields, field_metadata = _resolve_pit_mirror_fields(args)
    symbols, symbol_metadata = _resolve_symbols(args)
    resume = bool(getattr(args, "resume", False))
    output_dir = _prepare_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="pit_financials",
        start_quarter=args.start_quarter,
        end_quarter=args.end_quarter,
        statements=args.statements,
        name=getattr(args, "name", None),
        resume=resume,
    )
    _write_text_list(output_dir / "fields.txt", fields)
    _write_text_list(output_dir / "symbols.txt", symbols)

    parts_root = output_dir / ".parts"
    partition_batches: list[dict[str, object]] = []
    part_dirs: list[Path] = []
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    output_name = output_dir.name

    for chunk_start, chunk_end in chunks:
        part_name = f"{output_name}__part_{chunk_start}_{chunk_end}"
        part_args = _copy_args_with(
            args,
            out_root=str(parts_root),
            name=part_name,
            start_quarter=chunk_start,
            end_quarter=chunk_end,
            quarter_chunk_size=None,
        )
        part_dir = parts_root / "hk" / "pit_financials" / part_name
        part_code = _mirror_hk_pit_financials_unpartitioned(part_args, rqdatac)
        part_manifest = _load_manifest(part_dir / "manifest.yml") or {}
        part_status = str(part_manifest.get("status") or "missing_manifest")
        part_error = part_manifest.get("error")
        partition_batches.append(
            {
                "name": part_name,
                "start_quarter": chunk_start,
                "end_quarter": chunk_end,
                "status": part_status,
                "return_code": part_code,
                "path": str(part_dir),
                "rows": (part_manifest.get("totals") or {}).get("rows")
                if isinstance(part_manifest.get("totals"), Mapping)
                else None,
                "symbols_written": (part_manifest.get("totals") or {}).get("symbols_written")
                if isinstance(part_manifest.get("totals"), Mapping)
                else None,
                "error": part_error,
            }
        )
        part_dirs.append(part_dir)
        result_code = max(result_code, int(part_code or 0))
        if part_status == "stopped_quota":
            status = "stopped_quota"
            error = str(part_error or "quarter partition stopped on quota")
            result_code = max(result_code, 2)
            break
        if part_code or part_status not in {"completed"}:
            status = "completed_with_failures"
            error = str(part_error or f"quarter partition {part_name} did not complete")
            result_code = max(result_code, 1)
            break

    return _compose_partitioned_pit_snapshot(
        args=args,
        output_dir=output_dir,
        part_dirs=part_dirs,
        partition_batches=partition_batches,
        fields=fields,
        field_metadata=field_metadata,
        symbols=symbols,
        symbol_metadata=symbol_metadata,
        status=status,
        error=error,
        result_code=result_code,
        started_at=started_at,
    )


def _validate_pit_patch_resume_manifest(
    existing_manifest: Mapping[str, object] | None,
    *,
    target_date: str,
    statements: str,
    patch_start_quarter: str,
    patch_end_quarter: str,
) -> None:
    if not existing_manifest:
        return

    existing_query = (
        existing_manifest.get("query")
        if isinstance(existing_manifest.get("query"), Mapping)
        else {}
    )
    existing_patch = (
        existing_manifest.get("patch")
        if isinstance(existing_manifest.get("patch"), Mapping)
        else {}
    )
    checks = [
        ("query.date", existing_query.get("date"), target_date),
        ("query.statements", existing_query.get("statements"), statements),
        (
            "patch.patch_start_quarter",
            existing_patch.get("patch_start_quarter"),
            patch_start_quarter,
        ),
        ("patch.patch_end_quarter", existing_patch.get("patch_end_quarter"), patch_end_quarter),
    ]
    for label, actual, expected in checks:
        if actual not in {None, expected}:
            raise SystemExit(
                f"Resume target mismatch for {label}: expected {expected!r}, got {actual!r}."
            )


def _pit_entry_manifest_rows(entries: Sequence[object]) -> list[dict[str, object]]:
    return [
        {
            "symbol": item.symbol,
            "order_book_id": item.order_book_id,
            "path": str(item.path),
            "rows": item.rows,
            "total_bytes": item.total_bytes,
            "min_quarter": item.min_quarter,
            "max_quarter": item.max_quarter,
            "min_info_date": item.min_info_date,
            "max_info_date": item.max_info_date,
        }
        for item in entries
    ]


def _pit_audit_symbols_with_status(
    audit_records: Sequence[object],
    status: str,
) -> list[str]:
    return [item.symbol for item in audit_records if item.status == status]


def _pit_patch_totals(
    *,
    symbols: Sequence[str],
    entries: Sequence[object],
    status_counts: Counter,
) -> dict[str, int]:
    return {
        "symbols_requested": len(symbols),
        "symbols_written": len(entries),
        "symbols_merged_patch": int(status_counts.get("merged_patch", 0)),
        "symbols_patch_only": int(status_counts.get("patch_only", 0)),
        "symbols_linked_base": int(status_counts.get("linked_base", 0)),
        "symbols_skipped_existing": int(status_counts.get("skipped_existing", 0)),
        "symbols_missing_remote": int(status_counts.get("missing_base_and_patch", 0)),
        "symbols_failed": int(status_counts.get("failed", 0)),
        "symbols_quota_blocked": int(status_counts.get("quota_blocked", 0)),
        "files": len(entries),
        "rows": sum(item.rows for item in entries),
        "bytes": sum(item.total_bytes for item in entries),
    }


def _build_pit_patch_manifest(
    *,
    args,
    output_dir: Path,
    audit_path: Path,
    base_dir: Path,
    base_manifest: Mapping[str, object],
    base_query: Mapping[str, object],
    target_date: str,
    statements: str,
    fields: Sequence[str],
    patch_start_quarter: str,
    patch_end_quarter: str,
    query_start_quarter: str,
    query_end_quarter: str,
    symbol_metadata: Mapping[str, object],
    symbols: Sequence[str],
    columns: Sequence[str],
    field_coverage_rows: Sequence[Mapping[str, object]],
    batches: Sequence[Mapping[str, object]],
    entries: Sequence[object],
    audit_records: Sequence[object],
    status_counts: Counter,
    started_at: str,
    finished_at: str,
    status: str,
    error: str | None,
) -> dict[str, object]:
    return {
        "name": output_dir.name,
        "created_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "error": error,
        "dataset": "pit_financials",
        "api": "rqdatac.get_pit_financials_ex + base_patch_merge",
        "market": "hk",
        "config_ref": getattr(args, "config", None),
        "repo_root": str(Path.cwd().resolve()),
        "output_dir": str(output_dir),
        "query": {
            "start_quarter": query_start_quarter,
            "end_quarter": query_end_quarter,
            "date": target_date,
            "statements": statements,
            "fields_count": len(fields),
            "fields": list(fields),
            "patch_start_quarter": patch_start_quarter,
            "patch_end_quarter": patch_end_quarter,
        },
        "patch": {
            "strategy": "base_plus_quarter_patch",
            "strict_full_snapshot": False,
            "merge_key": ["symbol", "quarter"],
            "base_asset_dir": str(base_dir),
            "base_name": base_dir.name,
            "base_status": base_manifest.get("status"),
            "base_query": dict(base_query),
            "base_as_of": base_query.get("date"),
            "target_as_of": target_date,
            "patch_start_quarter": patch_start_quarter,
            "patch_end_quarter": patch_end_quarter,
            "unchanged_base_file_mode": "hardlink_or_copy",
            "limitation": (
                "Only quarters in the patch window are refreshed at target_as_of; older-quarter "
                "restatements after the base snapshot require a wider patch window or a strict "
                "full mirror."
            ),
        },
        "symbol_source": dict(symbol_metadata),
        "columns": list(columns),
        "audit_file": str(audit_path),
        "status_counts": dict(status_counts),
        "field_coverage": list(field_coverage_rows),
        "batches": list(batches),
        "entries": _pit_entry_manifest_rows(entries),
        "missing_symbols": _pit_audit_symbols_with_status(
            audit_records,
            "missing_base_and_patch",
        ),
        "failed_symbols": _pit_audit_symbols_with_status(audit_records, "failed"),
        "quota_blocked_symbols": _pit_audit_symbols_with_status(
            audit_records,
            "quota_blocked",
        ),
        "totals": _pit_patch_totals(
            symbols=symbols,
            entries=entries,
            status_counts=status_counts,
        ),
        "git": _git_metadata(Path.cwd().resolve()),
    }


@dataclass(frozen=True)
class _PitPatchContext:
    base_dir: Path
    base_manifest: Mapping[str, object]
    fields: list[str]
    base_data_dir: Path
    target_date: str
    patch_start_quarter: str
    patch_end_quarter: str
    patch_start_key: tuple[int, int]
    patch_end_key: tuple[int, int]
    statements: str
    symbols: list[str]
    symbol_metadata: dict[str, object]
    symbol_map: dict[str, str]
    order_book_ids: list[str]
    base_query: Mapping[str, object]
    query_start_quarter: str
    query_end_quarter: str
    output_dir: Path
    data_dir: Path
    audit_path: Path


def _prepare_pit_patch_context(args) -> _PitPatchContext:
    base_dir = _resolve_path(args.base_asset_dir)
    base_manifest, fields, base_symbols = _load_base_pit_asset(base_dir)
    base_data_dir = base_dir / "data"

    target_date = _normalize_absolute_date(args.target_date, label="--target-date")
    patch_start_quarter = _normalize_quarter_arg(
        args.patch_start_quarter,
        label="--patch-start-quarter",
    )
    patch_end_quarter = _normalize_quarter_arg(
        args.patch_end_quarter,
        label="--patch-end-quarter",
    )
    patch_start_key = _quarter_key(patch_start_quarter)
    patch_end_key = _quarter_key(patch_end_quarter)
    if patch_start_key is None or patch_end_key is None or patch_start_key > patch_end_key:
        raise SystemExit("--patch-start-quarter must be <= --patch-end-quarter.")

    statements = str(getattr(args, "statements", "latest") or "latest")
    if statements not in {"latest", "all"}:
        raise SystemExit("--statements must be 'latest' or 'all'.")

    symbols, symbol_metadata = _resolve_pit_patch_symbols(args, base_symbols=base_symbols)
    symbol_map = {_to_rqdata_symbol("hk", symbol): symbol for symbol in symbols}
    order_book_ids = list(symbol_map.keys())

    base_query = base_manifest.get("query") if isinstance(base_manifest.get("query"), Mapping) else {}
    base_start_quarter = str(base_query.get("start_quarter") or patch_start_quarter)
    base_end_quarter = str(base_query.get("end_quarter") or patch_end_quarter)
    base_start_key = _quarter_key(base_start_quarter) or patch_start_key
    base_end_key = _quarter_key(base_end_quarter) or patch_end_key
    query_start_quarter = _quarter_text(min(base_start_key, patch_start_key))
    query_end_quarter = _quarter_text(max(base_end_key, patch_end_key))

    resume = bool(getattr(args, "resume", False))
    output_name = getattr(args, "name", None) or _default_pit_patch_name(
        base_dir=base_dir,
        target_date=target_date,
        patch_start_quarter=patch_start_quarter,
        patch_end_quarter=patch_end_quarter,
    )
    output_dir = _prepare_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="pit_financials",
        start_quarter=query_start_quarter,
        end_quarter=query_end_quarter,
        statements=statements,
        name=output_name,
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return _PitPatchContext(
        base_dir=base_dir,
        base_manifest=base_manifest,
        fields=fields,
        base_data_dir=base_data_dir,
        target_date=target_date,
        patch_start_quarter=patch_start_quarter,
        patch_end_quarter=patch_end_quarter,
        patch_start_key=patch_start_key,
        patch_end_key=patch_end_key,
        statements=statements,
        symbols=symbols,
        symbol_metadata=symbol_metadata,
        symbol_map=symbol_map,
        order_book_ids=order_book_ids,
        base_query=base_query,
        query_start_quarter=query_start_quarter,
        query_end_quarter=query_end_quarter,
        output_dir=output_dir,
        data_dir=data_dir,
        audit_path=output_dir / "audit.csv",
    )


def _pit_patch_quota_batch_summary(
    *,
    order_book_ids: int,
    attempts: int,
    error: str,
) -> dict[str, object]:
    return {
        "order_book_ids": order_book_ids,
        "rows": 0,
        "symbols_merged_patch": 0,
        "symbols_linked_base": 0,
        "symbols_missing_base_and_patch": 0,
        "status": "quota_blocked",
        "attempts": attempts,
        "error": error,
    }


def _pit_patch_error_batch_summary(
    *,
    order_book_ids: int,
    status: str,
    attempts: int,
    error: str,
) -> dict[str, object]:
    return {
        "order_book_ids": order_book_ids,
        "rows": 0,
        "symbols_merged_patch": 0,
        "symbols_linked_base": 0,
        "symbols_missing_base_and_patch": 0,
        "status": status,
        "attempts": attempts,
        "error": error,
    }


def _process_pit_patch_batch(
    *,
    batch_order_book_ids: list[str],
    rqdatac,
    context: _PitPatchContext,
    max_attempts: int,
    backoff_seconds: float,
    max_backoff_seconds: float,
    status: str,
    error: str | None,
    result_code: int,
    quota_blocked: bool,
    batches: list[dict[str, object]],
    audit_by_symbol: Mapping[str, object],
    record_non_entry,
    write_symbol_patch_result,
) -> tuple[str, str | None, int, bool]:
    if not batch_order_book_ids or quota_blocked:
        return status, error, result_code, quota_blocked

    batch_symbol_map = {
        order_book_id: context.symbol_map[order_book_id]
        for order_book_id in batch_order_book_ids
    }
    batch_started_at = _timestamp_now()
    try:
        label = f"pit_financials patch fetch failed for {', '.join(batch_order_book_ids)}"
        payload, attempts = _retry_fetch(
            label,
            lambda: rqdatac.get_pit_financials_ex(
                order_book_ids=batch_order_book_ids,
                fields=list(context.fields),
                start_quarter=context.patch_start_quarter,
                end_quarter=context.patch_end_quarter,
                date=context.target_date,
                statements=context.statements,
                market="hk",
            ),
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )
    except MirrorQuotaError as exc:
        batch_finished_at = _timestamp_now()
        quota_blocked = True
        status = "stopped_quota"
        error = str(exc)
        result_code = max(result_code, 2)
        for order_book_id in batch_order_book_ids:
            symbol = context.symbol_map[order_book_id]
            if symbol in audit_by_symbol:
                continue
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="quota_blocked",
                attempts=exc.attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                error_text=str(exc),
            )
        batches.append(
            _pit_patch_quota_batch_summary(
                order_book_ids=len(batch_order_book_ids),
                attempts=exc.attempts,
                error=str(exc),
            )
        )
        return status, error, result_code, quota_blocked
    except MirrorFetchError as exc:
        batch_finished_at = _timestamp_now()
        if len(batch_order_book_ids) > 1:
            batches.append(
                _pit_patch_error_batch_summary(
                    order_book_ids=len(batch_order_book_ids),
                    status="split_after_error",
                    attempts=exc.attempts,
                    error=str(exc),
                )
            )
            for order_book_id in batch_order_book_ids:
                status, error, result_code, quota_blocked = _process_pit_patch_batch(
                    batch_order_book_ids=[order_book_id],
                    rqdatac=rqdatac,
                    context=context,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds,
                    max_backoff_seconds=max_backoff_seconds,
                    status=status,
                    error=error,
                    result_code=result_code,
                    quota_blocked=quota_blocked,
                    batches=batches,
                    audit_by_symbol=audit_by_symbol,
                    record_non_entry=record_non_entry,
                    write_symbol_patch_result=write_symbol_patch_result,
                )
                if quota_blocked:
                    break
            return status, error, result_code, quota_blocked

        order_book_id = batch_order_book_ids[0]
        symbol = context.symbol_map[order_book_id]
        record_non_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="failed",
            attempts=exc.attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
            error_text=str(exc),
        )
        batches.append(
            _pit_patch_error_batch_summary(
                order_book_ids=1,
                status="failed",
                attempts=exc.attempts,
                error=str(exc),
            )
        )
        status = "completed_with_failures" if status == "completed" else status
        result_code = max(result_code, 1)
        return status, error, result_code, quota_blocked

    batch_finished_at = _timestamp_now()
    prepared = _prepare_asset_frame(payload, symbol_map=batch_symbol_map)
    prepared = _ensure_requested_fields(prepared, context.fields)

    before_counts = Counter(item.status for item in audit_by_symbol.values())
    for order_book_id in batch_order_book_ids:
        symbol = context.symbol_map[order_book_id]
        if prepared.empty:
            patch_symbol_frame = pd.DataFrame()
        else:
            patch_symbol_frame = prepared[prepared["symbol"] == symbol].reset_index(drop=True)
        write_symbol_patch_result(
            symbol=symbol,
            order_book_id=order_book_id,
            patch_symbol_frame=patch_symbol_frame,
            attempts=attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
        )
    delta_counts = Counter(item.status for item in audit_by_symbol.values()) - before_counts
    batches.append(
        {
            "order_book_ids": len(batch_order_book_ids),
            "rows": int(len(prepared)),
            "symbols_merged_patch": int(delta_counts.get("merged_patch", 0)),
            "symbols_patch_only": int(delta_counts.get("patch_only", 0)),
            "symbols_linked_base": int(delta_counts.get("linked_base", 0)),
            "symbols_missing_base_and_patch": int(
                delta_counts.get("missing_base_and_patch", 0)
            ),
            "status": "completed",
            "attempts": attempts,
        }
    )
    return status, error, result_code, quota_blocked


def _collect_pending_pit_patch_order_book_ids(
    *,
    context: _PitPatchContext,
    skip_existing: bool,
    record_entry,
) -> list[str]:
    pending_order_book_ids: list[str] = []
    for order_book_id in context.order_book_ids:
        symbol = context.symbol_map[order_book_id]
        out_path = context.data_dir / f"{symbol}.parquet"
        if skip_existing and out_path.exists():
            entry, symbol_frame = _load_existing_entry(out_path, fields=context.fields)
            record_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="skipped_existing",
                attempts=0,
                started_at_value=None,
                finished_at_value=_path_mtime_iso(out_path),
                symbol_frame=symbol_frame,
                entry=entry,
            )
            continue
        pending_order_book_ids.append(order_book_id)
    return pending_order_book_ids


def _record_unfinished_pit_patch_symbols(
    *,
    context: _PitPatchContext,
    pending_order_book_ids: Sequence[str],
    audit_by_symbol: Mapping[str, object],
    quota_blocked: bool,
    finished_at: str,
    error: str | None,
    status: str,
    result_code: int,
    record_non_entry,
) -> tuple[str, int]:
    if quota_blocked:
        for order_book_id in pending_order_book_ids:
            symbol = context.symbol_map[order_book_id]
            if symbol in audit_by_symbol:
                continue
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="quota_blocked",
                attempts=0,
                started_at_value=None,
                finished_at_value=finished_at,
                error_text=error,
            )

    for order_book_id in context.order_book_ids:
        symbol = context.symbol_map[order_book_id]
        if symbol in audit_by_symbol:
            continue
        record_non_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="failed",
            attempts=0,
            started_at_value=None,
            finished_at_value=finished_at,
            error_text=error or "missing audit status",
        )
        status = "completed_with_failures" if status == "completed" else status
        result_code = max(result_code, 1)
    return status, result_code


def patch_hk_pit_financials(args, rqdatac) -> int:
    _ensure_rqdatac_hk_plugin()

    context = _prepare_pit_patch_context(args)
    base_dir = context.base_dir
    base_manifest = context.base_manifest
    fields = context.fields
    base_data_dir = context.base_data_dir
    target_date = context.target_date
    patch_start_quarter = context.patch_start_quarter
    patch_end_quarter = context.patch_end_quarter
    patch_start_key = context.patch_start_key
    patch_end_key = context.patch_end_key
    statements = context.statements
    symbols = context.symbols
    symbol_metadata = context.symbol_metadata
    base_query = context.base_query
    query_start_quarter = context.query_start_quarter
    query_end_quarter = context.query_end_quarter
    resume = bool(getattr(args, "resume", False))
    skip_existing = bool(getattr(args, "skip_existing", False) or resume)
    output_dir = context.output_dir
    data_dir = context.data_dir
    audit_path = context.audit_path

    existing_manifest = _load_manifest(output_dir / "manifest.yml") if resume else None
    _validate_pit_patch_resume_manifest(
        existing_manifest,
        target_date=target_date,
        statements=statements,
        patch_start_quarter=patch_start_quarter,
        patch_end_quarter=patch_end_quarter,
    )

    _write_text_list(output_dir / "fields.txt", fields)
    _write_text_list(output_dir / "symbols.txt", symbols)

    max_attempts = max(1, int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1))
    backoff_seconds = float(getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS))
    max_backoff_seconds = float(
        getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
    )

    entries_by_symbol = {}
    audit_by_symbol = {}
    batches: list[dict[str, object]] = []
    columns: list[str] = []
    field_coverage = _field_coverage_template(fields)
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    quota_blocked = False

    def _record_entry(
        *,
        symbol: str,
        order_book_id: str,
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        symbol_frame: pd.DataFrame,
        entry,
        error_text: str | None = None,
    ) -> None:
        nonlocal columns
        entries_by_symbol[symbol] = entry
        if not columns and not symbol_frame.empty:
            columns = symbol_frame.columns.tolist()
        _update_field_coverage(field_coverage, symbol_frame, fields=fields)
        audit_by_symbol[symbol] = _audit_record(
            symbol=symbol,
            order_book_id=order_book_id,
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=_path_mtime_iso(entry.path),
            error=error_text,
            entry=entry,
        )

    def _record_non_entry(
        *,
        symbol: str,
        order_book_id: str,
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        error_text: str | None = None,
    ) -> None:
        audit_by_symbol[symbol] = _audit_record(
            symbol=symbol,
            order_book_id=order_book_id,
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=None,
            error=error_text,
            entry=None,
        )

    def _link_base_symbol(
        *,
        symbol: str,
        order_book_id: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
    ) -> None:
        base_path = base_data_dir / f"{symbol}.parquet"
        out_path = data_dir / f"{symbol}.parquet"
        _link_or_copy_file(base_path, out_path)
        entry, symbol_frame = _load_existing_entry(out_path, fields=fields)
        _record_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="linked_base",
            attempts=attempts,
            started_at_value=started_at_value,
            finished_at_value=finished_at_value,
            symbol_frame=symbol_frame,
            entry=entry,
        )

    def _write_symbol_patch_result(
        *,
        symbol: str,
        order_book_id: str,
        patch_symbol_frame: pd.DataFrame,
        attempts: int,
        started_at_value: str,
        finished_at_value: str,
    ) -> None:
        base_path = base_data_dir / f"{symbol}.parquet"
        if patch_symbol_frame.empty:
            if base_path.exists():
                _link_base_symbol(
                    symbol=symbol,
                    order_book_id=order_book_id,
                    attempts=attempts,
                    started_at_value=started_at_value,
                    finished_at_value=finished_at_value,
                )
            else:
                _record_non_entry(
                    symbol=symbol,
                    order_book_id=order_book_id,
                    record_status="missing_base_and_patch",
                    attempts=attempts,
                    started_at_value=started_at_value,
                    finished_at_value=finished_at_value,
                )
            return

        if base_path.exists():
            _, base_frame = _load_existing_entry(base_path, fields=fields)
            merged = _merge_pit_patch_frame(
                base_frame=base_frame,
                patch_frame=patch_symbol_frame,
                fields=fields,
                patch_start_key=patch_start_key,
                patch_end_key=patch_end_key,
            )
            record_status = "merged_patch"
        else:
            merged = _sort_pit_frame(_ensure_requested_fields(patch_symbol_frame, fields))
            record_status = "patch_only"

        entry = _write_symbol_frame(data_dir, merged)
        _record_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status=record_status,
            attempts=attempts,
            started_at_value=started_at_value,
            finished_at_value=finished_at_value,
            symbol_frame=merged,
            entry=entry,
        )

    def _process_batch(batch_order_book_ids: list[str]) -> None:
        nonlocal status, error, result_code, quota_blocked
        status, error, result_code, quota_blocked = _process_pit_patch_batch(
            batch_order_book_ids=batch_order_book_ids,
            rqdatac=rqdatac,
            context=context,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
            status=status,
            error=error,
            result_code=result_code,
            quota_blocked=quota_blocked,
            batches=batches,
            audit_by_symbol=audit_by_symbol,
            record_non_entry=_record_non_entry,
            write_symbol_patch_result=_write_symbol_patch_result,
        )

    pending_order_book_ids = _collect_pending_pit_patch_order_book_ids(
        context=context,
        skip_existing=skip_existing,
        record_entry=_record_entry,
    )

    for batch_order_book_ids in _chunked(
        pending_order_book_ids,
        int(getattr(args, "batch_size", DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE),
    ):
        if quota_blocked:
            break
        _process_batch(batch_order_book_ids)

    finished_at = _timestamp_now()
    status, result_code = _record_unfinished_pit_patch_symbols(
        context=context,
        pending_order_book_ids=pending_order_book_ids,
        audit_by_symbol=audit_by_symbol,
        quota_blocked=quota_blocked,
        finished_at=finished_at,
        error=error,
        status=status,
        result_code=result_code,
        record_non_entry=_record_non_entry,
    )

    audit_records = [audit_by_symbol[symbol] for symbol in symbols]
    status_counts = Counter(item.status for item in audit_records)
    if status == "completed" and int(status_counts.get("failed", 0)) > 0:
        status = "completed_with_failures"
        result_code = max(result_code, 1)

    _write_audit_csv(audit_path, audit_records)
    entries = [entries_by_symbol[symbol] for symbol in symbols if symbol in entries_by_symbol]
    manifest = _build_pit_patch_manifest(
        args=args,
        output_dir=output_dir,
        audit_path=audit_path,
        base_dir=base_dir,
        base_manifest=base_manifest,
        base_query=base_query,
        target_date=target_date,
        statements=statements,
        fields=fields,
        patch_start_quarter=patch_start_quarter,
        patch_end_quarter=patch_end_quarter,
        query_start_quarter=query_start_quarter,
        query_end_quarter=query_end_quarter,
        symbol_metadata=symbol_metadata,
        symbols=symbols,
        columns=columns,
        field_coverage_rows=list(field_coverage.values()),
        batches=batches,
        entries=entries,
        audit_records=audit_records,
        status_counts=status_counts,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
    )
    _write_manifest(output_dir / "manifest.yml", manifest)
    print(
        f"Wrote pit_financials patch mirror to {output_dir} "
        f"({len(entries)} symbols, {manifest['totals']['rows']} rows, "
        f"patch={patch_start_quarter}->{patch_end_quarter}, target={target_date}, status={status})"
    )
    return result_code


def mirror_hk_pit_financials(args, rqdatac) -> int:
    if int(getattr(args, "quarter_chunk_size", 0) or 0) > 0:
        return _mirror_hk_pit_financials_partitioned(args, rqdatac)
    return _mirror_hk_pit_financials_unpartitioned(args, rqdatac)


def mirror_hk_financial_details(args, rqdatac) -> int:
    _ensure_rqdatac_hk_plugin()
    hk_api = getattr(rqdatac, "hk", None)
    if hk_api is None or not hasattr(hk_api, "get_detailed_financial_items"):
        raise SystemExit(
            "rqdatac.hk.get_detailed_financial_items is unavailable. Check rqdatac-hk installation."
        )
    return _mirror_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="financial_details",
        api_name="rqdatac.hk.get_detailed_financial_items",
        fetch_batch=lambda order_book_ids, fields, start_quarter, end_quarter, **kwargs: hk_api.get_detailed_financial_items(
            order_book_ids=order_book_ids,
            fields=list(fields),
            start_quarter=start_quarter,
            end_quarter=end_quarter,
            market="hk",
            **kwargs,
        ),
    )
