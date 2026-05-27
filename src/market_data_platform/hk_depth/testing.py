"""Offline fake provider used by tests and dry local demonstrations."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd


class FakeProvider:
    """Deterministic tick provider that mimics RQData's indexed dataframe shape."""

    def get_price(
        self,
        order_book_ids: Sequence[str],
        start_date: str,
        end_date: str,
        fields: Sequence[str],
        adjust_type: str = "none",
        time_slice: str | None = None,
    ) -> pd.DataFrame:
        rows = []
        index = []
        timestamps = pd.date_range(
            f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]} 09:30:00",
            periods=4,
            freq="10min",
        )
        for symbol_index, symbol in enumerate(order_book_ids):
            base = 100.0 + symbol_index
            cumulative_volume = 0
            cumulative_turnover = 0.0
            for tick_index, timestamp in enumerate(timestamps, start=1):
                last = base + tick_index * 0.05
                cumulative_volume += 1000 * tick_index
                cumulative_turnover += last * 1000 * tick_index
                row = {"trading_date": start_date}
                for field in fields:
                    if field == "open":
                        row[field] = base
                    elif field == "high":
                        row[field] = last + 0.05
                    elif field == "low":
                        row[field] = base - 0.05
                    elif field == "last":
                        row[field] = last
                    elif field == "volume":
                        row[field] = cumulative_volume
                    elif field == "total_turnover":
                        row[field] = cumulative_turnover
                    elif field == "prev_close":
                        row[field] = base - 0.25
                    elif field == "num_trades":
                        row[field] = pd.NA
                    elif field in {"limit_up", "limit_down"}:
                        row[field] = pd.NA
                    elif field == "change_rate":
                        row[field] = (last / (base - 0.25) - 1) * 100
                    elif field.startswith("a") and field.endswith("_v"):
                        level = int(field[1:-2])
                        row[field] = 1000 + level * 10
                    elif field.startswith("b") and field.endswith("_v"):
                        level = int(field[1:-2])
                        row[field] = 900 + level * 10
                    elif field.startswith("a") and field[1:].isdigit():
                        level = int(field[1:])
                        row[field] = last + level * 0.01
                    elif field.startswith("b") and field[1:].isdigit():
                        level = int(field[1:])
                        row[field] = last - level * 0.01
                    else:
                        row[field] = pd.NA
                rows.append(row)
                index.append((symbol, timestamp))
        frame = pd.DataFrame(rows)
        frame.index = pd.MultiIndex.from_tuples(index, names=["order_book_id", "datetime"])
        return frame

    def quota_snapshot(self) -> dict[str, object]:
        return {
            "fake": True,
            "bytes_used": 100_000,
            "bytes_limit": 1_000_000,
            "bytes_remaining": 900_000,
            "used_pct": 10.0,
            "remaining_pct": 90.0,
        }

    def get_trading_dates(self, start_date: str, end_date: str) -> list[str]:
        dates = pd.date_range(
            f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
            f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}",
            freq="B",
        )
        return [date.strftime("%Y%m%d") for date in dates]
