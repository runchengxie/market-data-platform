from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from market_data_platform.rebalance import get_rebalance_dates
from .asset_io import _reset_frame_index
from .build import _load_universe_by_date_frame
from .shared import (
    DEFAULT_HK_INDUSTRY_CHANGE_LEVEL,
    DEFAULT_HK_INDUSTRY_SOURCE,
    DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL,
    HK_INDUSTRY_HIERARCHY_COLUMNS,
    HK_INSTRUMENT_INDUSTRY_FIELDS,
    _dedupe_preserve_order,
    _normalize_hk_symbol,
    _resolve_path,
)


DEFAULT_HK_SOUTHBOUND_TRADING_TYPES = ("sh", "sz")


def _resolve_hk_industry_source(args) -> str:
    source = str(
        getattr(args, "source", DEFAULT_HK_INDUSTRY_SOURCE) or DEFAULT_HK_INDUSTRY_SOURCE
    ).strip()
    if not source:
        raise SystemExit("--source must not be empty.")
    return source


def _resolve_hk_instrument_industry_level(args) -> tuple[int, list[str]]:
    raw_level = str(
        getattr(args, "level", DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL)
        if getattr(args, "level", None) is not None
        else DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL
    ).strip()
    try:
        level = int(raw_level)
    except ValueError as exc:
        raise SystemExit(
            "--level for mirror-hk-instrument-industry must be one of 0/1/2/3."
        ) from exc
    if level not in HK_INSTRUMENT_INDUSTRY_FIELDS:
        raise SystemExit("--level for mirror-hk-instrument-industry must be one of 0/1/2/3.")
    return level, list(HK_INSTRUMENT_INDUSTRY_FIELDS[level])


def _resolve_hk_industry_change_level(args) -> int:
    raw_level = str(
        getattr(args, "level", DEFAULT_HK_INDUSTRY_CHANGE_LEVEL)
        if getattr(args, "level", None) is not None
        else DEFAULT_HK_INDUSTRY_CHANGE_LEVEL
    ).strip()
    try:
        level = int(raw_level)
    except ValueError as exc:
        raise SystemExit("--level for mirror-hk-industry-changes must be one of 1/2/3.") from exc
    if level not in {1, 2, 3}:
        raise SystemExit("--level for mirror-hk-industry-changes must be one of 1/2/3.")
    return level


def _resolve_hk_rebalance_frequency(args, *, default: str = "M") -> str:
    freq = str(getattr(args, "rebalance_frequency", default) or default).strip().upper()
    if not freq:
        raise SystemExit("--rebalance-frequency must not be empty.")
    return freq


def _resolve_hk_snapshot_dates(
    args,
    *,
    start_date: str,
    end_date: str,
) -> tuple[list[str], dict]:
    start_ts = pd.to_datetime(start_date, format="%Y%m%d", errors="coerce")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        raise SystemExit("Unable to resolve snapshot dates from the requested date range.")

    frequency = _resolve_hk_rebalance_frequency(args)
    by_date_file = getattr(args, "by_date_file", None)
    if by_date_file:
        universe = _load_universe_by_date_frame(by_date_file)
        candidates = universe[
            (universe["trade_date"] >= start_ts.normalize())
            & (universe["trade_date"] <= end_ts.normalize())
        ]["trade_date"].drop_duplicates().tolist()
        source_meta = {
            "mode": "by_date_file",
            "by_date_file": str(_resolve_path(by_date_file)),
        }
    else:
        candidates = pd.date_range(start_ts.normalize(), end_ts.normalize(), freq="D").tolist()
        source_meta = {"mode": "calendar_range"}

    if not candidates:
        raise SystemExit("No snapshot dates resolved for industry mirroring.")

    if frequency != "D":
        dates = list(pd.to_datetime(get_rebalance_dates(sorted(candidates), frequency)))
    else:
        dates = list(pd.to_datetime(sorted(candidates)))
    normalized = [pd.Timestamp(item).normalize().strftime("%Y%m%d") for item in dates]
    normalized = _dedupe_preserve_order(normalized)
    if not normalized:
        raise SystemExit("No rebalance dates resolved for industry mirroring.")
    source_meta["rebalance_frequency"] = frequency
    source_meta["count"] = len(normalized)
    return normalized, source_meta


def _resolve_hk_trading_snapshot_dates(
    rqdatac,
    args,
    *,
    start_date: str,
    end_date: str,
) -> tuple[list[str], dict]:
    start_ts = pd.to_datetime(start_date, format="%Y%m%d", errors="coerce")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        raise SystemExit("Unable to resolve trading dates from the requested date range.")

    frequency = _resolve_hk_rebalance_frequency(args, default="D")
    by_date_file = getattr(args, "by_date_file", None)
    if by_date_file:
        universe = _load_universe_by_date_frame(by_date_file)
        candidates = universe[
            (universe["trade_date"] >= start_ts.normalize())
            & (universe["trade_date"] <= end_ts.normalize())
        ]["trade_date"].drop_duplicates().tolist()
        source_meta = {
            "mode": "by_date_file",
            "by_date_file": str(_resolve_path(by_date_file)),
        }
    else:
        candidates = list(pd.to_datetime(rqdatac.get_trading_dates(start_date, end_date, market="hk")))
        source_meta = {"mode": "trading_calendar", "market": "hk"}

    if not candidates:
        raise SystemExit("No trading dates resolved for southbound mirroring.")

    if frequency != "D":
        dates = list(pd.to_datetime(get_rebalance_dates(sorted(candidates), frequency)))
    else:
        dates = list(pd.to_datetime(sorted(candidates)))
    normalized = [pd.Timestamp(item).normalize().strftime("%Y%m%d") for item in dates]
    normalized = _dedupe_preserve_order(normalized)
    if not normalized:
        raise SystemExit("No rebalance dates resolved for southbound mirroring.")
    source_meta["rebalance_frequency"] = frequency
    source_meta["count"] = len(normalized)
    return normalized, source_meta


