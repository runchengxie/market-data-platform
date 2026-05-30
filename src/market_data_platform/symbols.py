from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

SYMBOL_COL = "symbol"
LEGACY_SYMBOL_COLUMNS = ("ts_code", "stock_ticker")
SYMBOL_INPUT_COLUMNS = ("symbol", "ts_code", "stock_ticker", "order_book_id")
DEFAULT_SYMBOL_PRIORITY = ("symbol", "ts_code", "stock_ticker", "order_book_id")
PROVIDER_SYMBOL_PRIORITY = ("ts_code", "stock_ticker", "order_book_id", "symbol")


def normalize_symbol_for_market(value: object, *, market: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    market_text = str(market or "").strip().lower()
    upper = text.upper()
    if market_text == "a_share":
        if upper.endswith(".XSHG"):
            return f"{upper[:-5].zfill(6)}.SH"
        if upper.endswith(".XSHE"):
            return f"{upper[:-5].zfill(6)}.SZ"
        if upper.endswith(".SH"):
            return f"{upper[:-3].zfill(6)}.SH"
        if upper.endswith(".SZ"):
            return f"{upper[:-3].zfill(6)}.SZ"
        if upper.isdigit():
            code = upper.zfill(6)
            if code.startswith(("5", "6", "9")):
                return f"{code}.SH"
            if code.startswith(("0", "2", "3")):
                return f"{code}.SZ"
        return upper
    if market_text != "hk":
        return text

    if upper.endswith(".XHKG"):
        upper = upper[:-5]
    if upper.endswith(".HK"):
        upper = upper[:-3]
    if upper.isdigit():
        return f"{upper.zfill(5)}.HK"
    return upper


def _column_series(df: pd.DataFrame, column: str) -> pd.Series:
    values = df.loc[:, column]
    if isinstance(values, pd.DataFrame):
        values = values.iloc[:, 0]
    return values


def _clean_symbol_series(values: pd.Series) -> pd.Series:
    series = values if isinstance(values, pd.Series) else pd.Series([values])
    return series.where(series.notna(), "").astype(str).str.strip()


def normalize_symbol_standard_name(name: object) -> str:
    text = str(name or "").strip()
    if text in {SYMBOL_COL, *LEGACY_SYMBOL_COLUMNS, "order_book_id"}:
        return SYMBOL_COL
    return text


def resolve_symbol_series(
    df: pd.DataFrame,
    *,
    context: str,
    priority: Sequence[str] = DEFAULT_SYMBOL_PRIORITY,
) -> pd.Series:
    present_columns = [column for column in priority if column in df.columns]
    if not present_columns:
        raise SystemExit(f"{context} is missing symbol/stock_ticker/ts_code/order_book_id.")

    merged = _clean_symbol_series(_column_series(df, present_columns[0]))
    for column in present_columns[1:]:
        series = _clean_symbol_series(_column_series(df, column))
        merged = merged.where(merged != "", series)
    return merged


def ensure_symbol_columns(
    df: pd.DataFrame,
    *,
    context: str,
    priority: Sequence[str] = DEFAULT_SYMBOL_PRIORITY,
) -> pd.DataFrame:
    normalized = df.copy()
    merged = resolve_symbol_series(normalized, context=context, priority=priority)

    normalized[SYMBOL_COL] = merged
    return normalized


def drop_legacy_symbol_columns(
    df: pd.DataFrame,
    *,
    drop_order_book_id: bool = False,
) -> pd.DataFrame:
    drop_columns = [*LEGACY_SYMBOL_COLUMNS]
    if drop_order_book_id:
        drop_columns.append("order_book_id")
    out = df.drop(columns=drop_columns, errors="ignore")
    out.attrs = dict(getattr(df, "attrs", {}))
    return out


def canonicalize_symbol_columns(
    df: pd.DataFrame,
    *,
    context: str,
    priority: Sequence[str] = DEFAULT_SYMBOL_PRIORITY,
    drop_order_book_id: bool = False,
) -> pd.DataFrame:
    normalized = ensure_symbol_columns(df, context=context, priority=priority)
    return drop_legacy_symbol_columns(
        normalized,
        drop_order_book_id=drop_order_book_id,
    )
