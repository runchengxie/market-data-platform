"""Raw tick dataframe normalization."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

INDEX_COLUMN_NAMES = ("order_book_id", "datetime")


def normalize_tick_frame(
    df: pd.DataFrame | None,
    requested_fields: Sequence[str] = (),
) -> pd.DataFrame:
    """Normalize an RQData tick dataframe into explicit columns.

    RQData commonly returns historical tick data indexed by order book id and timestamp.
    This helper makes those values normal parquet columns and derives `trading_date`
    when the provider did not return it.
    """
    if df is None:
        columns = [*INDEX_COLUMN_NAMES, "trading_date", *requested_fields]
        return pd.DataFrame(columns=list(dict.fromkeys(columns)))

    out = df.copy()
    if isinstance(out.index, pd.MultiIndex):
        out = out.reset_index()
    elif out.index.name is not None or out.index.names != [None]:
        out = out.reset_index()

    rename_by_position: dict[str, str] = {}
    if "level_0" in out.columns and "order_book_id" not in out.columns:
        rename_by_position["level_0"] = "order_book_id"
    if "level_1" in out.columns and "datetime" not in out.columns:
        rename_by_position["level_1"] = "datetime"
    if "date" in out.columns and "trading_date" not in out.columns:
        rename_by_position["date"] = "trading_date"
    out = out.rename(columns=rename_by_position)

    if "order_book_id" not in out.columns and "order_book_id" in out.index.names:
        out = out.reset_index("order_book_id")
    if "datetime" not in out.columns and "datetime" in out.index.names:
        out = out.reset_index("datetime")

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    if "order_book_id" in out.columns:
        out["order_book_id"] = out["order_book_id"].astype("string")

    if "trading_date" not in out.columns and "datetime" in out.columns:
        out["trading_date"] = out["datetime"].dt.strftime("%Y%m%d")
    elif "trading_date" in out.columns:
        parsed = pd.to_datetime(out["trading_date"], errors="coerce")
        if parsed.notna().any():
            out["trading_date"] = parsed.dt.strftime("%Y%m%d")
        else:
            out["trading_date"] = out["trading_date"].astype("string")

    ordered = [col for col in (*INDEX_COLUMN_NAMES, "trading_date") if col in out.columns]
    remaining = [col for col in out.columns if col not in ordered]
    return out[ordered + remaining]
