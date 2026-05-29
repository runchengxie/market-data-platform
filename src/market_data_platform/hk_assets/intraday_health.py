from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from market_data_platform.symbols import normalize_symbol_for_market
from market_data_platform.intraday_paths import resolve_input_parquet_paths, resolve_intraday_input_groups
from .quality_gate import (
    append_quality_verdict_lines,
    quality_gate_exit_code,
    summarize_quality_checks,
)
from .shared import _load_manifest, _normalize_frame_columns, _resolve_path


_EXPECTED_HK_5M_MORNING_TIME_KEYS = pd.date_range("09:35", "12:00", freq="5min").strftime("%H:%M").tolist()
_EXPECTED_HK_5M_AFTERNOON_TIME_KEYS = pd.date_range("13:05", "16:00", freq="5min").strftime("%H:%M").tolist()
_EXPECTED_HK_5M_TIME_KEYS = [
    *_EXPECTED_HK_5M_MORNING_TIME_KEYS,
    *_EXPECTED_HK_5M_AFTERNOON_TIME_KEYS,
]
_EXPECTED_HK_5M_TIME_KEY_SET = set(_EXPECTED_HK_5M_TIME_KEYS)
_EXPECTED_HK_5M_TIME_KEY_INDEX = {
    time_key: index
    for index, time_key in enumerate(_EXPECTED_HK_5M_TIME_KEYS)
}
_EXPECTED_HK_5M_TIME_MASK_LO = sum(1 << index for index in range(min(63, len(_EXPECTED_HK_5M_TIME_KEYS))))
_EXPECTED_HK_5M_TIME_MASK_HI = sum(
    1 << (index - 63)
    for index in range(63, len(_EXPECTED_HK_5M_TIME_KEYS))
)
_EXPECTED_HK_5M_MORNING_TIME_MASK_LO = sum(
    1 << _EXPECTED_HK_5M_TIME_KEY_INDEX[time_key]
    for time_key in _EXPECTED_HK_5M_MORNING_TIME_KEYS
    if _EXPECTED_HK_5M_TIME_KEY_INDEX[time_key] < 63
)
_EXPECTED_HK_5M_MORNING_TIME_MASK_HI = sum(
    1 << (_EXPECTED_HK_5M_TIME_KEY_INDEX[time_key] - 63)
    for time_key in _EXPECTED_HK_5M_MORNING_TIME_KEYS
    if _EXPECTED_HK_5M_TIME_KEY_INDEX[time_key] >= 63
)
_INFERRED_HALF_DAY_MIN_SYMBOL_DAYS = 50
_MINOR_DAILY_RECON_PRICE_DIFF_ATOL = 0.2
_INTRADAY_HEALTH_COLUMNS = (
    "symbol",
    "ts_code",
    "rq_order_book_id",
    "order_book_id",
    "trade_datetime",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "total_turnover",
)
_INTRADAY_READ_BATCH_SIZE = 500_000
_PRICE_RECONCILIATION_ISSUES = {
    "daily_open_mismatch",
    "daily_high_mismatch",
    "daily_low_mismatch",
    "daily_close_mismatch",
}


@dataclass(frozen=True)
class IntradayHealthConfig:
    input_specs: list[str]
    parquet_paths: list[Path]
    sample_limit: int
    expected_bars_per_day: int
    numeric_rtol: float
    numeric_atol: float
    daily_asset_dir: Path | None
    intraday_adjust_type: str | None
    daily_adjust_type: str | None
    fail_on_severity: str
    output_format: str
    out_path: Path | None


@dataclass
class IntradayHealthScan:
    rows_scanned: int
    symbols_seen: set[str]
    trade_date_min: pd.Timestamp | None
    trade_date_max: pd.Timestamp | None
    intraday_daily: pd.DataFrame
    inferred_half_day_dates: set[str]
    duplicate_timestamp_groups: int
    duplicate_timestamp_rows: int
    missing_bar_symbol_days: int
    missing_bar_rows: int
    unexpected_bar_count_symbol_days: int
    off_schedule_bar_rows: int
    negative_volume_rows: int
    negative_amount_rows: int
    bar_count_values: list[int]
    sample_duplicate_rows: list[dict[str, object]]
    sample_missing_symbol_days: list[dict[str, object]]
    sample_negative_rows: list[dict[str, object]]
    sample_unexpected_bar_count_symbol_days: list[dict[str, object]]
    sample_off_schedule_rows: list[dict[str, object]]


def _round_pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator) * 100.0, 2)


def _quantile_or_none(values: Sequence[int], quantile: float) -> int | float | None:
    if not values:
        return None
    result = float(pd.Series(list(values), dtype="float64").quantile(quantile))
    if result.is_integer():
        return int(result)
    return round(result, 2)


def _format_date(value: object) -> str | None:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.normalize().strftime("%Y-%m-%d")