def _resolve_hk_southbound_trading_types(args) -> list[str]:
    raw_values = list(getattr(args, "trading_type", None) or ["both"])
    resolved: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text == "both":
            resolved.extend(DEFAULT_HK_SOUTHBOUND_TRADING_TYPES)
            continue
        if text not in DEFAULT_HK_SOUTHBOUND_TRADING_TYPES:
            raise SystemExit("--trading-type must be one of: sh, sz, both.")
        resolved.append(text)
    normalized = _dedupe_preserve_order(resolved)
    if not normalized:
        raise SystemExit("No southbound trading types resolved.")
    return normalized


def _prepare_hk_instrument_industry_frame(
    frame: pd.DataFrame | pd.Series | None,
    *,
    symbol_map: Mapping[str, str],
    query_date: str,
) -> pd.DataFrame:
    normalized = _reset_frame_index(frame)
    if normalized.empty:
        return normalized
    if "order_book_id" not in normalized.columns:
        raise ValueError("RQData payload is missing order_book_id.")
    normalized["order_book_id"] = normalized["order_book_id"].astype(str).str.strip()
    normalized["symbol"] = normalized["order_book_id"].map(
        lambda value: symbol_map.get(value) or _normalize_hk_symbol(value)
    )
    normalized = normalized[normalized["symbol"] != ""].copy()
    normalized["date"] = pd.to_datetime(query_date, format="%Y%m%d", errors="coerce")
    preferred = [column for column in ("symbol", "order_book_id", "date") if column in normalized.columns]
    remaining = [column for column in normalized.columns if column not in preferred]
    work = normalized.loc[:, preferred + remaining].copy()
    return work.sort_values(["symbol", "date"]).reset_index(drop=True)


def _build_hk_industry_catalog(
    rqdatac,
    *,
    source: str,
    level: int,
    mapping_date: str | None,
) -> pd.DataFrame:
    catalog = rqdatac.get_industry_mapping(source=source, date=mapping_date, market="hk")
    if catalog is None or (isinstance(catalog, pd.DataFrame) and catalog.empty):
        raise SystemExit("rqdatac.get_industry_mapping returned no HK industry mapping rows.")
    normalized = _reset_frame_index(catalog)
    code_column = {1: "first_industry_code", 2: "second_industry_code", 3: "third_industry_code"}[level]
    name_column = {1: "first_industry_name", 2: "second_industry_name", 3: "third_industry_name"}[level]
    required_columns = [code_column, name_column, *HK_INDUSTRY_HIERARCHY_COLUMNS]
    missing = [column for column in required_columns if column not in normalized.columns]
    if missing:
        raise SystemExit(
            "Industry mapping payload is missing required columns: " + ", ".join(missing)
        )

    normalized = normalized.loc[:, _dedupe_preserve_order(required_columns)].copy()
    normalized[code_column] = normalized[code_column].astype(str).str.strip()
    normalized[name_column] = normalized[name_column].astype(str).str.strip()
    normalized = normalized[
        (normalized[code_column] != "") & (normalized[name_column] != "")
    ].copy()
    normalized = normalized.drop_duplicates(subset=[code_column, name_column]).sort_values(
        [code_column, name_column],
        kind="mergesort",
    )
    normalized["industry_code"] = normalized[code_column]
    normalized["industry_name"] = normalized[name_column]
    normalized["industry_level"] = level
    normalized["industry_source"] = source
    normalized.reset_index(drop=True, inplace=True)
    return normalized


def _prepare_hk_industry_change_frame(
    frame: pd.DataFrame | pd.Series | None,
    *,
    catalog_row: Mapping[str, object],
    symbol_filter: set[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    normalized = _reset_frame_index(frame)
    if normalized.empty:
        return normalized
    if "order_book_id" not in normalized.columns:
        raise ValueError("RQData payload is missing order_book_id.")
    if "start_date" not in normalized.columns or "cancel_date" not in normalized.columns:
        raise ValueError("RQData payload is missing start_date/cancel_date.")

    normalized["order_book_id"] = normalized["order_book_id"].astype(str).str.strip()
    normalized["symbol"] = normalized["order_book_id"].map(_normalize_hk_symbol)
    normalized = normalized[normalized["symbol"].isin(symbol_filter)].copy()
    if normalized.empty:
        return normalized

    overlap_start = pd.to_datetime(start_date, format="%Y%m%d", errors="coerce")
    overlap_end = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce")
    normalized["start_date"] = pd.to_datetime(normalized["start_date"], errors="coerce")
    normalized["cancel_date"] = pd.to_datetime(normalized["cancel_date"], errors="coerce")
    overlap_mask = normalized["start_date"].notna() & (
        (normalized["start_date"] <= overlap_end)
        & (normalized["cancel_date"].isna() | (normalized["cancel_date"] >= overlap_start))
    )
    normalized = normalized[overlap_mask].copy()
    if normalized.empty:
        return normalized

    for column in (
        "industry_code",
        "industry_name",
        "industry_level",
        "industry_source",
        *HK_INDUSTRY_HIERARCHY_COLUMNS,
    ):
        if column in catalog_row:
            normalized[column] = catalog_row[column]

    preferred = [column for column in ("symbol", "order_book_id", "start_date") if column in normalized.columns]
    remaining = [column for column in normalized.columns if column not in preferred]
    work = normalized.loc[:, preferred + remaining].copy()
    sort_columns = [
        column
        for column in ("symbol", "start_date", "cancel_date", "industry_code")
        if column in work.columns
    ]
    return work.sort_values(sort_columns).reset_index(drop=True)
