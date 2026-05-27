"""Daily aggregation for raw Hong Kong depth snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from market_data_platform.hk_depth.quality import quote_ladder_flags
from market_data_platform.hk_depth.storage import (
    atomic_write_parquet,
    discover_parquet_parts,
    metadata_path,
    write_json,
)

DAILY_METRIC_COLUMNS = (
    "tick_count",
    "quote_coverage_ratio",
    "bad_quote_ratio",
    "best_spread_cross_ratio",
    "quote_ladder_invalid_count",
    "negative_depth_volume_count",
    "spread_bps_p50",
    "spread_bps_p90",
    "depth1_notional_p50",
    "depth5_notional_p50",
    "depth10_notional_p50",
    "imbalance1_p50",
    "imbalance5_p50",
    "open_30m_vwap",
    "full_day_tick_vwap",
    "open_to_tick_vwap_bps",
    "valid_vwap_increment_count",
    "vwap_invalid_increment_count",
    "volume_decrease_count",
    "turnover_decrease_count",
    "quote_quality_flag",
    "vwap_quality_flag",
    "coverage_quality_flag",
    "tick_count_quality_flag",
    "is_usable_for_research",
    "is_usable_for_cost_model",
)
DAILY_OUTPUT_COLUMNS = ("order_book_id", "trading_date", *DAILY_METRIC_COLUMNS)


def _num(df: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(df[column], errors="coerce")


def _has(group: pd.DataFrame, columns: list[str]) -> bool:
    return all(column in group.columns for column in columns)


def spread_bps(group: pd.DataFrame) -> pd.Series:
    if not _has(group, ["a1", "b1"]):
        return pd.Series(dtype="float64")
    ask = _num(group, "a1")
    bid = _num(group, "b1")
    valid = (ask > 0) & (bid > 0) & (ask >= bid)
    mid = (ask + bid) / 2
    return ((ask - bid) / mid * 10000).where(valid)


def depth_notional(group: pd.DataFrame, levels: int) -> pd.Series:
    required = []
    for level in range(1, levels + 1):
        required.extend([f"a{level}", f"a{level}_v", f"b{level}", f"b{level}_v"])
    if not _has(group, required):
        return pd.Series(dtype="float64")
    total = pd.Series(0.0, index=group.index)
    for level in range(1, levels + 1):
        total = total + _num(group, f"a{level}") * _num(group, f"a{level}_v")
        total = total + _num(group, f"b{level}") * _num(group, f"b{level}_v")
    return total


def imbalance(group: pd.DataFrame, levels: int) -> pd.Series:
    required = []
    for level in range(1, levels + 1):
        required.extend([f"a{level}", f"a{level}_v", f"b{level}", f"b{level}_v"])
    if not _has(group, required):
        return pd.Series(dtype="float64")
    ask = pd.Series(0.0, index=group.index)
    bid = pd.Series(0.0, index=group.index)
    for level in range(1, levels + 1):
        ask = ask + _num(group, f"a{level}") * _num(group, f"a{level}_v")
        bid = bid + _num(group, f"b{level}") * _num(group, f"b{level}_v")
    denom = ask + bid
    return ((bid - ask) / denom).where(denom > 0)


def _incremental_with_stats(values: pd.Series) -> dict[str, Any]:
    numeric = pd.to_numeric(values, errors="coerce")
    delta = numeric.diff()
    if not numeric.empty:
        delta.iloc[0] = numeric.iloc[0]
    negative = delta < 0
    return {
        "delta": delta.where(delta >= 0),
        "decrease_count": int(negative.sum()),
        "missing_then_resumed_count": int(
            (numeric.notna() & numeric.shift().isna() & numeric.shift(2).notna()).sum()
        ),
    }


def _vwap_with_stats(group: pd.DataFrame) -> dict[str, Any]:
    if not _has(group, ["volume", "total_turnover"]):
        return {
            "value": None,
            "valid_increment_count": 0,
            "invalid_increment_count": 0,
            "volume_decrease_count": 0,
            "turnover_decrease_count": 0,
        }
    ordered = group.sort_values("datetime") if "datetime" in group.columns else group
    volume_stats = _incremental_with_stats(ordered["volume"])
    turnover_stats = _incremental_with_stats(ordered["total_turnover"])
    volume = volume_stats["delta"]
    turnover = turnover_stats["delta"]
    valid = (volume > 0) & (turnover >= 0)
    volume_sum = volume[valid].sum()
    value = None
    if not volume_sum:
        value = None
    else:
        value = float(turnover[valid].sum() / volume_sum)
    invalid_increment_count = int((volume.isna() | turnover.isna()).sum())
    return {
        "value": value,
        "valid_increment_count": int(valid.sum()),
        "invalid_increment_count": invalid_increment_count,
        "volume_decrease_count": int(volume_stats["decrease_count"]),
        "turnover_decrease_count": int(turnover_stats["decrease_count"]),
    }


def _open_30m(group: pd.DataFrame) -> pd.DataFrame:
    if "datetime" not in group.columns:
        return group.iloc[0:0]
    ordered = group.copy()
    ordered["datetime"] = pd.to_datetime(ordered["datetime"], errors="coerce")
    start = ordered["datetime"].min()
    if pd.isna(start):
        return ordered.iloc[0:0]
    return ordered.loc[ordered["datetime"] <= start + pd.Timedelta(minutes=30)]


def aggregate_group(group: pd.DataFrame) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_book_id": group["order_book_id"].iloc[0],
        "trading_date": group["trading_date"].iloc[0],
        "tick_count": int(len(group)),
    }

    if _has(group, ["a1", "b1"]):
        ask = _num(group, "a1")
        bid = _num(group, "b1")
        positive = (ask > 0) & (bid > 0)
        flags = quote_ladder_flags(group)
        invalid = flags["crossed_best_spread"]
        row["quote_coverage_ratio"] = float(positive.mean())
        row["bad_quote_ratio"] = float((~positive | invalid).mean())
        row["best_spread_cross_ratio"] = float(invalid.mean())
        row["quote_ladder_invalid_count"] = int(flags["quote_ladder_invalid"].sum())
        row["negative_depth_volume_count"] = int(flags["negative_depth_volume"].sum())
    else:
        row["quote_coverage_ratio"] = pd.NA
        row["bad_quote_ratio"] = pd.NA
        row["best_spread_cross_ratio"] = pd.NA
        row["quote_ladder_invalid_count"] = pd.NA
        row["negative_depth_volume_count"] = pd.NA

    spread = spread_bps(group)
    row["spread_bps_p50"] = float(spread.quantile(0.50)) if not spread.dropna().empty else pd.NA
    row["spread_bps_p90"] = float(spread.quantile(0.90)) if not spread.dropna().empty else pd.NA

    for levels in (1, 5, 10):
        depth = depth_notional(group, levels)
        key = f"depth{levels}_notional_p50"
        row[key] = float(depth.quantile(0.50)) if not depth.dropna().empty else pd.NA

    for levels in (1, 5):
        series = imbalance(group, levels)
        key = f"imbalance{levels}_p50"
        row[key] = float(series.quantile(0.50)) if not series.dropna().empty else pd.NA

    full_vwap_stats = _vwap_with_stats(group)
    open_vwap_stats = _vwap_with_stats(_open_30m(group))
    full_vwap = full_vwap_stats["value"]
    open_vwap = open_vwap_stats["value"]
    row["open_30m_vwap"] = open_vwap if open_vwap is not None else pd.NA
    row["full_day_tick_vwap"] = full_vwap if full_vwap is not None else pd.NA
    if open_vwap is not None and full_vwap:
        row["open_to_tick_vwap_bps"] = float((open_vwap / full_vwap - 1) * 10000)
    else:
        row["open_to_tick_vwap_bps"] = pd.NA
    row["valid_vwap_increment_count"] = int(full_vwap_stats["valid_increment_count"])
    row["vwap_invalid_increment_count"] = int(full_vwap_stats["invalid_increment_count"])
    row["volume_decrease_count"] = int(full_vwap_stats["volume_decrease_count"])
    row["turnover_decrease_count"] = int(full_vwap_stats["turnover_decrease_count"])

    quote_coverage = row["quote_coverage_ratio"]
    quote_invalid_count = row["quote_ladder_invalid_count"]
    if pd.isna(quote_coverage):
        row["quote_quality_flag"] = "missing"
    elif int(quote_invalid_count or 0) > 0:
        row["quote_quality_flag"] = "fail"
    elif float(quote_coverage) < 0.80:
        row["quote_quality_flag"] = "warning"
    else:
        row["quote_quality_flag"] = "pass"

    if full_vwap is None:
        row["vwap_quality_flag"] = "fail"
    elif row["volume_decrease_count"] or row["turnover_decrease_count"]:
        row["vwap_quality_flag"] = "warning"
    else:
        row["vwap_quality_flag"] = "pass"

    row["coverage_quality_flag"] = "pass" if row["tick_count"] > 0 else "fail"
    row["tick_count_quality_flag"] = "pass" if row["tick_count"] >= 2 else "warning"
    row["is_usable_for_research"] = bool(
        row["coverage_quality_flag"] == "pass"
        and row["quote_quality_flag"] in {"pass", "warning"}
        and row["vwap_quality_flag"] in {"pass", "warning"}
    )
    row["is_usable_for_cost_model"] = bool(
        row["coverage_quality_flag"] == "pass"
        and row["quote_quality_flag"] == "pass"
        and row["vwap_quality_flag"] in {"pass", "warning"}
    )

    return row


def unavailable_metrics_for_columns(
    columns: set[str] | list[str] | pd.Index,
) -> dict[str, list[str]]:
    requirements = {
        "spread_bps_p50": ["a1", "b1"],
        "spread_bps_p90": ["a1", "b1"],
        "depth1_notional_p50": ["a1", "a1_v", "b1", "b1_v"],
        "depth5_notional_p50": [
            *(f"a{i}" for i in range(1, 6)),
            *(f"a{i}_v" for i in range(1, 6)),
            *(f"b{i}" for i in range(1, 6)),
            *(f"b{i}_v" for i in range(1, 6)),
        ],
        "depth10_notional_p50": [
            *(f"a{i}" for i in range(1, 11)),
            *(f"a{i}_v" for i in range(1, 11)),
            *(f"b{i}" for i in range(1, 11)),
            *(f"b{i}_v" for i in range(1, 11)),
        ],
        "imbalance1_p50": ["a1", "a1_v", "b1", "b1_v"],
        "imbalance5_p50": [
            *(f"a{i}" for i in range(1, 6)),
            *(f"a{i}_v" for i in range(1, 6)),
            *(f"b{i}" for i in range(1, 6)),
            *(f"b{i}_v" for i in range(1, 6)),
        ],
        "open_30m_vwap": ["datetime", "volume", "total_turnover"],
        "full_day_tick_vwap": ["volume", "total_turnover"],
        "open_to_tick_vwap_bps": ["datetime", "volume", "total_turnover"],
    }
    available = set(columns)
    missing: dict[str, list[str]] = {}
    for metric, columns in requirements.items():
        absent = [column for column in columns if column not in available]
        if absent:
            missing[metric] = absent
    return missing


def unavailable_metrics(df: pd.DataFrame) -> dict[str, list[str]]:
    return unavailable_metrics_for_columns(df.columns)


def aggregate_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=DAILY_OUTPUT_COLUMNS)
    required = {"order_book_id", "trading_date"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required aggregate columns: {sorted(missing)}")
    rows = [
        aggregate_group(group)
        for _, group in df.groupby(["order_book_id", "trading_date"], sort=True, dropna=False)
    ]
    out = pd.DataFrame(rows)
    for column in DAILY_METRIC_COLUMNS:
        if column not in out.columns:
            out[column] = pd.NA
    return out[list(DAILY_OUTPUT_COLUMNS)]


def aggregate_daily_parts(input_root: str | Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    parts = discover_parquet_parts(input_root)
    if not parts:
        aggregate = aggregate_daily_frame(pd.DataFrame())
        return aggregate, {
            "source_rows": 0,
            "source_parts": 0,
            "source_fields": [],
            "missing_source_fields": unavailable_metrics_for_columns(set()),
        }

    rows: list[pd.DataFrame] = []
    source_rows = 0
    source_columns: list[str] = []
    seen_columns: set[str] = set()
    seen_units: set[tuple[str, str]] = set()
    duplicate_units: set[tuple[str, str]] = set()

    for part in parts:
        frame = pd.read_parquet(part)
        source_rows += int(len(frame))
        for column in frame.columns:
            column_name = str(column)
            if column_name not in seen_columns:
                seen_columns.add(column_name)
                source_columns.append(column_name)
        if frame.empty:
            continue
        aggregate = aggregate_daily_frame(frame)
        if aggregate.empty:
            continue
        part_units = {
            (str(row.order_book_id), str(row.trading_date))
            for row in aggregate.loc[:, ["order_book_id", "trading_date"]].itertuples(
                index=False
            )
        }
        duplicate_units.update(seen_units & part_units)
        seen_units.update(part_units)
        rows.append(aggregate)

    if duplicate_units:
        sample = ", ".join(f"{symbol}/{date}" for symbol, date in sorted(duplicate_units)[:5])
        raise ValueError(
            "Cannot stream aggregate duplicate symbol-date units split across parquet parts: "
            f"{sample}."
        )

    if rows:
        concat_ready = [frame.dropna(axis=1, how="all") for frame in rows]
        output = pd.concat(concat_ready, ignore_index=True).reindex(
            columns=DAILY_OUTPUT_COLUMNS
        ).sort_values(
            ["order_book_id", "trading_date"],
            ignore_index=True,
        )
    else:
        output = aggregate_daily_frame(pd.DataFrame())

    return output, {
        "source_rows": source_rows,
        "source_parts": len(parts),
        "source_fields": source_columns,
        "missing_source_fields": unavailable_metrics_for_columns(seen_columns),
    }


def write_daily_aggregate(
    input_root: str | Path,
    output_path: str | Path,
    meta_output: str | Path | None = None,
    schema_version: str = "tick_depth_daily.v1",
) -> dict[str, Any]:
    aggregate, stream_meta = aggregate_daily_parts(input_root)
    output = atomic_write_parquet(aggregate, output_path)
    unavailable = stream_meta["missing_source_fields"]
    metadata = {
        "kind": "aggregate_daily",
        "schema_version": schema_version,
        "source_path": str(input_root),
        "output_path": str(output),
        "source_rows": int(stream_meta["source_rows"]),
        "source_parts": int(stream_meta["source_parts"]),
        "source_fields": list(stream_meta["source_fields"]),
        "rows": int(len(aggregate)),
        "unavailable_metrics": sorted(unavailable),
        "missing_source_fields": unavailable,
    }
    meta_path = (
        Path(meta_output) if meta_output else metadata_path(Path(output).parent, "aggregate_daily")
    )
    write_json(meta_path, metadata)
    metadata["metadata_path"] = str(meta_path)
    return metadata
