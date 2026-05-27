from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol
from market_data_platform.symbols import canonicalize_symbol_columns
from .models import (
    DailyMirrorAuditRecord,
    DailyMirrorEntry,
    DatedMirrorAuditRecord,
    DatedMirrorEntry,
    MirrorAuditRecord,
    MirrorEntry,
)
from .shared import (
    DATE_TEXT_OUTPUT_COLUMNS,
    _drop_conflicting_index_levels,
    _normalize_frame_columns,
    _normalize_hk_symbol,
)

LEGACY_STORAGE_SYMBOL_COLUMNS = ("ts_code", "stock_ticker")


def _chunked(values: Sequence[str], size: int) -> Iterable[list[str]]:
    if size <= 0:
        raise SystemExit("--batch-size must be > 0.")
    for idx in range(0, len(values), size):
        yield list(values[idx : idx + size])


def _reset_frame_index(frame: pd.DataFrame | pd.Series | None) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if isinstance(frame, pd.Series):
        frame = frame.to_frame(name=str(frame.name or "value"))
    if frame.empty and len(frame.columns) == 0:
        return frame.copy()
    normalized = _normalize_frame_columns(frame)
    normalized = _drop_conflicting_index_levels(normalized)
    if isinstance(normalized.index, pd.MultiIndex):
        has_named_levels = any(name is not None for name in normalized.index.names)
    else:
        has_named_levels = normalized.index.name is not None
    if "order_book_id" in normalized.columns and not has_named_levels:
        return normalized
    if not has_named_levels:
        return normalized
    reset = _normalize_frame_columns(normalized.reset_index())
    if "order_book_id" not in reset.columns and "index" in reset.columns:
        reset = reset.rename(columns={"index": "order_book_id"})
    return reset


def _prepare_asset_frame(frame: pd.DataFrame | pd.Series | None, *, symbol_map: Mapping[str, str]) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if isinstance(frame, pd.Series):
        frame = frame.to_frame(name=str(frame.name or "value"))
    if frame.empty and len(frame.columns) == 0:
        return frame.copy()

    normalized = _reset_frame_index(frame)
    if normalized.empty and "order_book_id" not in normalized.columns:
        return normalized
    if "order_book_id" not in normalized.columns:
        if len(symbol_map) == 1:
            normalized["order_book_id"] = next(iter(symbol_map.keys()))
        else:
            raise ValueError("RQData payload is missing order_book_id.")
    normalized["order_book_id"] = normalized["order_book_id"].astype(str).str.strip()
    normalized["symbol"] = normalized["order_book_id"].map(symbol_map)
    missing_mask = normalized["symbol"].isna()
    if missing_mask.any():
        normalized.loc[missing_mask, "symbol"] = normalized.loc[missing_mask, "order_book_id"].map(
            _normalize_hk_symbol
        )
    if "quarter" in normalized.columns:
        normalized["quarter"] = normalized["quarter"].astype(str)

    sort_cols = [
        col
        for col in ["symbol", "quarter", "info_date", "rice_create_tm", "field", "subject"]
        if col in normalized.columns
    ]
    if sort_cols:
        normalized = normalized.sort_values(sort_cols).reset_index(drop=True)
    return normalized


def _ensure_requested_fields(frame: pd.DataFrame, fields: Sequence[str]) -> pd.DataFrame:
    requested_fields = _field_columns_for_audit(fields)
    missing_fields = [field for field in requested_fields if field not in frame.columns]
    if not missing_fields:
        return frame.copy()
    extras = pd.DataFrame({field: pd.Series(pd.NA, index=frame.index) for field in missing_fields})
    return pd.concat([frame.copy(), extras], axis=1)


def _series_bounds_as_date(frame: pd.DataFrame, column: str) -> tuple[str | None, str | None]:
    if column not in frame.columns:
        return None, None
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    if values.empty:
        return None, None
    return values.min().strftime("%Y-%m-%d"), values.max().strftime("%Y-%m-%d")


def _series_bounds_as_text(frame: pd.DataFrame, column: str) -> tuple[str | None, str | None]:
    if column not in frame.columns:
        return None, None
    values = frame[column].dropna().astype(str)
    if values.empty:
        return None, None
    return values.min(), values.max()


def _field_columns_for_audit(fields: Sequence[str]) -> list[str]:
    return [str(field) for field in fields if str(field).strip()]


