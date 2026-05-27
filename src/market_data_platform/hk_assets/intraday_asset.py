from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from pathlib import Path

import pandas as pd

from market_data_platform.intraday_paths import resolve_intraday_input_groups
from market_data_platform.symbols import normalize_symbol_for_market
from .shared import (
    _git_metadata,
    _normalize_frame_columns,
    _path_mtime_iso,
    _prepare_daily_output_dir,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)


DEFAULT_INTRADAY_VALUE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)


def _normalize_intraday_field_name(value: object) -> str:
    text = str(value or "").strip()
    if text == "total_turnover":
        return "amount"
    return text


def _remove_existing_path(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _resolve_alias_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _create_relative_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        _remove_existing_path(link)
    rel_target = os.path.relpath(target, start=link.parent)
    os.symlink(rel_target, link, target_is_directory=target.is_dir())


def _directory_file_count(path: Path) -> int:
    return sum(1 for child in path.rglob("*") if child.is_file())


def _directory_size_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _load_json(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _normalize_optional_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y%m%d")


def _resolve_intraday_symbol_series(frame: pd.DataFrame) -> pd.Series:
    for column in ("symbol", "ts_code", "rq_order_book_id", "order_book_id"):
        if column in frame.columns:
            return frame[column]
    raise SystemExit(
        "Intraday frame is missing a canonical symbol column. "
        "Legacy aliases ts_code, rq_order_book_id, and order_book_id remain accepted."
    )


def _scan_intraday_paths(read_paths: list[Path]) -> dict[str, object]:
    rows = 0
    symbol_set: set[str] = set()
    min_trade_date: str | None = None
    max_trade_date: str | None = None
    columns: list[str] = []

    for read_path in read_paths:
        frame = _normalize_frame_columns(pd.read_parquet(read_path))
        if "trade_datetime" not in frame.columns and "datetime" in frame.columns:
            frame = frame.rename(columns={"datetime": "trade_datetime"})
        if "amount" not in frame.columns and "total_turnover" in frame.columns:
            frame = frame.rename(columns={"total_turnover": "amount"})
        if not columns and not frame.empty:
            columns = frame.columns.tolist()
        elif not columns and len(frame.columns) > 0:
            columns = frame.columns.tolist()

        rows += int(len(frame))
        if frame.empty:
            continue

        if "trade_datetime" in frame.columns:
            trade_datetime = pd.to_datetime(frame["trade_datetime"], errors="coerce")
        else:
            trade_datetime = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
        valid_trade_datetime = trade_datetime.dropna()
        if not valid_trade_datetime.empty:
            candidate_min = valid_trade_datetime.min().strftime("%Y%m%d")
            candidate_max = valid_trade_datetime.max().strftime("%Y%m%d")
            min_trade_date = candidate_min if min_trade_date is None else min(min_trade_date, candidate_min)
            max_trade_date = candidate_max if max_trade_date is None else max(max_trade_date, candidate_max)

        symbols = _resolve_intraday_symbol_series(frame).map(
            lambda value: normalize_symbol_for_market(value, market="hk")
        )
        symbol_set.update(symbol for symbol in symbols.tolist() if symbol)

    return {
        "rows": rows,
        "columns": columns,
        "min_trade_date": min_trade_date,
        "max_trade_date": max_trade_date,
        "symbols_downloaded": len(symbol_set),
    }


def _resolve_fields(columns: list[str], meta_payload: dict[str, object]) -> list[str]:
    preferred = list(DEFAULT_INTRADAY_VALUE_FIELDS)
    actual_columns = [
        _normalize_intraday_field_name(column)
        for column in columns
        if _normalize_intraday_field_name(column)
    ]
    excluded = {"trade_datetime", "datetime", "symbol", "ts_code", "rq_order_book_id", "order_book_id"}
    discovered = [column for column in actual_columns if column not in excluded]
    if not discovered:
        raw_fields = meta_payload.get("fields")
        if isinstance(raw_fields, list):
            discovered = [
                _normalize_intraday_field_name(value)
                for value in raw_fields
                if _normalize_intraday_field_name(value)
            ]
    ordered = [field for field in preferred if field in discovered]
    ordered.extend(field for field in discovered if field not in ordered)
    return ordered


def _copy_intraday_entry(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)


def build_hk_intraday_asset(args) -> int:
    input_specs = list(getattr(args, "input", None) or [])
    if not input_specs:
        raise SystemExit("Provide at least one --input.")

    groups = resolve_intraday_input_groups(input_specs)
    stem_counts = Counter(group.stem for group in groups)
    duplicate_stems = sorted(stem for stem, count in stem_counts.items() if count > 1)
    if duplicate_stems:
        raise SystemExit(
            "Refusing to build an intraday asset with duplicate file stems: "
            + ", ".join(duplicate_stems)
        )

    started_at = _timestamp_now()
    entry_payloads: list[dict[str, object]] = []
    union_columns: list[str] = []
    union_fields: list[str] = []
    min_trade_date: str | None = None
    max_trade_date: str | None = None
    frequencies: list[str] = []
    adjust_types: list[str] = []

    for group in groups:
        meta_payload = _load_json(group.meta_path)
        read_paths = []
        if group.parts_dir is not None:
            part_files = sorted(group.parts_dir.glob("batch_*.parquet"))
            if not part_files:
                part_files = sorted(group.parts_dir.glob("*.parquet"))
            read_paths.extend(part_files)
        elif group.parquet_path is not None:
            read_paths.append(group.parquet_path)
        if not read_paths and group.parquet_path is not None:
            read_paths.append(group.parquet_path)
        if not read_paths:
            raise SystemExit(f"No parquet files found for intraday input: {group.parent_dir / group.stem}")

        scanned = _scan_intraday_paths(read_paths) if not meta_payload else {}
        columns = [
            str(column).strip()
            for column in (
                meta_payload.get("columns")
                if isinstance(meta_payload.get("columns"), list)
                else scanned.get("columns") or []
            )
            if str(column).strip()
        ]
        fields = _resolve_fields(columns, meta_payload)
        rows = int(meta_payload.get("rows") or scanned.get("rows") or 0)
        symbols_downloaded = meta_payload.get("symbols_downloaded")
        if symbols_downloaded is None:
            symbols_downloaded = meta_payload.get("symbols")
        if symbols_downloaded is None:
            symbols_downloaded = scanned.get("symbols_downloaded")
        try:
            symbols_downloaded = int(symbols_downloaded) if symbols_downloaded is not None else None
        except (TypeError, ValueError):
            symbols_downloaded = None

        symbols_requested = meta_payload.get("symbols_requested")
        try:
            symbols_requested = int(symbols_requested) if symbols_requested is not None else None
        except (TypeError, ValueError):
            symbols_requested = None

        if "min_trade_date" in meta_payload or "max_trade_date" in meta_payload:
            entry_min_trade_date = _normalize_optional_date(
                meta_payload.get("min_trade_date") or scanned.get("min_trade_date")
            )
            entry_max_trade_date = _normalize_optional_date(
                meta_payload.get("max_trade_date") or scanned.get("max_trade_date")
            )
        else:
            entry_min_trade_date = _normalize_optional_date(
                scanned.get("min_trade_date") or meta_payload.get("start_date")
            )
            entry_max_trade_date = _normalize_optional_date(
                scanned.get("max_trade_date") or meta_payload.get("end_date")
            )
        if entry_min_trade_date:
            min_trade_date = (
                entry_min_trade_date
                if min_trade_date is None
                else min(min_trade_date, entry_min_trade_date)
            )
        if entry_max_trade_date:
            max_trade_date = (
                entry_max_trade_date
                if max_trade_date is None
                else max(max_trade_date, entry_max_trade_date)
            )

        frequency = str(meta_payload.get("frequency") or "").strip()
        if frequency and frequency not in frequencies:
            frequencies.append(frequency)
        adjust_type_raw = meta_payload.get("adjust_type")
        adjust_type = str(adjust_type_raw).strip() if adjust_type_raw not in {None, ""} else ""
        if adjust_type and adjust_type not in adjust_types:
            adjust_types.append(adjust_type)

        for column in columns:
            if column not in union_columns:
                union_columns.append(column)
        for field in fields:
            if field not in union_fields:
                union_fields.append(field)

        parts_files = _directory_file_count(group.parts_dir) if group.parts_dir is not None else 0
        total_bytes = 0
        if group.parquet_path is not None and group.parquet_path.exists():
            total_bytes += group.parquet_path.stat().st_size
        if group.meta_path is not None and group.meta_path.exists():
            total_bytes += group.meta_path.stat().st_size
        if group.parts_dir is not None and group.parts_dir.exists():
            total_bytes += _directory_size_bytes(group.parts_dir)

        entry_payloads.append(
            {
                "name": group.stem,
                "source_parquet": str(group.parquet_path) if group.parquet_path is not None else None,
                "source_meta": str(group.meta_path) if group.meta_path is not None else None,
                "source_parts_dir": str(group.parts_dir) if group.parts_dir is not None else None,
                "path": None,
                "meta_path": None,
                "parts_dir": None,
                "rows": rows,
                "total_bytes": total_bytes,
                "min_trade_date": entry_min_trade_date,
                "max_trade_date": entry_max_trade_date,
                "symbols_requested": symbols_requested,
                "symbols_downloaded": symbols_downloaded,
                "frequency": frequency or None,
                "adjust_type": adjust_type or None,
                "columns": columns,
                "fields": fields,
                "parts_files": parts_files,
                "meta_mtime": _path_mtime_iso(group.meta_path) if group.meta_path is not None else None,
                "parquet_mtime": _path_mtime_iso(group.parquet_path) if group.parquet_path is not None else None,
                "quota_bytes_used_delta": meta_payload.get("bytes_used_delta"),
                "source_symbols_file": meta_payload.get("symbols_file"),
                "source_dataset": meta_payload.get("dataset"),
            }
        )

    if min_trade_date is None or max_trade_date is None:
        raise SystemExit("Could not determine the overall intraday date range from the provided inputs.")

    out_root = getattr(args, "out_root", None)
    output_dir = _prepare_daily_output_dir(
        out_root=str(out_root),
        dataset_name="intraday",
        start_date=min_trade_date,
        end_date=max_trade_date,
        name=getattr(args, "name", None),
        resume=False,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    for group, entry in zip(groups, entry_payloads, strict=True):
        if group.parquet_path is not None and group.parquet_path.exists():
            dest_path = data_dir / group.parquet_path.name
            _copy_intraday_entry(group.parquet_path, dest_path)
            entry["path"] = str(dest_path)
        if group.meta_path is not None and group.meta_path.exists():
            dest_meta_path = data_dir / group.meta_path.name
            _copy_intraday_entry(group.meta_path, dest_meta_path)
            entry["meta_path"] = str(dest_meta_path)
        if group.parts_dir is not None and group.parts_dir.exists():
            dest_parts_dir = data_dir / group.parts_dir.name
            _copy_intraday_entry(group.parts_dir, dest_parts_dir)
            entry["parts_dir"] = str(dest_parts_dir)

    inputs_file = output_dir / "inputs.txt"
    fields_file = output_dir / "fields.txt"
    _write_text_list(
        inputs_file,
        [
            str(entry["source_parquet"] or entry["source_parts_dir"])
            for entry in entry_payloads
            if entry["source_parquet"] or entry["source_parts_dir"]
        ],
    )
    _write_text_list(fields_file, union_fields)

    finished_at = _timestamp_now()
    manifest = {
        "name": output_dir.name,
        "created_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": "completed",
        "dataset": "intraday",
        "api": "local_intraday_cache",
        "market": "hk",
        "repo_root": str(Path.cwd().resolve()),
        "output_dir": str(output_dir),
        "source_kind": "packaged_from_local_intraday_cache",
        "query": {
            "start_date": min_trade_date,
            "end_date": max_trade_date,
            "frequency": frequencies[0] if len(frequencies) == 1 else None,
            "frequencies": frequencies,
            "adjust_type": adjust_types[0] if len(adjust_types) == 1 else None,
            "adjust_types": adjust_types,
            "inputs_count": len(entry_payloads),
            "fields_count": len(union_fields),
            "fields": union_fields,
        },
        "inputs_file": str(inputs_file),
        "fields_file": str(fields_file),
        "columns": union_columns,
        "entries": entry_payloads,
        "totals": {
            "inputs": len(entry_payloads),
            "rows": int(sum(int(entry["rows"] or 0) for entry in entry_payloads)),
            "bytes": int(sum(int(entry["total_bytes"] or 0) for entry in entry_payloads)),
            "parquet_files": sum(1 for entry in entry_payloads if entry["path"]),
            "meta_files": sum(1 for entry in entry_payloads if entry["meta_path"]),
            "part_directories": sum(1 for entry in entry_payloads if entry["parts_dir"]),
            "part_files": int(sum(int(entry["parts_files"] or 0) for entry in entry_payloads)),
        },
        "git": _git_metadata(Path.cwd().resolve()),
    }
    _write_manifest(output_dir / "manifest.yml", manifest)

    alias_raw = getattr(args, "alias", None)
    if alias_raw:
        alias_path = _resolve_alias_path(alias_raw)
        _create_relative_symlink(output_dir, alias_path)

    print(f"Saved HK intraday asset: {output_dir}")
    return 0


__all__ = ["build_hk_intraday_asset"]