def _format_timestamp(value: object) -> str | None:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if timestamp == timestamp.normalize():
        return timestamp.strftime("%Y-%m-%d")
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _serialize_scalar(value: object) -> int | float | str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return _format_timestamp(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        numeric = float(value)
        if not np.isfinite(numeric):
            return str(numeric)
        if numeric.is_integer():
            return int(numeric)
        return round(numeric, 8)
    return str(value)


def _normalize_adjust_type(value: object | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return text


def _manifest_query_adjust_type(asset_dir: Path | None) -> str | None:
    if asset_dir is None:
        return None
    manifest_path = asset_dir / "manifest.yml"
    if not manifest_path.exists():
        return None
    manifest = _load_manifest(manifest_path)
    query = manifest.get("query") if isinstance(manifest, Mapping) else None
    if not isinstance(query, Mapping):
        return None
    return _normalize_adjust_type(query.get("adjust_type"))


def _infer_intraday_adjust_type(input_specs: Sequence[str]) -> str | None:
    inferred: set[str] = set()
    for group in resolve_intraday_input_groups(list(input_specs)):
        meta_path = group.meta_path
        if meta_path is None or not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        value = _normalize_adjust_type(payload.get("adjust_type"))
        if value:
            inferred.add(value)
    if len(inferred) == 1:
        return next(iter(inferred))
    return None


def _bitwise_or_int(values: pd.Series) -> int:
    if values.empty:
        return 0
    return int(np.bitwise_or.reduce(values.to_numpy(dtype=np.uint64)))


def _read_intraday_frame_chunks(path: Path) -> Sequence[pd.DataFrame]:
    parquet_file = pq.ParquetFile(path)
    available_columns = set(parquet_file.schema_arrow.names)
    columns = [column for column in _INTRADAY_HEALTH_COLUMNS if column in available_columns]
    if not columns:
        return []
    return (
        batch.to_pandas()
        for batch in parquet_file.iter_batches(
            batch_size=_INTRADAY_READ_BATCH_SIZE,
            columns=columns,
        )
    )


def _add_expected_time_masks(work: pd.DataFrame) -> pd.DataFrame:
    time_index = work["time_key"].map(_EXPECTED_HK_5M_TIME_KEY_INDEX)
    valid_mask = time_index.notna()
    work["_time_mask_lo"] = np.uint64(0)
    work["_time_mask_hi"] = np.uint64(0)
    if not bool(valid_mask.any()):
        return work

    index_values = time_index.loc[valid_mask].astype("int64")
    low_mask = index_values < 63
    if bool(low_mask.any()):
        low_index = index_values.loc[low_mask].to_numpy(dtype=np.uint64)
        work.loc[index_values.loc[low_mask].index, "_time_mask_lo"] = np.left_shift(np.uint64(1), low_index)
    high_mask = ~low_mask
    if bool(high_mask.any()):
        high_index = (index_values.loc[high_mask] - 63).to_numpy(dtype=np.uint64)
        work.loc[index_values.loc[high_mask].index, "_time_mask_hi"] = np.left_shift(np.uint64(1), high_index)
    return work


def _missing_times_from_masks(
    lo_mask: int,
    hi_mask: int,
    *,
    expected_lo_mask: int = _EXPECTED_HK_5M_TIME_MASK_LO,
    expected_hi_mask: int = _EXPECTED_HK_5M_TIME_MASK_HI,
) -> list[str]:
    missing: list[str] = []
    lo_value = int(lo_mask)
    hi_value = int(hi_mask)
    for time_key, index in _EXPECTED_HK_5M_TIME_KEY_INDEX.items():
        if index < 63:
            expected = bool(int(expected_lo_mask) & (1 << index))
            if expected and not (lo_value & (1 << index)):
                missing.append(time_key)
        else:
            expected = bool(int(expected_hi_mask) & (1 << (index - 63)))
            if expected and not (hi_value & (1 << (index - 63))):
                missing.append(time_key)
    return missing


def _infer_half_day_dates(intraday_daily: pd.DataFrame) -> set[str]:
    """Infer market-wide morning-only sessions from observed all-market 5m bars."""
    if intraday_daily.empty:
        return set()

    inferred: set[str] = set()
    for trade_date, date_frame in intraday_daily.groupby("trade_date", sort=True):
        if len(date_frame) < _INFERRED_HALF_DAY_MIN_SYMBOL_DAYS:
            continue
        afternoon_mask = pd.to_numeric(
            date_frame["intraday_time_mask_hi"],
            errors="coerce",
        ).fillna(0)
        if bool((afternoon_mask != 0).any()):
            continue
        bar_counts = pd.to_numeric(date_frame["intraday_bar_count"], errors="coerce").dropna()
        if bar_counts.empty:
            continue
        if float(bar_counts.quantile(0.9)) <= len(_EXPECTED_HK_5M_MORNING_TIME_KEYS):
            date_key = _format_date(trade_date)
            if date_key:
                inferred.add(date_key)
    return inferred


def _expected_masks_for_date(
    trade_date: object,
    inferred_half_day_dates: set[str],
    *,
    full_day_expected_bars: int = len(_EXPECTED_HK_5M_TIME_KEYS),
) -> tuple[int, int, int]:
    date_key = _format_date(trade_date)
    if date_key in inferred_half_day_dates:
        return (
            _EXPECTED_HK_5M_MORNING_TIME_MASK_LO,
            _EXPECTED_HK_5M_MORNING_TIME_MASK_HI,
            len(_EXPECTED_HK_5M_MORNING_TIME_KEYS),
        )
    return (
        _EXPECTED_HK_5M_TIME_MASK_LO,
        _EXPECTED_HK_5M_TIME_MASK_HI,
        full_day_expected_bars,
    )


def _resolve_intraday_symbol_series(frame: pd.DataFrame) -> pd.Series:
    for column in ("symbol", "ts_code", "rq_order_book_id", "order_book_id"):
        if column in frame.columns:
            return frame[column]
    raise SystemExit(
        "Intraday frame is missing a canonical symbol column. "
        "Legacy aliases ts_code, rq_order_book_id, and order_book_id remain accepted."
    )


def _normalize_intraday_frame(frame: pd.DataFrame) -> pd.DataFrame:
    work = _normalize_frame_columns(frame)
    if "trade_datetime" not in work.columns and "datetime" in work.columns:
        work = work.rename(columns={"datetime": "trade_datetime"})
    if "amount" not in work.columns and "total_turnover" in work.columns:
        work = work.rename(columns={"total_turnover": "amount"})

    work["symbol"] = _resolve_intraday_symbol_series(work).map(
        lambda value: normalize_symbol_for_market(value, market="hk")
    )
    work["trade_datetime"] = pd.to_datetime(work.get("trade_datetime"), errors="coerce")
    work = work.dropna(subset=["symbol", "trade_datetime"]).copy()
    work["trade_date"] = work["trade_datetime"].dt.normalize()
    work["time_key"] = work["trade_datetime"].dt.strftime("%H:%M")

    for field in ("open", "high", "low", "close", "volume", "amount"):
        if field in work.columns:
            work[field] = pd.to_numeric(work[field], errors="coerce")
        else:
            work[field] = np.nan
    return work.sort_values(["symbol", "trade_datetime"]).reset_index(drop=True)


def _append_sample(samples: list[dict[str, object]], row: Mapping[str, object], *, limit: int) -> None:
    if len(samples) >= limit:
        return
    payload = {key: _serialize_scalar(value) for key, value in row.items()}
    if payload in samples:
        return
    samples.append(payload)


def _aggregate_intraday_daily(frame: pd.DataFrame) -> pd.DataFrame:
    work = _add_expected_time_masks(frame.copy())
    grouped = work.groupby(["symbol", "trade_date"], sort=True)
    daily = grouped.agg(
        intraday_open=("open", "first"),
        intraday_high=("high", "max"),
        intraday_low=("low", "min"),
        intraday_close=("close", "last"),
        intraday_volume=("volume", "sum"),
        intraday_amount=("amount", "sum"),
        intraday_bar_count=("trade_datetime", "size"),
        intraday_first_datetime=("trade_datetime", "first"),
        intraday_last_datetime=("trade_datetime", "last"),
        intraday_time_mask_lo=("_time_mask_lo", _bitwise_or_int),
        intraday_time_mask_hi=("_time_mask_hi", _bitwise_or_int),
    ).reset_index()
    return daily


def _combine_intraday_daily_parts(parts: list[pd.DataFrame]) -> pd.DataFrame:
    if not parts:
        return pd.DataFrame(
            columns=[
                "symbol",
                "trade_date",
                "intraday_open",
                "intraday_high",
                "intraday_low",
                "intraday_close",
                "intraday_volume",
                "intraday_amount",
                "intraday_bar_count",
                "intraday_first_datetime",
                "intraday_last_datetime",
                "intraday_time_mask_lo",
                "intraday_time_mask_hi",
            ]
        )

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.sort_values(["symbol", "trade_date", "intraday_first_datetime"]).reset_index(drop=True)
    open_frame = (
        combined.groupby(["symbol", "trade_date"], sort=True)
        .first()[["intraday_open", "intraday_first_datetime"]]
        .reset_index()
    )
    close_frame = (
        combined.sort_values(["symbol", "trade_date", "intraday_last_datetime"])
        .groupby(["symbol", "trade_date"], sort=True)
        .last()[["intraday_close", "intraday_last_datetime"]]
        .reset_index()
    )
    aggregate_frame = (
        combined.groupby(["symbol", "trade_date"], sort=True, as_index=False)
        .agg(
            intraday_high=("intraday_high", "max"),
            intraday_low=("intraday_low", "min"),
            intraday_volume=("intraday_volume", "sum"),
            intraday_amount=("intraday_amount", "sum"),
            intraday_bar_count=("intraday_bar_count", "sum"),
            intraday_time_mask_lo=("intraday_time_mask_lo", _bitwise_or_int),
            intraday_time_mask_hi=("intraday_time_mask_hi", _bitwise_or_int),
        )
    )
    return (
        aggregate_frame.merge(open_frame, on=["symbol", "trade_date"], how="left")
        .merge(close_frame, on=["symbol", "trade_date"], how="left")
        .sort_values(["symbol", "trade_date"])
        .reset_index(drop=True)
    )


def _compare_numeric(
    left: pd.Series,
    right: pd.Series,
    *,
    rtol: float,
    atol: float,
) -> pd.Series:
    left_numeric = pd.to_numeric(left, errors="coerce")
    right_numeric = pd.to_numeric(right, errors="coerce")
    both = left_numeric.notna() & right_numeric.notna()
    result = pd.Series(False, index=left.index, dtype=bool)
    if bool(both.any()):
        result.loc[both] = ~np.isclose(
            left_numeric.loc[both].to_numpy(dtype="float64"),
            right_numeric.loc[both].to_numpy(dtype="float64"),
            rtol=rtol,
            atol=atol,
        )
    return result


def _daily_reconciliation_exact_match_mask(
    merged: pd.DataFrame,
    *,
    rtol: float,
    atol: float,
) -> dict[str, pd.Series]:
    return {
        "close": ~_compare_numeric(merged["intraday_close"], merged["daily_close"], rtol=rtol, atol=atol),
        "volume": ~_compare_numeric(merged["intraday_volume"], merged["daily_volume"], rtol=rtol, atol=atol),
        "amount": ~_compare_numeric(merged["intraday_amount"], merged["daily_amount"], rtol=rtol, atol=atol),
    }


def _zero_volume_amount_mask(frame: pd.DataFrame) -> pd.Series:
    volume = pd.to_numeric(frame.get("intraday_volume"), errors="coerce")
    amount = pd.to_numeric(frame.get("intraday_amount"), errors="coerce")
    return volume.notna() & amount.notna() & np.isclose(volume, 0.0, rtol=0.0, atol=1e-8) & np.isclose(
        amount,
        0.0,
        rtol=0.0,
        atol=1e-8,
    )


def _resolve_reconciliation_suppressed_mask(
    *,
    merged: pd.DataFrame,
    mismatch_mask: pd.Series,
    issue_key: str,
    exact_match_mask: Mapping[str, pd.Series],
) -> pd.Series:
    base_mask = (
        mismatch_mask
        & exact_match_mask["close"]
        & exact_match_mask["volume"]
        & exact_match_mask["amount"]
    )
    if not bool(base_mask.any()):
        return pd.Series(False, index=merged.index, dtype=bool)

    if issue_key == "daily_open_mismatch":
        within_session_range = (
            merged["daily_open"].notna()
            & merged["intraday_low"].notna()
            & merged["intraday_high"].notna()
            & (merged["daily_open"] >= merged["intraday_low"])
            & (merged["daily_open"] <= merged["intraday_high"])
        )
        return base_mask & within_session_range

    if issue_key in {"daily_high_mismatch", "daily_low_mismatch"}:
        intraday_field = "intraday_high" if issue_key == "daily_high_mismatch" else "intraday_low"
        daily_field = "daily_high" if issue_key == "daily_high_mismatch" else "daily_low"
        diff = (pd.to_numeric(merged[intraday_field], errors="coerce") - pd.to_numeric(
            merged[daily_field], errors="coerce")
        ).abs()
        small_diff_mask = diff <= _MINOR_DAILY_RECON_PRICE_DIFF_ATOL
        if issue_key == "daily_high_mismatch":
            auction_like_mask = (
                pd.to_numeric(merged["daily_high"], errors="coerce")
                > pd.to_numeric(merged["intraday_high"], errors="coerce")
            ) & np.isclose(
                pd.to_numeric(merged["daily_high"], errors="coerce"),
                pd.to_numeric(merged["daily_open"], errors="coerce"),
                rtol=1e-6,
                atol=1e-8,
            )
        else:
            auction_like_mask = (
                pd.to_numeric(merged["daily_low"], errors="coerce")
                < pd.to_numeric(merged["intraday_low"], errors="coerce")
            ) & np.isclose(
                pd.to_numeric(merged["daily_low"], errors="coerce"),
                pd.to_numeric(merged["daily_open"], errors="coerce"),
                rtol=1e-6,
                atol=1e-8,
            )
        return base_mask & (small_diff_mask | auction_like_mask)

    return pd.Series(False, index=merged.index, dtype=bool)


def _build_daily_reconciliation(
    *,
    intraday_daily: pd.DataFrame,
    daily_asset_dir: Path,
    sample_limit: int,
    rtol: float,
    atol: float,
    intraday_adjust_type: str | None,
    daily_adjust_type: str | None,
) -> dict[str, object]:
    data_dir = daily_asset_dir / "data"
    if not data_dir.exists():
        raise SystemExit(f"Daily asset directory is missing data/: {daily_asset_dir}")

    mismatch_counts: Counter[str] = Counter()
    suppressed_mismatch_counts: Counter[str] = Counter()
    sample_missing_daily_rows: list[dict[str, object]] = []
    sample_inactive_zero_volume_rows: list[dict[str, object]] = []
    sample_inactive_zero_volume_missing_daily_rows: list[dict[str, object]] = []
    sample_intraday_after_daily_end_with_trading_rows: list[dict[str, object]] = []
    sample_daily_active_missing_intraday_rows: list[dict[str, object]] = []
    sample_mismatch_rows: list[dict[str, object]] = []
    reconciled_symbol_days = 0
    missing_daily_rows = 0
    inactive_zero_volume_after_daily_end_rows = 0
    inactive_zero_volume_missing_daily_rows = 0
    intraday_after_daily_end_with_trading_rows = 0
    daily_active_missing_intraday_rows = 0
    intraday_date_min = intraday_daily["trade_date"].min() if not intraday_daily.empty else None
    intraday_date_max = intraday_daily["trade_date"].max() if not intraday_daily.empty else None
    price_adjustment_basis_mismatch = (
        intraday_adjust_type is not None
        and (daily_adjust_type is None or intraday_adjust_type != daily_adjust_type)
    )

    for symbol, symbol_df in intraday_daily.groupby("symbol", sort=True):
        daily_path = data_dir / f"{symbol}.parquet"
        if not daily_path.exists():
            missing_daily_rows += int(len(symbol_df))
            for _, row in symbol_df.head(sample_limit).iterrows():
                _append_sample(
                    sample_missing_daily_rows,
                    {
                        "symbol": symbol,
                        "trade_date": row["trade_date"],
                        "reason": "daily_parquet_missing",
                    },
                    limit=sample_limit,
                )
            continue

        daily_frame = _normalize_frame_columns(
            pd.read_parquet(daily_path)
        )
        if "trade_date" not in daily_frame.columns:
            continue
        daily_frame["trade_date"] = pd.to_datetime(daily_frame["trade_date"], errors="coerce").dt.normalize()
        daily_frame = daily_frame.dropna(subset=["trade_date"]).copy()
        daily_frame = daily_frame.rename(
            columns={
                "open": "daily_open",
                "high": "daily_high",
                "low": "daily_low",
                "close": "daily_close",
                "volume": "daily_volume",
                "total_turnover": "daily_amount",
            }
        )
        if pd.notna(intraday_date_min) and pd.notna(intraday_date_max):
            daily_active_candidates = daily_frame.loc[
                (daily_frame["trade_date"] >= intraday_date_min)
                & (daily_frame["trade_date"] <= intraday_date_max)
            ].copy()
            daily_volume = pd.to_numeric(
                daily_active_candidates.get("daily_volume"),
                errors="coerce",
            ).fillna(0.0)
            daily_amount = pd.to_numeric(
                daily_active_candidates.get("daily_amount"),
                errors="coerce",
            ).fillna(0.0)
            daily_active_candidates = daily_active_candidates.loc[(daily_volume > 0.0) | (daily_amount > 0.0)]
            intraday_dates = set(symbol_df["trade_date"].dropna().tolist())
            daily_missing_intraday = daily_active_candidates.loc[
                ~daily_active_candidates["trade_date"].isin(intraday_dates)
            ]
            if not daily_missing_intraday.empty:
                daily_active_missing_intraday_rows += int(len(daily_missing_intraday))
                for _, row in daily_missing_intraday.head(sample_limit).iterrows():
                    _append_sample(
                        sample_daily_active_missing_intraday_rows,
                        {
                            "symbol": symbol,
                            "trade_date": row["trade_date"],
                            "daily_volume": row.get("daily_volume"),
                            "daily_amount": row.get("daily_amount"),
                        },
                        limit=sample_limit,
                    )

        merged = symbol_df.merge(
            daily_frame,
            on="trade_date",
            how="left",
            sort=True,
        )
        missing_match_mask = merged["daily_open"].isna() & merged["daily_close"].isna()
        if bool(missing_match_mask.any()):
            daily_max_date = daily_frame["trade_date"].max()
            after_daily_end_mask = missing_match_mask & merged["trade_date"].notna() & (merged["trade_date"] > daily_max_date)
            inactive_zero_without_daily_mask = missing_match_mask & _zero_volume_amount_mask(merged)
            inactive_zero_mask = after_daily_end_mask & inactive_zero_without_daily_mask
            inactive_zero_missing_daily_mask = inactive_zero_without_daily_mask & ~after_daily_end_mask
            trading_after_end_mask = after_daily_end_mask & ~inactive_zero_mask
            unclassified_missing_mask = (
                missing_match_mask
                & ~inactive_zero_mask
                & ~inactive_zero_missing_daily_mask
                & ~trading_after_end_mask
            )

            if bool(inactive_zero_mask.any()):
                inactive_zero_volume_after_daily_end_rows += int(inactive_zero_mask.sum())
                for _, row in merged.loc[inactive_zero_mask].head(sample_limit).iterrows():
                    _append_sample(
                        sample_inactive_zero_volume_rows,
                        {
                            "symbol": symbol,
                            "trade_date": row["trade_date"],
                            "reason": "inactive_zero_volume_intraday_after_daily_end",
                            "intraday_volume": row.get("intraday_volume"),
                            "intraday_amount": row.get("intraday_amount"),
                        },
                        limit=sample_limit,
                    )

            if bool(inactive_zero_missing_daily_mask.any()):
                inactive_zero_volume_missing_daily_rows += int(inactive_zero_missing_daily_mask.sum())
                for _, row in merged.loc[inactive_zero_missing_daily_mask].head(sample_limit).iterrows():
                    _append_sample(
                        sample_inactive_zero_volume_missing_daily_rows,
                        {
                            "symbol": symbol,
                            "trade_date": row["trade_date"],
                            "reason": "inactive_zero_volume_intraday_missing_daily_row",
                            "intraday_volume": row.get("intraday_volume"),
                            "intraday_amount": row.get("intraday_amount"),
                        },
                        limit=sample_limit,
                    )

            if bool(trading_after_end_mask.any()):
                intraday_after_daily_end_with_trading_rows += int(trading_after_end_mask.sum())
                for _, row in merged.loc[trading_after_end_mask].head(sample_limit).iterrows():
                    _append_sample(
                        sample_intraday_after_daily_end_with_trading_rows,
                        {
                            "symbol": symbol,
                            "trade_date": row["trade_date"],
                            "reason": "intraday_after_daily_end_with_trading",
                            "intraday_volume": row.get("intraday_volume"),
                            "intraday_amount": row.get("intraday_amount"),
                        },
                        limit=sample_limit,
                    )

            missing_daily_rows += int(unclassified_missing_mask.sum())
            for _, row in merged.loc[unclassified_missing_mask].head(sample_limit).iterrows():
                _append_sample(
                    sample_missing_daily_rows,
                    {
                        "symbol": symbol,
                        "trade_date": row["trade_date"],
                        "reason": "daily_trade_date_missing",
                    },
                    limit=sample_limit,
                )

        matched = merged.loc[~missing_match_mask].copy()
        if matched.empty:
            continue
        reconciled_symbol_days += int(len(matched))
        exact_match_mask = _daily_reconciliation_exact_match_mask(
            matched,
            rtol=rtol,
            atol=atol,
        )

        for intraday_field, daily_field, issue_key in (
            ("intraday_open", "daily_open", "daily_open_mismatch"),
            ("intraday_high", "daily_high", "daily_high_mismatch"),
            ("intraday_low", "daily_low", "daily_low_mismatch"),
            ("intraday_close", "daily_close", "daily_close_mismatch"),
            ("intraday_volume", "daily_volume", "daily_volume_mismatch"),
            ("intraday_amount", "daily_amount", "daily_amount_mismatch"),
        ):
            mismatch_mask = _compare_numeric(
                matched[intraday_field],
                matched[daily_field],
                rtol=rtol,
                atol=atol,
            )
            suppressed_mask = _resolve_reconciliation_suppressed_mask(
                merged=matched,
                mismatch_mask=mismatch_mask,
                issue_key=issue_key,
                exact_match_mask=exact_match_mask,
            )
            if bool(suppressed_mask.any()):
                suppressed_mismatch_counts[issue_key] += int(suppressed_mask.sum())
                mismatch_mask = mismatch_mask & ~suppressed_mask
            count = int(mismatch_mask.sum())
            if count <= 0:
                continue
            mismatch_counts[issue_key] += count
            for _, row in matched.loc[mismatch_mask].head(sample_limit).iterrows():
                _append_sample(
                    sample_mismatch_rows,
                    {
                        "symbol": symbol,
                        "trade_date": row["trade_date"],
                        "field": issue_key,
                        "intraday_value": row[intraday_field],
                        "daily_value": row[daily_field],
                    },
                    limit=sample_limit,
                )

    return {
        "summary": {
            "daily_asset_dir": str(daily_asset_dir),
            "reconciled_symbol_days": reconciled_symbol_days,
            "missing_daily_symbol_days": missing_daily_rows,
            "inactive_zero_volume_intraday_after_daily_end_symbol_days": inactive_zero_volume_after_daily_end_rows,
            "inactive_zero_volume_intraday_missing_daily_row_symbol_days": inactive_zero_volume_missing_daily_rows,
            "intraday_after_daily_end_with_trading_symbol_days": intraday_after_daily_end_with_trading_rows,
            "daily_active_symbol_days_missing_intraday": daily_active_missing_intraday_rows,
            "mismatch_counts": dict(sorted(mismatch_counts.items())),
            "suppressed_mismatch_counts": dict(sorted(suppressed_mismatch_counts.items())),
            "intraday_adjust_type": intraday_adjust_type,
            "daily_adjust_type": daily_adjust_type,
            "price_adjustment_basis_mismatch": price_adjustment_basis_mismatch,
        },
        "sample_missing_daily_rows": sample_missing_daily_rows,
        "sample_inactive_zero_volume_intraday_after_daily_end": sample_inactive_zero_volume_rows,
        "sample_inactive_zero_volume_intraday_missing_daily_row": sample_inactive_zero_volume_missing_daily_rows,
        "sample_intraday_after_daily_end_with_trading": sample_intraday_after_daily_end_with_trading_rows,
        "sample_daily_active_missing_intraday_rows": sample_daily_active_missing_intraday_rows,
        "sample_mismatch_rows": sample_mismatch_rows,
    }


def _render_intraday_health_text(payload: Mapping[str, object]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    quality_checks = payload.get("quality_checks") if isinstance(payload.get("quality_checks"), list) else []
    quality_verdict = (
        payload.get("quality_verdict") if isinstance(payload.get("quality_verdict"), Mapping) else None
    )
    lines = ["HK Intraday Health"]
    for key in (
        "rows_scanned",
        "symbols_scanned",
        "symbol_days_scanned",
        "trade_date_min",
        "trade_date_max",
        "duplicate_timestamp_groups",
        "duplicate_timestamp_rows",
        "symbol_days_with_missing_bars",
        "missing_bar_rows",
        "negative_volume_rows",
        "negative_amount_rows",
        "daily_reconciliation_symbol_days",
        "daily_reconciliation_missing_daily_rows",
        "daily_reconciliation_inactive_zero_volume_after_daily_end_rows",
        "daily_reconciliation_inactive_zero_volume_missing_daily_rows",
        "daily_reconciliation_intraday_after_daily_end_with_trading_rows",
        "daily_reconciliation_daily_active_missing_intraday_rows",
    ):
        lines.append(f"{key}: {summary.get(key)}")

    if quality_checks:
        lines.append("")
        lines.append("Quality Checks")
        for row in quality_checks:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                "{severity}: {check} -> {affected} item(s), {pct}%".format(
                    severity=row.get("severity"),
                    check=row.get("check"),
                    affected=row.get("affected_items"),
                    pct=row.get("affected_pct"),
                )
            )
    append_quality_verdict_lines(lines, quality_verdict)
    return "\n".join(lines).strip() + "\n"


def _build_intraday_health_config(args) -> IntradayHealthConfig:
    input_specs = list(getattr(args, "input", []) or [])
    if not input_specs:
        raise SystemExit("At least one --input is required.")

    parquet_paths = resolve_input_parquet_paths(input_specs)
    sample_limit = max(1, int(getattr(args, "sample_limit", 5) or 5))
    expected_bars_per_day = max(1, int(getattr(args, "expected_bars_per_day", 66) or 66))
    numeric_rtol = float(getattr(args, "numeric_rtol", 1e-6) or 1e-6)
    numeric_atol = float(getattr(args, "numeric_atol", 1e-8) or 1e-8)
    daily_asset_dir_arg = getattr(args, "daily_asset_dir", None)
    daily_asset_dir = _resolve_path(daily_asset_dir_arg) if daily_asset_dir_arg else None
    intraday_adjust_type = _normalize_adjust_type(getattr(args, "intraday_adjust_type", None))
    if intraday_adjust_type is None:
        intraday_adjust_type = _infer_intraday_adjust_type(input_specs)
    daily_adjust_type = _normalize_adjust_type(getattr(args, "daily_adjust_type", None))
    if daily_adjust_type is None:
        daily_adjust_type = _manifest_query_adjust_type(daily_asset_dir)
    out_path = _resolve_path(args.out) if getattr(args, "out", None) else None
    return IntradayHealthConfig(
        input_specs=input_specs,
        parquet_paths=list(parquet_paths),
        sample_limit=sample_limit,
        expected_bars_per_day=expected_bars_per_day,
        numeric_rtol=numeric_rtol,
        numeric_atol=numeric_atol,
        daily_asset_dir=daily_asset_dir,
        intraday_adjust_type=intraday_adjust_type,
        daily_adjust_type=daily_adjust_type,
        fail_on_severity=str(getattr(args, "fail_on_severity", "none") or "none"),
        output_format=str(getattr(args, "format", "text") or "text").strip().lower(),
        out_path=out_path,
    )


def _scan_intraday_health_inputs(config: IntradayHealthConfig) -> IntradayHealthScan:
    duplicate_timestamp_groups = 0
    duplicate_timestamp_rows = 0
    missing_bar_symbol_days = 0
    missing_bar_rows = 0
    unexpected_bar_count_symbol_days = 0
    off_schedule_bar_rows = 0
    negative_volume_rows = 0
    negative_amount_rows = 0
    rows_scanned = 0
    symbols_seen: set[str] = set()
    trade_date_min: pd.Timestamp | None = None
    trade_date_max: pd.Timestamp | None = None
    bar_count_values: list[int] = []
    sample_duplicate_rows: list[dict[str, object]] = []
    sample_missing_symbol_days: list[dict[str, object]] = []
    sample_negative_rows: list[dict[str, object]] = []
    sample_unexpected_bar_count_symbol_days: list[dict[str, object]] = []
    sample_off_schedule_rows: list[dict[str, object]] = []
    daily_parts: list[pd.DataFrame] = []

    for parquet_path in config.parquet_paths:
        for frame in _read_intraday_frame_chunks(parquet_path):
            work = _normalize_intraday_frame(frame)
            if work.empty:
                continue
            rows_scanned += int(len(work))
            symbols_seen.update(work["symbol"].dropna().astype(str).tolist())
            part_date_min = work["trade_date"].min()
            part_date_max = work["trade_date"].max()
            trade_date_min = part_date_min if trade_date_min is None or part_date_min < trade_date_min else trade_date_min
            trade_date_max = part_date_max if trade_date_max is None or part_date_max > trade_date_max else trade_date_max

            duplicate_groups = (
                work.groupby(["symbol", "trade_datetime"], sort=True).size().rename("duplicate_rows").reset_index()
            )
            duplicate_groups = duplicate_groups.loc[duplicate_groups["duplicate_rows"] > 1].copy()
            duplicate_timestamp_groups += int(len(duplicate_groups))
            duplicate_timestamp_rows += int(duplicate_groups["duplicate_rows"].sum()) if not duplicate_groups.empty else 0
            for _, row in duplicate_groups.head(config.sample_limit).iterrows():
                _append_sample(
                    sample_duplicate_rows,
                    {
                        "symbol": row["symbol"],
                        "trade_datetime": pd.to_datetime(row["trade_datetime"]),
                        "duplicate_rows": row["duplicate_rows"],
                    },
                    limit=config.sample_limit,
                )

            negative_volume_mask = work["volume"].notna() & (work["volume"] < 0.0)
            negative_amount_mask = work["amount"].notna() & (work["amount"] < 0.0)
            negative_volume_rows += int(negative_volume_mask.sum())
            negative_amount_rows += int(negative_amount_mask.sum())
            for field_name, mask in (("volume", negative_volume_mask), ("amount", negative_amount_mask)):
                if not bool(mask.any()):
                    continue
                for _, row in work.loc[mask].head(config.sample_limit).iterrows():
                    _append_sample(
                        sample_negative_rows,
                        {
                            "symbol": row["symbol"],
                            "trade_datetime": row["trade_datetime"],
                            "field": field_name,
                            "value": row[field_name],
                        },
                        limit=config.sample_limit,
                    )

            off_schedule_mask = ~work["time_key"].isin(_EXPECTED_HK_5M_TIME_KEY_SET)
            off_schedule_bar_rows += int(off_schedule_mask.sum())
            if bool(off_schedule_mask.any()):
                for _, row in work.loc[off_schedule_mask].head(config.sample_limit).iterrows():
                    _append_sample(
                        sample_off_schedule_rows,
                        {
                            "symbol": row["symbol"],
                            "trade_datetime": row["trade_datetime"],
                            "time_key": row["time_key"],
                        },
                        limit=config.sample_limit,
                    )

            deduped = (
                work.drop_duplicates(subset=["symbol", "trade_datetime"], keep="last")
                .sort_values(["symbol", "trade_datetime"])
                .reset_index(drop=True)
            )
            daily_parts.append(_aggregate_intraday_daily(deduped))

    intraday_daily = _combine_intraday_daily_parts(daily_parts)
    inferred_half_day_dates = _infer_half_day_dates(intraday_daily)
    if not intraday_daily.empty:
        for _, row in intraday_daily.iterrows():
            observed_bars = int(row["intraday_bar_count"])
            bar_count_values.append(observed_bars)
            expected_lo_mask, expected_hi_mask, expected_bars_for_date = _expected_masks_for_date(
                row["trade_date"],
                inferred_half_day_dates,
                full_day_expected_bars=config.expected_bars_per_day,
            )
            if observed_bars != expected_bars_for_date:
                unexpected_bar_count_symbol_days += 1
                _append_sample(
                    sample_unexpected_bar_count_symbol_days,
                    {
                        "symbol": row["symbol"],
                        "trade_date": row["trade_date"],
                        "observed_bars": observed_bars,
                        "expected_bars": expected_bars_for_date,
                    },
                    limit=config.sample_limit,
                )

            missing_times = _missing_times_from_masks(
                int(row.get("intraday_time_mask_lo") or 0),
                int(row.get("intraday_time_mask_hi") or 0),
                expected_lo_mask=expected_lo_mask,
                expected_hi_mask=expected_hi_mask,
            )
            if missing_times:
                missing_bar_symbol_days += 1
                missing_bar_rows += int(len(missing_times))
                _append_sample(
                    sample_missing_symbol_days,
                    {
                        "symbol": row["symbol"],
                        "trade_date": row["trade_date"],
                        "observed_bars": observed_bars,
                        "missing_bars": len(missing_times),
                        "sample_missing_times": ",".join(missing_times[:5]),
                    },
                    limit=config.sample_limit,
                )

    return IntradayHealthScan(
        rows_scanned=rows_scanned,
        symbols_seen=symbols_seen,
        trade_date_min=trade_date_min,
        trade_date_max=trade_date_max,
        intraday_daily=intraday_daily,
        inferred_half_day_dates=inferred_half_day_dates,
        duplicate_timestamp_groups=duplicate_timestamp_groups,
        duplicate_timestamp_rows=duplicate_timestamp_rows,
        missing_bar_symbol_days=missing_bar_symbol_days,
        missing_bar_rows=missing_bar_rows,
        unexpected_bar_count_symbol_days=unexpected_bar_count_symbol_days,
        off_schedule_bar_rows=off_schedule_bar_rows,
        negative_volume_rows=negative_volume_rows,
        negative_amount_rows=negative_amount_rows,
        bar_count_values=bar_count_values,
        sample_duplicate_rows=sample_duplicate_rows,
        sample_missing_symbol_days=sample_missing_symbol_days,
        sample_negative_rows=sample_negative_rows,
        sample_unexpected_bar_count_symbol_days=sample_unexpected_bar_count_symbol_days,
        sample_off_schedule_rows=sample_off_schedule_rows,
    )


def _build_intraday_quality_checks(
    *,
    scan: IntradayHealthScan,
    reconciliation: Mapping[str, object] | None,
) -> tuple[list[dict[str, object]], Mapping[str, object]]:
    quality_checks: list[dict[str, object]] = []
    symbol_days_scanned = int(len(scan.intraday_daily))
    if scan.duplicate_timestamp_groups > 0:
        quality_checks.append(
            {
                "check": "duplicate_intraday_timestamps",
                "severity": "error",
                "affected_items": scan.duplicate_timestamp_groups,
                "affected_pct": _round_pct(scan.duplicate_timestamp_groups, symbol_days_scanned),
                "sample_rows": scan.sample_duplicate_rows,
            }
        )
    if scan.missing_bar_symbol_days > 0:
        quality_checks.append(
            {
                "check": "intraday_missing_bars_vs_expected_schedule",
                "severity": "warning",
                "affected_items": scan.missing_bar_symbol_days,
                "affected_pct": _round_pct(scan.missing_bar_symbol_days, symbol_days_scanned),
                "sample_rows": scan.sample_missing_symbol_days,
            }
        )
    if scan.unexpected_bar_count_symbol_days > 0:
        quality_checks.append(
            {
                "check": "intraday_unexpected_session_bar_count",
                "severity": "warning",
                "affected_items": scan.unexpected_bar_count_symbol_days,
                "affected_pct": _round_pct(
                    scan.unexpected_bar_count_symbol_days,
                    symbol_days_scanned,
                ),
                "sample_rows": scan.sample_unexpected_bar_count_symbol_days,
            }
        )
    if scan.negative_volume_rows > 0:
        quality_checks.append(
            {
                "check": "intraday_negative_volume_rows",
                "severity": "error",
                "affected_items": scan.negative_volume_rows,
                "affected_pct": _round_pct(scan.negative_volume_rows, scan.rows_scanned),
                "sample_rows": [
                    row for row in scan.sample_negative_rows if row.get("field") == "volume"
                ],
            }
        )
    if scan.negative_amount_rows > 0:
        quality_checks.append(
            {
                "check": "intraday_negative_amount_rows",
                "severity": "error",
                "affected_items": scan.negative_amount_rows,
                "affected_pct": _round_pct(scan.negative_amount_rows, scan.rows_scanned),
                "sample_rows": [
                    row for row in scan.sample_negative_rows if row.get("field") == "amount"
                ],
            }
        )
    if scan.off_schedule_bar_rows > 0:
        quality_checks.append(
            {
                "check": "intraday_off_schedule_bar_rows",
                "severity": "warning",
                "affected_items": scan.off_schedule_bar_rows,
                "affected_pct": _round_pct(scan.off_schedule_bar_rows, scan.rows_scanned),
                "sample_rows": scan.sample_off_schedule_rows,
            }
        )

    reconciliation_summary: Mapping[str, object] = {}
    if isinstance(reconciliation, Mapping):
        reconciliation_summary = (
            reconciliation.get("summary") if isinstance(reconciliation.get("summary"), Mapping) else {}
        )
        if int(reconciliation_summary.get("missing_daily_symbol_days") or 0) > 0:
            quality_checks.append(
                {
                    "check": "intraday_daily_rows_missing_from_asset",
                    "asset_key": "daily_clean",
                    "severity": "warning",
                    "affected_items": int(
                        reconciliation_summary.get("missing_daily_symbol_days") or 0
                    ),
                    "affected_pct": _round_pct(
                        int(reconciliation_summary.get("missing_daily_symbol_days") or 0),
                        symbol_days_scanned,
                    ),
                    "sample_rows": list(reconciliation.get("sample_missing_daily_rows") or []),
                }
            )
        inactive_zero_count = int(
            reconciliation_summary.get("inactive_zero_volume_intraday_after_daily_end_symbol_days") or 0
        )
        inactive_zero_missing_daily_count = int(
            reconciliation_summary.get("inactive_zero_volume_intraday_missing_daily_row_symbol_days") or 0
        )
        if inactive_zero_count > 0:
            quality_checks.append(
                {
                    "check": "inactive_zero_volume_intraday_after_daily_end",
                    "severity": "info",
                    "affected_items": inactive_zero_count,
                    "affected_pct": _round_pct(inactive_zero_count, symbol_days_scanned),
                    "classification": "provider-inactive-boundary",
                    "sample_rows": list(
                        reconciliation.get("sample_inactive_zero_volume_intraday_after_daily_end")
                        or []
                    ),
                }
            )
        if inactive_zero_missing_daily_count > 0:
            quality_checks.append(
                {
                    "check": "inactive_zero_volume_intraday_without_daily_row",
                    "severity": "info",
                    "affected_items": inactive_zero_missing_daily_count,
                    "affected_pct": _round_pct(
                        inactive_zero_missing_daily_count,
                        symbol_days_scanned,
                    ),
                    "classification": "provider-inactive-boundary",
                    "sample_rows": list(
                        reconciliation.get("sample_inactive_zero_volume_intraday_missing_daily_row")
                        or []
                    ),
                }
            )
        trading_after_end_count = int(
            reconciliation_summary.get("intraday_after_daily_end_with_trading_symbol_days") or 0
        )
        if trading_after_end_count > 0:
            quality_checks.append(
                {
                    "check": "intraday_after_daily_end_with_trading",
                    "asset_key": "daily_clean",
                    "severity": "warning",
                    "affected_items": trading_after_end_count,
                    "affected_pct": _round_pct(trading_after_end_count, symbol_days_scanned),
                    "sample_rows": list(
                        reconciliation.get("sample_intraday_after_daily_end_with_trading") or []
                    ),
                }
            )
        daily_active_missing_intraday_count = int(
            reconciliation_summary.get("daily_active_symbol_days_missing_intraday") or 0
        )
        if daily_active_missing_intraday_count > 0:
            quality_checks.append(
                {
                    "check": "daily_active_but_intraday_missing",
                    "asset_key": "intraday",
                    "severity": "warning",
                    "affected_items": daily_active_missing_intraday_count,
                    "affected_pct": _round_pct(
                        daily_active_missing_intraday_count,
                        symbol_days_scanned,
                    ),
                    "sample_rows": list(
                        reconciliation.get("sample_daily_active_missing_intraday_rows") or []
                    ),
                }
            )
        mismatch_counts = (
            reconciliation_summary.get("mismatch_counts")
            if isinstance(reconciliation_summary.get("mismatch_counts"), Mapping)
            else {}
        )
        price_basis_mismatch = bool(
            reconciliation_summary.get("price_adjustment_basis_mismatch")
        )
        for field_name, severity in (
            ("daily_open_mismatch", "warning"),
            ("daily_high_mismatch", "warning"),
            ("daily_low_mismatch", "warning"),
            ("daily_close_mismatch", "warning"),
            ("daily_volume_mismatch", "warning"),
            ("daily_amount_mismatch", "warning"),
        ):
            affected = int(mismatch_counts.get(field_name) or 0)
            if affected <= 0:
                continue
            check_severity = severity
            classification = None
            if field_name in _PRICE_RECONCILIATION_ISSUES and price_basis_mismatch:
                check_severity = "info"
                classification = "adjustment-basis-mismatch"
            quality_checks.append(
                {
                    "check": field_name,
                    "severity": check_severity,
                    "affected_items": affected,
                    "affected_pct": _round_pct(
                        affected,
                        int(reconciliation_summary.get("reconciled_symbol_days") or 0),
                    ),
                    "sample_rows": [
                        row
                        for row in (reconciliation.get("sample_mismatch_rows") or [])
                        if row.get("field") == field_name
                    ],
                    **({"classification": classification} if classification else {}),
                }
            )
    return quality_checks, reconciliation_summary


def _build_intraday_health_summary(
    *,
    config: IntradayHealthConfig,
    scan: IntradayHealthScan,
    reconciliation_summary: Mapping[str, object],
    quality_checks: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "input_count": len(config.input_specs),
        "parquet_files_scanned": len(config.parquet_paths),
        "rows_scanned": scan.rows_scanned,
        "symbols_scanned": len(scan.symbols_seen),
        "symbol_days_scanned": int(len(scan.intraday_daily)),
        "trade_date_min": _format_date(scan.trade_date_min),
        "trade_date_max": _format_date(scan.trade_date_max),
        "expected_bars_per_day": config.expected_bars_per_day,
        "intraday_adjust_type": config.intraday_adjust_type,
        "daily_adjust_type": config.daily_adjust_type,
        "daily_reconciliation_price_adjustment_basis_mismatch": bool(
            reconciliation_summary.get("price_adjustment_basis_mismatch")
        ),
        "inferred_half_day_dates": sorted(scan.inferred_half_day_dates),
        "inferred_half_day_count": len(scan.inferred_half_day_dates),
        "duplicate_timestamp_groups": scan.duplicate_timestamp_groups,
        "duplicate_timestamp_rows": scan.duplicate_timestamp_rows,
        "symbol_days_with_missing_bars": scan.missing_bar_symbol_days,
        "missing_bar_rows": scan.missing_bar_rows,
        "off_schedule_bar_rows": scan.off_schedule_bar_rows,
        "bar_count_min": min(scan.bar_count_values) if scan.bar_count_values else None,
        "bar_count_p50": _quantile_or_none(scan.bar_count_values, 0.5),
        "bar_count_p90": _quantile_or_none(scan.bar_count_values, 0.9),
        "bar_count_max": max(scan.bar_count_values) if scan.bar_count_values else None,
        "symbol_days_with_unexpected_bar_count": scan.unexpected_bar_count_symbol_days,
        "negative_volume_rows": scan.negative_volume_rows,
        "negative_amount_rows": scan.negative_amount_rows,
        "daily_reconciliation_symbol_days": int(
            reconciliation_summary.get("reconciled_symbol_days") or 0
        ),
        "daily_reconciliation_missing_daily_rows": int(
            reconciliation_summary.get("missing_daily_symbol_days") or 0
        ),
        "daily_reconciliation_inactive_zero_volume_after_daily_end_rows": int(
            reconciliation_summary.get(
                "inactive_zero_volume_intraday_after_daily_end_symbol_days"
            )
            or 0
        ),
        "daily_reconciliation_inactive_zero_volume_missing_daily_rows": int(
            reconciliation_summary.get(
                "inactive_zero_volume_intraday_missing_daily_row_symbol_days"
            )
            or 0
        ),
        "daily_reconciliation_intraday_after_daily_end_with_trading_rows": int(
            reconciliation_summary.get("intraday_after_daily_end_with_trading_symbol_days")
            or 0
        ),
        "daily_reconciliation_daily_active_missing_intraday_rows": int(
            reconciliation_summary.get("daily_active_symbol_days_missing_intraday") or 0
        ),
        "quality_check_issue_count": len(quality_checks),
    }


def _build_intraday_health_payload(
    *,
    config: IntradayHealthConfig,
    scan: IntradayHealthScan,
    reconciliation: Mapping[str, object] | None,
) -> dict[str, object]:
    quality_checks, reconciliation_summary = _build_intraday_quality_checks(
        scan=scan,
        reconciliation=reconciliation,
    )
    summary = _build_intraday_health_summary(
        config=config,
        scan=scan,
        reconciliation_summary=reconciliation_summary,
        quality_checks=quality_checks,
    )
    quality_verdict = summarize_quality_checks(
        quality_checks,
        fail_on_severity=config.fail_on_severity,
    )
    return {
        "summary": summary,
        "quality_verdict": quality_verdict,
        "sample_duplicate_timestamps": scan.sample_duplicate_rows,
        "sample_missing_symbol_days": scan.sample_missing_symbol_days,
        "sample_negative_rows": scan.sample_negative_rows,
        "sample_unexpected_bar_count_symbol_days": scan.sample_unexpected_bar_count_symbol_days,
        "sample_off_schedule_rows": scan.sample_off_schedule_rows,
        "daily_reconciliation": reconciliation,
        "quality_checks": quality_checks,
    }


def _render_intraday_health_payload(
    payload: Mapping[str, object],
    *,
    output_format: str,
) -> str:
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return _render_intraday_health_text(payload)


def _write_or_print_intraday_health(rendered: str, out_path: Path | None) -> None:
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


def inspect_hk_intraday_health(args) -> int:
    config = _build_intraday_health_config(args)
    scan = _scan_intraday_health_inputs(config)
    reconciliation = None
    if config.daily_asset_dir is not None:
        reconciliation = _build_daily_reconciliation(
            intraday_daily=scan.intraday_daily,
            daily_asset_dir=config.daily_asset_dir,
            sample_limit=config.sample_limit,
            rtol=config.numeric_rtol,
            atol=config.numeric_atol,
            intraday_adjust_type=config.intraday_adjust_type,
            daily_adjust_type=config.daily_adjust_type,
        )
    payload = _build_intraday_health_payload(
        config=config,
        scan=scan,
        reconciliation=reconciliation,
    )
    rendered = _render_intraday_health_payload(payload, output_format=config.output_format)
    _write_or_print_intraday_health(rendered, config.out_path)
    verdict = payload["quality_verdict"]
    return quality_gate_exit_code(verdict if isinstance(verdict, Mapping) else {})