def _canonicalize_output_columns(columns: Sequence[str], *, preferred: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for column in columns:
        name = str(column).strip()
        if not name:
            continue
        if name in LEGACY_STORAGE_SYMBOL_COLUMNS:
            name = "symbol"
        if name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    preferred_columns = [column for column in preferred if column in seen]
    remaining = [column for column in normalized if column not in preferred_columns]
    return preferred_columns + remaining


def _normalize_storage_symbol_column(frame: pd.DataFrame) -> pd.DataFrame:
    if "symbol" not in frame.columns:
        return frame.copy()
    normalized = frame.copy()
    normalized["symbol"] = normalized["symbol"].map(_normalize_hk_symbol)
    return normalized


def _canonicalize_symbol_frame_for_storage(
    symbol_frame: pd.DataFrame,
    *,
    context: str,
    preferred: Sequence[str],
) -> pd.DataFrame:
    normalized = canonicalize_symbol_columns(symbol_frame, context=context)
    normalized = _normalize_storage_symbol_column(normalized)
    columns = _canonicalize_output_columns(normalized.columns.tolist(), preferred=preferred)
    if not columns:
        return normalized.copy()
    return normalized.loc[:, columns].copy()


def _entry_from_symbol_frame(out_path: Path, symbol_frame: pd.DataFrame) -> MirrorEntry:
    symbol = str(symbol_frame["symbol"].iloc[0])
    order_book_id = (
        str(symbol_frame["order_book_id"].iloc[0])
        if "order_book_id" in symbol_frame.columns
        else _to_rqdata_symbol("hk", symbol)
    )
    min_quarter, max_quarter = _series_bounds_as_text(symbol_frame, "quarter")
    min_info_date, max_info_date = _series_bounds_as_date(symbol_frame, "info_date")
    return MirrorEntry(
        symbol=symbol,
        order_book_id=order_book_id,
        path=out_path,
        rows=int(len(symbol_frame)),
        total_bytes=int(out_path.stat().st_size),
        min_quarter=min_quarter,
        max_quarter=max_quarter,
        min_info_date=min_info_date,
        max_info_date=max_info_date,
    )


def _write_symbol_frame(data_dir: Path, symbol_frame: pd.DataFrame) -> MirrorEntry:
    storage_frame = _canonicalize_symbol_frame_for_storage(
        symbol_frame,
        context="Mirror asset output",
        preferred=("symbol", "order_book_id"),
    )
    symbol = str(storage_frame["symbol"].iloc[0])
    out_path = data_dir / f"{symbol}.parquet"
    storage_frame.to_parquet(out_path, index=False)
    return _entry_from_symbol_frame(out_path, storage_frame)


def _load_symbol_frame(path: Path, *, fields: Sequence[str]) -> pd.DataFrame:
    columns = ["symbol", "ts_code", "order_book_id", "quarter", "info_date", *_field_columns_for_audit(fields)]
    requested: list[str] = []
    seen: set[str] = set()
    for column in columns:
        if column and column not in seen:
            requested.append(column)
            seen.add(column)
    try:
        frame = pd.read_parquet(path, columns=requested)
    except Exception:
        frame = pd.read_parquet(path)
    normalized = _normalize_frame_columns(frame)
    if normalized.empty and len(normalized.columns) == 0:
        return normalized
    normalized = canonicalize_symbol_columns(normalized, context=f"Mirror asset file {path.name}")
    normalized = _normalize_storage_symbol_column(normalized)
    return normalized


def _load_existing_entry(path: Path, *, fields: Sequence[str]) -> tuple[MirrorEntry, pd.DataFrame]:
    frame = _ensure_requested_fields(_load_symbol_frame(path, fields=fields), fields)
    if frame.empty:
        symbol = path.stem
        order_book_id = _to_rqdata_symbol("hk", symbol)
        entry = MirrorEntry(
            symbol=symbol,
            order_book_id=order_book_id,
            path=path,
            rows=0,
            total_bytes=int(path.stat().st_size),
            min_quarter=None,
            max_quarter=None,
            min_info_date=None,
            max_info_date=None,
        )
        return entry, frame
    return _entry_from_symbol_frame(path, frame), frame


def _field_coverage_template(fields: Sequence[str]) -> dict[str, dict[str, int | str]]:
    return {
        field: {"field": field, "nonnull_rows": 0, "symbols_with_values": 0}
        for field in _field_columns_for_audit(fields)
    }


def _update_field_coverage(
    coverage: dict[str, dict[str, int | str]],
    frame: pd.DataFrame,
    *,
    fields: Sequence[str],
) -> None:
    for field in _field_columns_for_audit(fields):
        if field not in coverage:
            continue
        if field in frame.columns:
            nonnull_rows = int(frame[field].notna().sum())
            if nonnull_rows == 0 and {"field", "amount"}.issubset(frame.columns):
                mask = frame["field"].astype(str) == str(field)
                mask = mask & frame["amount"].notna()
                nonnull_rows = int(mask.sum())
        elif {"field", "amount"}.issubset(frame.columns):
            mask = frame["field"].astype(str) == str(field)
            mask = mask & frame["amount"].notna()
            nonnull_rows = int(mask.sum())
        else:
            continue
        coverage[field]["nonnull_rows"] = int(coverage[field]["nonnull_rows"]) + nonnull_rows
        if nonnull_rows > 0:
            coverage[field]["symbols_with_values"] = int(coverage[field]["symbols_with_values"]) + 1


def _audit_record(
    *,
    symbol: str,
    order_book_id: str,
    status: str,
    attempts: int,
    started_at: str | None,
    finished_at: str | None,
    file_mtime: str | None,
    dropped_fields: Sequence[str] | None = None,
    error: str | None,
    entry: MirrorEntry | None = None,
) -> MirrorAuditRecord:
    return MirrorAuditRecord(
        symbol=symbol,
        order_book_id=order_book_id,
        status=status,
        attempts=attempts,
        rows=entry.rows if entry else 0,
        total_bytes=entry.total_bytes if entry else 0,
        min_quarter=entry.min_quarter if entry else None,
        max_quarter=entry.max_quarter if entry else None,
        min_info_date=entry.min_info_date if entry else None,
        max_info_date=entry.max_info_date if entry else None,
        started_at=started_at,
        finished_at=finished_at,
        file_mtime=file_mtime,
        dropped_fields=",".join(str(item) for item in (dropped_fields or []) if str(item).strip()) or None,
        error=error,
    )


def _write_audit_csv(path: Path, records: Sequence[MirrorAuditRecord]) -> None:
    rows = [
        {
            "symbol": item.symbol,
            "order_book_id": item.order_book_id,
            "status": item.status,
            "attempts": item.attempts,
            "rows": item.rows,
            "total_bytes": item.total_bytes,
            "min_quarter": item.min_quarter,
            "max_quarter": item.max_quarter,
            "min_info_date": item.min_info_date,
            "max_info_date": item.max_info_date,
            "started_at": item.started_at,
            "finished_at": item.finished_at,
            "file_mtime": item.file_mtime,
            "dropped_fields": item.dropped_fields,
            "error": item.error,
        }
        for item in records
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def _prepare_daily_asset_frame(
    frame: pd.DataFrame | pd.Series | None,
    *,
    symbol: str,
    order_book_id: str,
) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if isinstance(frame, pd.Series):
        frame = frame.to_frame(name=str(frame.name or "value"))
    if frame.empty and len(frame.columns) == 0:
        return frame.copy()

    normalized = _normalize_frame_columns(frame)
    if "trade_date" not in normalized.columns and "date" in normalized.columns:
        normalized = normalized.rename(columns={"date": "trade_date"})
    if "trade_date" not in normalized.columns:
        reset = _normalize_frame_columns(frame.reset_index())
        if "trade_date" not in reset.columns and "date" in reset.columns:
            reset = reset.rename(columns={"date": "trade_date"})
        elif "trade_date" not in reset.columns and "index" in reset.columns:
            reset = reset.rename(columns={"index": "trade_date"})
        normalized = reset
    if "trade_date" not in normalized.columns:
        raise ValueError("RQData daily payload is missing trade_date.")

    trade_dates = pd.to_datetime(normalized["trade_date"], errors="coerce")
    valid_trade_date = trade_dates.notna()
    work = normalized.loc[valid_trade_date].copy()
    if work.empty:
        return work
    work["trade_date"] = trade_dates.loc[valid_trade_date].dt.strftime("%Y%m%d")
    work["symbol"] = symbol
    work["order_book_id"] = order_book_id
    preferred = ["trade_date", "symbol", "order_book_id"]
    remaining = [column for column in work.columns if column not in preferred]
    work = work.loc[:, preferred + remaining].copy()
    work = work.drop_duplicates(subset=["trade_date"], keep="last")
    work = work.sort_values(["trade_date"]).reset_index(drop=True)
    return work


def _prepare_daily_batch_asset_frame(
    frame: pd.DataFrame | pd.Series | None,
    *,
    symbol_map: Mapping[str, str],
) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if isinstance(frame, pd.Series):
        frame = frame.to_frame(name=str(frame.name or "value"))
    if frame.empty and len(frame.columns) == 0:
        return frame.copy()

    normalized = _reset_frame_index(frame)
    if normalized.empty and "order_book_id" not in normalized.columns:
        return normalized

    rename_map: dict[str, str] = {}
    if "order_book_id" not in normalized.columns:
        if "level_0" in normalized.columns:
            rename_map["level_0"] = "order_book_id"
        elif isinstance(frame.index, pd.MultiIndex):
            normalized = normalized.copy()
            normalized["order_book_id"] = frame.index.get_level_values(0).tolist()
        elif len(symbol_map) == 1:
            normalized = normalized.copy()
            normalized["order_book_id"] = next(iter(symbol_map.keys()))
        else:
            raise ValueError("RQData daily payload is missing order_book_id.")
    if "trade_date" not in normalized.columns:
        for candidate in ("date", "datetime", "level_1", "index"):
            if candidate in normalized.columns:
                rename_map[candidate] = "trade_date"
                break
    if rename_map:
        normalized = normalized.rename(columns=rename_map)
    if "trade_date" not in normalized.columns:
        normalized = normalized.copy()
        if isinstance(frame.index, pd.MultiIndex):
            normalized["trade_date"] = frame.index.get_level_values(-1).tolist()
        elif not isinstance(frame.index, pd.RangeIndex):
            normalized["trade_date"] = frame.index.tolist()
        else:
            raise ValueError("RQData daily payload is missing trade_date.")

    normalized = normalized.copy()
    normalized["order_book_id"] = normalized["order_book_id"].astype(str).str.strip()
    normalized["symbol"] = normalized["order_book_id"].map(symbol_map)
    missing_symbol = normalized["symbol"].isna()
    if missing_symbol.any():
        normalized.loc[missing_symbol, "symbol"] = normalized.loc[missing_symbol, "order_book_id"].map(
            _normalize_hk_symbol
        )

    trade_dates = pd.to_datetime(normalized["trade_date"], errors="coerce")
    valid_trade_date = trade_dates.notna()
    work = normalized.loc[valid_trade_date].copy()
    if work.empty:
        return work
    work["trade_date"] = trade_dates.loc[valid_trade_date].dt.strftime("%Y%m%d")
    preferred = ["trade_date", "symbol", "order_book_id"]
    remaining = [column for column in work.columns if column not in preferred]
    work = work.loc[:, preferred + remaining].copy()
    work = work.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    work = work.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return work


def _daily_entry_from_symbol_frame(out_path: Path, symbol_frame: pd.DataFrame) -> DailyMirrorEntry:
    symbol = str(symbol_frame["symbol"].iloc[0])
    order_book_id = str(symbol_frame["order_book_id"].iloc[0])
    min_trade_date, max_trade_date = _series_bounds_as_text(symbol_frame, "trade_date")
    return DailyMirrorEntry(
        symbol=symbol,
        order_book_id=order_book_id,
        path=out_path,
        rows=int(len(symbol_frame)),
        total_bytes=int(out_path.stat().st_size),
        min_trade_date=min_trade_date,
        max_trade_date=max_trade_date,
    )


def _write_daily_symbol_frame(data_dir: Path, symbol_frame: pd.DataFrame) -> DailyMirrorEntry:
    storage_frame = _canonicalize_symbol_frame_for_storage(
        symbol_frame,
        context="Daily mirror asset output",
        preferred=("trade_date", "symbol", "order_book_id"),
    )
    symbol = str(storage_frame["symbol"].iloc[0])
    out_path = data_dir / f"{symbol}.parquet"
    storage_frame.to_parquet(out_path, index=False)
    return _daily_entry_from_symbol_frame(out_path, storage_frame)


def _load_daily_symbol_frame(path: Path, *, fields: Sequence[str]) -> pd.DataFrame:
    columns = ["trade_date", "symbol", "ts_code", "order_book_id", *_field_columns_for_audit(fields)]
    requested: list[str] = []
    seen: set[str] = set()
    for column in columns:
        if column and column not in seen:
            requested.append(column)
            seen.add(column)
    try:
        frame = pd.read_parquet(path, columns=requested)
    except Exception:
        frame = pd.read_parquet(path)
    normalized = _normalize_frame_columns(frame)
    if normalized.empty and len(normalized.columns) == 0:
        return normalized
    normalized = canonicalize_symbol_columns(normalized, context=f"Daily mirror asset file {path.name}")
    normalized = _normalize_storage_symbol_column(normalized)
    return normalized


def _load_existing_daily_entry(path: Path, *, fields: Sequence[str]) -> tuple[DailyMirrorEntry, pd.DataFrame]:
    frame = _ensure_requested_fields(_load_daily_symbol_frame(path, fields=fields), fields)
    if frame.empty:
        symbol = path.stem
        order_book_id = _to_rqdata_symbol("hk", symbol)
        entry = DailyMirrorEntry(
            symbol=symbol,
            order_book_id=order_book_id,
            path=path,
            rows=0,
            total_bytes=int(path.stat().st_size),
            min_trade_date=None,
            max_trade_date=None,
        )
        return entry, frame
    return _daily_entry_from_symbol_frame(path, frame), frame


def _daily_audit_record(
    *,
    symbol: str,
    order_book_id: str,
    status: str,
    attempts: int,
    started_at: str | None,
    finished_at: str | None,
    file_mtime: str | None,
    error: str | None,
    entry: DailyMirrorEntry | None = None,
) -> DailyMirrorAuditRecord:
    return DailyMirrorAuditRecord(
        symbol=symbol,
        order_book_id=order_book_id,
        status=status,
        attempts=attempts,
        rows=entry.rows if entry else 0,
        total_bytes=entry.total_bytes if entry else 0,
        min_trade_date=entry.min_trade_date if entry else None,
        max_trade_date=entry.max_trade_date if entry else None,
        started_at=started_at,
        finished_at=finished_at,
        file_mtime=file_mtime,
        error=error,
    )


def _write_daily_audit_csv(path: Path, records: Sequence[DailyMirrorAuditRecord]) -> None:
    rows = [
        {
            "symbol": item.symbol,
            "order_book_id": item.order_book_id,
            "status": item.status,
            "attempts": item.attempts,
            "rows": item.rows,
            "total_bytes": item.total_bytes,
            "min_trade_date": item.min_trade_date,
            "max_trade_date": item.max_trade_date,
            "started_at": item.started_at,
            "finished_at": item.finished_at,
            "file_mtime": item.file_mtime,
            "error": item.error,
        }
        for item in records
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def _prepare_dated_asset_frame(
    frame: pd.DataFrame | pd.Series | None,
    *,
    symbol_map: Mapping[str, str],
    date_column: str,
    sort_columns: Sequence[str] = (),
) -> pd.DataFrame:
    normalized = _prepare_asset_frame(frame, symbol_map=symbol_map)
    if normalized.empty:
        return normalized
    if date_column not in normalized.columns:
        raise ValueError(f"RQData payload is missing {date_column}.")
    parsed_dates = pd.to_datetime(normalized[date_column], errors="coerce")
    valid_dates = parsed_dates.notna()
    work = normalized.loc[valid_dates].copy()
    if work.empty:
        return work
    if date_column in DATE_TEXT_OUTPUT_COLUMNS:
        work[date_column] = parsed_dates.loc[valid_dates].dt.strftime("%Y%m%d")
    else:
        work[date_column] = parsed_dates.loc[valid_dates]

    preferred = [column for column in ["symbol", "order_book_id", date_column] if column in work.columns]
    remaining = [column for column in work.columns if column not in preferred]
    work = work.loc[:, preferred + remaining].copy()
    ordered_sort_cols = [column for column in ["symbol", date_column, *sort_columns] if column in work.columns]
    if ordered_sort_cols:
        work = work.sort_values(ordered_sort_cols).reset_index(drop=True)
    return work


def _dated_entry_from_symbol_frame(
    out_path: Path,
    symbol_frame: pd.DataFrame,
    *,
    date_column: str,
) -> DatedMirrorEntry:
    symbol = str(symbol_frame["symbol"].iloc[0])
    order_book_id = (
        str(symbol_frame["order_book_id"].iloc[0])
        if "order_book_id" in symbol_frame.columns
        else _to_rqdata_symbol("hk", symbol)
    )
    min_date, max_date = _series_bounds_as_date(symbol_frame, date_column)
    return DatedMirrorEntry(
        symbol=symbol,
        order_book_id=order_book_id,
        path=out_path,
        rows=int(len(symbol_frame)),
        total_bytes=int(out_path.stat().st_size),
        min_date=min_date,
        max_date=max_date,
    )


def _write_dated_symbol_frame(
    data_dir: Path,
    symbol_frame: pd.DataFrame,
    *,
    date_column: str,
) -> DatedMirrorEntry:
    storage_frame = _canonicalize_symbol_frame_for_storage(
        symbol_frame,
        context=f"Dated mirror asset output ({date_column})",
        preferred=("symbol", "order_book_id", date_column),
    )
    symbol = str(storage_frame["symbol"].iloc[0])
    out_path = data_dir / f"{symbol}.parquet"
    storage_frame.to_parquet(out_path, index=False)
    return _dated_entry_from_symbol_frame(out_path, storage_frame, date_column=date_column)


def _load_dated_symbol_frame(path: Path, *, date_column: str, fields: Sequence[str]) -> pd.DataFrame:
    columns = ["symbol", "ts_code", "order_book_id", date_column, *_field_columns_for_audit(fields)]
    requested: list[str] = []
    seen: set[str] = set()
    for column in columns:
        if column and column not in seen:
            requested.append(column)
            seen.add(column)
    try:
        frame = pd.read_parquet(path, columns=requested)
    except Exception:
        frame = pd.read_parquet(path)
    normalized = _normalize_frame_columns(frame)
    if normalized.empty and len(normalized.columns) == 0:
        return normalized
    normalized = canonicalize_symbol_columns(normalized, context=f"Dated mirror asset file {path.name}")
    normalized = _normalize_storage_symbol_column(normalized)
    return normalized


def _load_existing_dated_entry(
    path: Path,
    *,
    date_column: str,
    fields: Sequence[str],
) -> tuple[DatedMirrorEntry, pd.DataFrame]:
    frame = _ensure_requested_fields(_load_dated_symbol_frame(path, date_column=date_column, fields=fields), fields)
    if frame.empty:
        symbol = path.stem
        order_book_id = _to_rqdata_symbol("hk", symbol)
        entry = DatedMirrorEntry(
            symbol=symbol,
            order_book_id=order_book_id,
            path=path,
            rows=0,
            total_bytes=int(path.stat().st_size),
            min_date=None,
            max_date=None,
        )
        return entry, frame
    return _dated_entry_from_symbol_frame(path, frame, date_column=date_column), frame


def _dated_audit_record(
    *,
    symbol: str,
    order_book_id: str,
    status: str,
    attempts: int,
    started_at: str | None,
    finished_at: str | None,
    file_mtime: str | None,
    dropped_fields: Sequence[str] | None = None,
    error: str | None,
    entry: DatedMirrorEntry | None = None,
) -> DatedMirrorAuditRecord:
    return DatedMirrorAuditRecord(
        symbol=symbol,
        order_book_id=order_book_id,
        status=status,
        attempts=attempts,
        rows=entry.rows if entry else 0,
        total_bytes=entry.total_bytes if entry else 0,
        min_date=entry.min_date if entry else None,
        max_date=entry.max_date if entry else None,
        started_at=started_at,
        finished_at=finished_at,
        file_mtime=file_mtime,
        dropped_fields=",".join(str(item) for item in (dropped_fields or []) if str(item).strip()) or None,
        error=error,
    )


def _write_dated_audit_csv(path: Path, records: Sequence[DatedMirrorAuditRecord]) -> None:
    rows = [
        {
            "symbol": item.symbol,
            "order_book_id": item.order_book_id,
            "status": item.status,
            "attempts": item.attempts,
            "rows": item.rows,
            "total_bytes": item.total_bytes,
            "min_date": item.min_date,
            "max_date": item.max_date,
            "started_at": item.started_at,
            "finished_at": item.finished_at,
            "file_mtime": item.file_mtime,
            "dropped_fields": item.dropped_fields,
            "error": item.error,
        }
        for item in records
    ]
    pd.DataFrame(rows).to_csv(path, index=False)
