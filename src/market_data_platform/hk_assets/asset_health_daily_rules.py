from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

DAILY_RULE_CHECK_NAMES = (
    "daily_price_bounds_violation",
    "daily_nonpositive_price",
    "daily_negative_volume",
    "daily_negative_total_turnover",
)


def init_daily_rule_stats() -> dict[str, dict[str, object]]:
    return {name: {"count": 0, "sample_symbols": []} for name in DAILY_RULE_CHECK_NAMES}


def record_daily_rule_stats(
    *,
    target_frame: pd.DataFrame,
    symbol: str,
    stats: dict[str, dict[str, object]],
    sample_limit: int,
) -> None:
    price_values: dict[str, float] = {}
    for price_field in ("open", "high", "low", "close"):
        if price_field not in target_frame.columns:
            continue
        price_numeric = _finite_numeric(target_frame[price_field])
        if not price_numeric.empty:
            price_values[price_field] = float(price_numeric.iloc[0])

    if {"open", "high", "low", "close"}.issubset(price_values):
        _record_price_rule_stats(
            price_values=price_values,
            symbol=symbol,
            stats=stats,
            sample_limit=sample_limit,
        )

    for field_name, stat_key in (
        ("volume", "daily_negative_volume"),
        ("total_turnover", "daily_negative_total_turnover"),
    ):
        if field_name not in target_frame.columns:
            continue
        numeric_series = _finite_numeric(target_frame[field_name])
        if not numeric_series.empty and float(numeric_series.iloc[0]) < 0:
            _increment_rule(stats, stat_key, symbol=symbol, sample_limit=sample_limit)


def build_daily_rule_quality_checks(
    *,
    daily_rule_stats: Mapping[str, Mapping[str, object]],
    symbols_with_target_date_row: int,
    sample_limit: int,
) -> list[dict[str, object]]:
    quality_checks: list[dict[str, object]] = []
    for check_name, stats in daily_rule_stats.items():
        count = int(stats.get("count") or 0)
        if count <= 0:
            continue
        sample_symbols = stats.get("sample_symbols")
        quality_checks.append(
            {
                "check": check_name,
                "field": None,
                "severity": "error",
                "affected_symbols": count,
                "affected_pct": _round_pct(count, symbols_with_target_date_row),
                "sample_symbols": list(sample_symbols or [])[:sample_limit],
            }
        )
    return quality_checks


def _record_price_rule_stats(
    *,
    price_values: Mapping[str, float],
    symbol: str,
    stats: dict[str, dict[str, object]],
    sample_limit: int,
) -> None:
    open_price = price_values["open"]
    high_price = price_values["high"]
    low_price = price_values["low"]
    close_price = price_values["close"]
    if high_price < max(open_price, low_price, close_price) or low_price > min(
        open_price,
        high_price,
        close_price,
    ):
        _increment_rule(
            stats,
            "daily_price_bounds_violation",
            symbol=symbol,
            sample_limit=sample_limit,
        )
    if min(open_price, high_price, low_price, close_price) <= 0:
        _increment_rule(
            stats,
            "daily_nonpositive_price",
            symbol=symbol,
            sample_limit=sample_limit,
        )


def _finite_numeric(series: pd.Series) -> pd.Series:
    numeric_series = pd.to_numeric(series, errors="coerce")
    return numeric_series[np.isfinite(numeric_series.to_numpy(dtype="float64"))]


def _increment_rule(
    stats: dict[str, dict[str, object]],
    key: str,
    *,
    symbol: str,
    sample_limit: int,
) -> None:
    entry = stats.setdefault(key, {"count": 0, "sample_symbols": []})
    entry["count"] = int(entry.get("count") or 0) + 1
    sample_symbols = entry.setdefault("sample_symbols", [])
    if isinstance(sample_symbols, list) and symbol not in sample_symbols:
        if len(sample_symbols) < sample_limit:
            sample_symbols.append(symbol)


def _round_pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator) * 100.0, 2)
