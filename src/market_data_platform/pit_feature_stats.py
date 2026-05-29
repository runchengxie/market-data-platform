from __future__ import annotations

import numpy as np
import pandas as pd


def compute_trailing_calendar_window_stat(
    frame: pd.DataFrame,
    value_series: pd.Series,
    *,
    years: int,
    stat: str,
    min_periods: int = 3,
) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    if frame.empty:
        return result

    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    values = pd.to_numeric(value_series, errors="coerce")

    for _, index_values in frame.groupby("symbol", sort=False).groups.items():
        group_index = pd.Index(index_values)
        group_dates = trade_dates.loc[group_index]
        ordered_index = group_dates.sort_values(kind="stable").index
        ordered_dates = trade_dates.loc[ordered_index]
        ordered_values = values.loc[ordered_index]
        result.loc[ordered_index] = _compute_group_trailing_stat(
            ordered_dates,
            ordered_values,
            years=years,
            stat=stat,
            min_periods=min_periods,
        ).to_numpy(dtype=float)

    return result


def compute_calendar_cagr(
    frame: pd.DataFrame,
    value_series: pd.Series,
    *,
    years: int,
) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    if frame.empty:
        return result

    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    values = pd.to_numeric(value_series, errors="coerce")

    for _, index_values in frame.groupby("symbol", sort=False).groups.items():
        group_index = pd.Index(index_values)
        group_dates = trade_dates.loc[group_index]
        ordered_index = group_dates.sort_values(kind="stable").index
        ordered_dates = trade_dates.loc[ordered_index]
        ordered_values = values.loc[ordered_index]
        result.loc[ordered_index] = _compute_group_cagr(
            ordered_dates,
            ordered_values,
            years=years,
        ).to_numpy(dtype=float)

    return result


def _compute_group_trailing_stat(
    dates: pd.Series,
    values: pd.Series,
    *,
    years: int,
    stat: str,
    min_periods: int,
) -> pd.Series:
    result = pd.Series(np.nan, index=dates.index, dtype=float)
    if dates.empty:
        return result

    for row_index, current_date in dates.items():
        if pd.isna(current_date):
            continue
        window_start = current_date - pd.DateOffset(years=years)
        mask = (dates >= window_start) & (dates <= current_date)
        window = values.loc[mask].dropna()
        if len(window) < min_periods:
            continue
        if stat == "mean":
            result.loc[row_index] = float(window.mean())
        elif stat == "median":
            result.loc[row_index] = float(window.median())
        elif stat == "std":
            result.loc[row_index] = float(window.std(ddof=0))
        elif stat == "positive_ratio":
            result.loc[row_index] = float((window > 0).mean())
        else:
            raise ValueError(f"Unsupported trailing calendar stat: {stat}")

    return result


def _compute_group_cagr(
    dates: pd.Series,
    values: pd.Series,
    *,
    years: int,
) -> pd.Series:
    result = pd.Series(np.nan, index=dates.index, dtype=float)
    if dates.empty:
        return result

    for row_index, current_date in dates.items():
        if pd.isna(current_date):
            continue
        current_value = values.loc[row_index]
        if pd.isna(current_value) or float(current_value) <= 0:
            continue

        anchor_target = current_date - pd.DateOffset(years=years)
        anchor_mask = (dates <= anchor_target) & values.notna()
        if not anchor_mask.any():
            continue

        anchor_date = dates.loc[anchor_mask].iloc[-1]
        anchor_value = values.loc[anchor_mask].iloc[-1]
        if pd.isna(anchor_date) or pd.isna(anchor_value) or float(anchor_value) <= 0:
            continue

        elapsed_years = (current_date - anchor_date).days / 365.25
        if elapsed_years <= 0:
            continue
        growth_rate = np.exp(np.log(current_value / anchor_value) / elapsed_years) - 1.0
        result.loc[row_index] = float(growth_rate)

    return result
