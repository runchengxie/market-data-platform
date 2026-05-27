from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def get_rebalance_dates(dates: Iterable[pd.Timestamp], freq: str) -> list[pd.Timestamp]:
    """Return rebalance dates based on a pandas Period frequency."""
    dates_list = list(dates)
    if not dates_list:
        return []
    if not freq or str(freq).upper() == "D":
        return sorted(pd.to_datetime(dates_list))

    date_series = pd.to_datetime(pd.Series(dates_list, name="date"))
    date_df = pd.DataFrame({"date": date_series})
    date_df["period"] = date_df["date"].dt.to_period(freq)
    rebalance_dates = date_df.groupby("period")["date"].max().sort_values().tolist()
    return rebalance_dates


def estimate_rebalance_gap(
    trade_dates: Iterable[pd.Timestamp],
    rebalance_dates: Iterable[pd.Timestamp],
) -> float:
    trade_dates_sorted = list(pd.to_datetime(list(trade_dates)))
    rebalance_dates_sorted = list(pd.to_datetime(list(rebalance_dates)))
    if len(rebalance_dates_sorted) < 2 or len(trade_dates_sorted) < 2:
        return np.nan
    date_to_idx = {date: idx for idx, date in enumerate(sorted(trade_dates_sorted))}
    gaps: list[int] = []
    for i in range(len(rebalance_dates_sorted) - 1):
        start = rebalance_dates_sorted[i]
        end = rebalance_dates_sorted[i + 1]
        if start in date_to_idx and end in date_to_idx:
            gaps.append(date_to_idx[end] - date_to_idx[start])
    if not gaps:
        return np.nan
    median_gap = float(np.median(gaps))
    return float(np.floor(median_gap + 0.5))
