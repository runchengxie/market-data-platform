"""Symbol and date parsing helpers."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

SYMBOL_FILE_COLUMNS: tuple[str, ...] = ("order_book_id", "symbol", "stock_ticker", "ts_code")


def _append_unique(items: list[str], value: str) -> None:
    normalized = normalize_hk_order_book_id(value)
    if normalized and normalized not in items:
        items.append(normalized)


def normalize_hk_order_book_id(value: object) -> str:
    """Normalize HK symbol aliases into RQData order_book_id form."""
    text = str(value or "").strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return ""
    if text.endswith((".XSHG", ".XSHE", ".SH", ".SZ")):
        raise ValueError(f"Unsupported non-HK symbol {value!r}.")
    if text.endswith(".XHKG"):
        code = text.removesuffix(".XHKG")
    elif text.endswith(".HK"):
        code = text.removesuffix(".HK")
    elif "." in text:
        raise ValueError(f"Unsupported HK symbol format {value!r}.")
    else:
        code = text
    if code.isdigit():
        code = code.zfill(5)
    return f"{code}.XHKG"


def _read_table_symbol_values(path: Path) -> Sequence[object]:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path)
    for column in SYMBOL_FILE_COLUMNS:
        if column in frame.columns:
            return frame[column].tolist()
    raise ValueError(
        "Unsupported symbol file schema: expected one of "
        f"{', '.join(SYMBOL_FILE_COLUMNS)}."
    )


def _read_symbol_file_values(path: Path) -> Sequence[object]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".parquet"}:
        return _read_table_symbol_values(path)
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        values.extend(stripped.replace(",", " ").split())
    return values


def parse_symbols(symbols: str | None = None, symbols_file: str | Path | None = None) -> list[str]:
    """Parse HK symbols from CLI text and/or a TXT/CSV/Parquet file."""
    parsed: list[str] = []
    if symbols:
        for chunk in symbols.replace(",", " ").split():
            _append_unique(parsed, chunk)
    if symbols_file:
        path = Path(symbols_file)
        for value in _read_symbol_file_values(path):
            _append_unique(parsed, str(value))
    if not parsed:
        raise ValueError("At least one symbol is required.")
    return parsed


def parse_date(value: str | date | datetime) -> date:
    """Parse YYYYMMDD or ISO date strings."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {value!r}; expected YYYYMMDD or YYYY-MM-DD.")


def format_date(value: str | date | datetime) -> str:
    """Format a date as YYYYMMDD."""
    return parse_date(value).strftime("%Y%m%d")


def iter_dates(start_date: str | date | datetime, end_date: str | date | datetime) -> Iterator[str]:
    """Yield calendar dates in deterministic inclusive order as YYYYMMDD."""
    start = parse_date(start_date)
    end = parse_date(end_date)
    if start > end:
        raise ValueError("start_date must be on or before end_date.")
    current = start
    while current <= end:
        yield current.strftime("%Y%m%d")
        current += timedelta(days=1)
