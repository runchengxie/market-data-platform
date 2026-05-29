from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import numpy as np
import pandas as pd


def get_rebalance_dates(dates: Iterable[pd.Timestamp], freq: str) -> list[pd.Timestamp]:
    """Return rebalance dates based on a pandas Period frequency."""
    dates_list = list(dates)
    if not dates_list:
        return []
    if not freq or str(freq).upper() == "D":
        return sorted(pd.to_datetime(dates_list))

    period_end_dates: dict[pd.Period, pd.Timestamp] = {}
    valid_dates: list[pd.Timestamp] = []
    for value in dates_list:
        date = cast(pd.Timestamp, pd.Timestamp(value))
        if not pd.isna(date):
            valid_dates.append(date)
    for date in sorted(valid_dates):
        period_end_dates[date.to_period(freq)] = date
    return list(period_end_dates.values())


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
