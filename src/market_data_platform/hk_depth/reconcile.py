"""Tick-vs-daily reconciliation checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from market_data_platform.hk_depth.quality import (
    append_quality_check,
    quote_ladder_invalid,
    sample_frame,
    session_phase_counts,
)
from market_data_platform.hk_depth.quality import (
    numeric as quality_numeric,
)
from market_data_platform.hk_depth.quality import (
    quality_verdict as shared_quality_verdict,
)
from market_data_platform.hk_depth.storage import discover_parquet_parts, write_json
from market_data_platform.hk_depth.symbols import normalize_hk_order_book_id

REFERENCE_POLICIES = ("raw-daily", "cross-clean")
ECONOMIC_MISMATCH_CHECKS = {
    "tick_close_mismatch",
    "tick_volume_mismatch",
    "tick_turnover_mismatch",
}


def _normalize_reference_policy(value: str) -> str:
    policy = value.strip().lower()
    if policy not in REFERENCE_POLICIES:
        raise ValueError("reference_policy must be one of: raw-daily, cross-clean.")
    return policy


def _policy_check_severity(*, check: str, severity: str, reference_policy: str) -> str:
    if reference_policy == "cross-clean" and check in ECONOMIC_MISMATCH_CHECKS:
        return "info"
    return severity


def _reference_policy_metadata(policy: str) -> dict[str, Any]:
    if policy == "cross-clean":
        return {
            "name": policy,
            "gate_reference": "raw-daily",
            "research_reference": "cross-clean",
            "description": (
                "Use cross daily clean assets for research coverage checks. Numeric tick-vs-daily "
                "mismatches are recorded as info because clean/adjusted prices may not share the "
                "raw depth snapshot quote basis."
            ),
            "numeric_mismatch_severity": "info",
        }
    return {
        "name": policy,
        "gate_reference": "raw-daily",
        "research_reference": "cross-clean",
        "description": (
            "Use a raw daily reference on the same quote basis as adjust_type=none ticks for "
            "download-quality gates."
        ),
        "numeric_mismatch_severity": "warning",
    }


@dataclass(frozen=True)
class ReconcileConfig:
    price_rtol: float = 1e-4
    price_atol: float = 1e-4
    volume_rtol: float = 1e-4
    volume_atol: float = 1.0
    turnover_rtol: float = 1e-4
    turnover_atol: float = 1.0
    session_start: str = "09:00"
    session_end: str = "16:30"
    sample_limit: int = 20
    fail_on_severity: str = "error"
    reference_policy: str = "raw-daily"

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_rtol": self.price_rtol,
            "price_atol": self.price_atol,
            "volume_rtol": self.volume_rtol,
            "volume_atol": self.volume_atol,
            "turnover_rtol": self.turnover_rtol,
            "turnover_atol": self.turnover_atol,
            "session_start": self.session_start,
            "session_end": self.session_end,
            "sample_limit": self.sample_limit,
            "fail_on_severity": self.fail_on_severity,
            "reference_policy": _normalize_reference_policy(self.reference_policy),
        }


def canonical_daily_symbol(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        normalized = normalize_hk_order_book_id(text)
    except ValueError:
        return text.upper()
    return normalized.removesuffix(".XHKG") + ".HK"


def canonical_tick_symbol(value: object) -> str | None:
    daily = canonical_daily_symbol(value)
    if daily is None:
        return None
    if daily.endswith(".HK") and daily[:5].isdigit():
        return f"{daily[:5]}.XHKG"
    return daily


def _format_date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    compact = text.replace("-", "")
    if len(compact) >= 8 and compact[:8].isdigit():
        return compact[:8]
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.strftime("%Y%m%d")


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    return quality_numeric(frame, column)


def _session_time(value: str) -> time:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return time(parts[0], parts[1])
    if len(parts) == 3:
        return time(parts[0], parts[1], parts[2])
    raise ValueError("Session time must be HH:MM or HH:MM:SS.")


def _quality_verdict(
    checks: list[dict[str, Any]],
    *,
    fail_on_severity: str,
) -> dict[str, Any]:
    return shared_quality_verdict(checks, fail_on_severity=fail_on_severity)


def _sample(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    return sample_frame(frame, limit)


def _append_check(
    checks: list[dict[str, Any]],
    *,
    check: str,
    severity: str,
    message: str,
    affected: int,
    samples: pd.DataFrame | None = None,
    sample_limit: int = 20,
    **extra: Any,
) -> None:
    append_quality_check(
        checks,
        check=check,
        severity=severity,
        message=message,
        affected=affected,
        samples=samples,
        sample_limit=sample_limit,
        **extra,
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "manifest.yml"
    if not manifest_path.exists():
        return {}
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"manifest_path": str(manifest_path), "manifest_error": str(exc)}
    if isinstance(payload, dict):
        return {"manifest_path": str(manifest_path), **payload}
    return {"manifest_path": str(manifest_path)}


def normalize_tick_for_reconciliation(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    if work.empty:
        return work
    if "order_book_id" not in work.columns and "symbol" in work.columns:
        work["order_book_id"] = work["symbol"]
    if "order_book_id" not in work.columns:
        work["order_book_id"] = pd.NA
    if "datetime" not in work.columns and "trade_datetime" in work.columns:
        work["datetime"] = work["trade_datetime"]
    if "datetime" in work.columns:
        work["_timestamp"] = pd.to_datetime(work["datetime"], errors="coerce")
    else:
        work["_timestamp"] = pd.Series(pd.NaT, index=work.index)
    if "trading_date" not in work.columns:
        work["trading_date"] = work["_timestamp"].map(_format_date)
    else:
        work["trading_date"] = work["trading_date"].map(_format_date)
    work["symbol_key"] = work["order_book_id"].map(canonical_daily_symbol)
    return work


def _timestamp_iso(value: object) -> str | None:
    if isinstance(value, pd.Series):
        value = value.iloc[0] if not value.empty else None
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _final_cumulative_source(group: pd.DataFrame, column: str) -> dict[str, Any]:
    values = pd.to_numeric(group[column], errors="coerce") if column in group.columns else None
    if values is None or values.dropna().empty:
        return {
            "value": None,
            "used_fallback": False,
            "source": "missing",
            "timestamp": None,
        }
    last_raw = values.iloc[-1]
    if pd.notna(last_raw):
        timestamp = group["_timestamp"].iloc[-1] if "_timestamp" in group.columns else None
        return {
            "value": float(last_raw),
            "used_fallback": False,
            "source": "final",
            "timestamp": _timestamp_iso(timestamp),
        }
    fallback = values.max(skipna=True)
    if pd.isna(fallback):
        return {
            "value": None,
            "used_fallback": False,
            "source": "missing",
            "timestamp": None,
        }
    max_index = values.idxmax()
    timestamp = group.loc[max_index, "_timestamp"] if "_timestamp" in group.columns else None
    if isinstance(timestamp, pd.Series):
        timestamp = timestamp.iloc[0]
    return {
        "value": float(fallback),
        "used_fallback": True,
        "source": "max_fallback",
        "timestamp": _timestamp_iso(timestamp),
    }


def aggregate_tick_ohlcv(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    work = normalize_tick_for_reconciliation(df)
    metadata: dict[str, Any] = {
        "source_rows": int(len(df)),
        "timestamp_parse_failure_count": 0,
        "close_missing_count": 0,
        "volume_fallback_count": 0,
        "turnover_fallback_count": 0,
        "close_source_counts": {},
        "volume_source_counts": {},
        "turnover_source_counts": {},
    }
    if work.empty:
        return pd.DataFrame(), metadata
    metadata["timestamp_parse_failure_count"] = int(work["_timestamp"].isna().sum())
    work = work.dropna(subset=["symbol_key", "trading_date"]).copy()
    if work.empty:
        return pd.DataFrame(), metadata

    rows: list[dict[str, Any]] = []
    for (symbol_key, trade_date), group in work.groupby(
        ["symbol_key", "trading_date"],
        sort=True,
        dropna=False,
    ):
        ordered = group.sort_values("_timestamp", na_position="last").copy()
        last = _numeric(ordered, "last")
        valid_last = last.dropna()
        high_source = _numeric(ordered, "high").dropna()
        low_source = _numeric(ordered, "low").dropna()
        if high_source.empty:
            high_source = valid_last
        if low_source.empty:
            low_source = valid_last
        close_source = "last_valid_tick" if not valid_last.empty else "missing"
        close_timestamp = None
        if not valid_last.empty and "_timestamp" in ordered.columns:
            close_timestamp = _timestamp_iso(ordered.loc[valid_last.index[-1], "_timestamp"])
        volume_source = _final_cumulative_source(ordered, "volume")
        turnover_source = _final_cumulative_source(ordered, "total_turnover")
        volume = volume_source["value"]
        turnover = turnover_source["value"]
        volume_fallback = bool(volume_source["used_fallback"])
        turnover_fallback = bool(turnover_source["used_fallback"])
        metadata["close_missing_count"] += int(close_source == "missing")
        metadata["volume_fallback_count"] += int(volume_fallback)
        metadata["turnover_fallback_count"] += int(turnover_fallback)
        metadata["close_source_counts"][close_source] = (
            int(metadata["close_source_counts"].get(close_source, 0)) + 1
        )
        metadata["volume_source_counts"][str(volume_source["source"])] = (
            int(metadata["volume_source_counts"].get(str(volume_source["source"]), 0)) + 1
        )
        metadata["turnover_source_counts"][str(turnover_source["source"])] = (
            int(metadata["turnover_source_counts"].get(str(turnover_source["source"]), 0)) + 1
        )
        row = {
            "symbol_key": symbol_key,
            "order_book_id": canonical_tick_symbol(symbol_key),
            "trading_date": trade_date,
            "tick_open": float(valid_last.iloc[0]) if not valid_last.empty else pd.NA,
            "tick_high": float(high_source.max()) if not high_source.empty else pd.NA,
            "tick_low": float(low_source.min()) if not low_source.empty else pd.NA,
            "tick_close": float(valid_last.iloc[-1]) if not valid_last.empty else pd.NA,
            "tick_volume": volume if volume is not None else pd.NA,
            "tick_total_turnover": turnover if turnover is not None else pd.NA,
            "tick_first_timestamp": ordered["_timestamp"].min(),
            "tick_last_timestamp": ordered["_timestamp"].max(),
            "tick_row_count": int(len(ordered)),
            "tick_close_source": close_source,
            "tick_close_timestamp": close_timestamp,
            "volume_used_fallback": bool(volume_fallback),
            "volume_source": volume_source["source"],
            "volume_source_timestamp": volume_source["timestamp"],
            "turnover_used_fallback": bool(turnover_fallback),
            "turnover_source": turnover_source["source"],
            "turnover_source_timestamp": turnover_source["timestamp"],
        }
        rows.append(row)
    return pd.DataFrame(rows), metadata


def _empty_tick_aggregate_metadata() -> dict[str, Any]:
    return {
        "source_rows": 0,
        "source_parts": 0,
        "timestamp_parse_failure_count": 0,
        "close_missing_count": 0,
        "volume_fallback_count": 0,
        "turnover_fallback_count": 0,
        "close_source_counts": {},
        "volume_source_counts": {},
        "turnover_source_counts": {},
    }


def _merge_count_map(target: dict[str, Any], source: dict[str, Any], key: str) -> None:
    target_counts = target.setdefault(key, {})
    for name, count in (source.get(key) or {}).items():
        target_counts[str(name)] = int(target_counts.get(str(name), 0)) + int(count)


def aggregate_tick_ohlcv_parts(tick_input: str | Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    parts = discover_parquet_parts(tick_input)
    metadata = _empty_tick_aggregate_metadata()
    metadata["source_parts"] = len(parts)
    rows: list[pd.DataFrame] = []
    seen_units: set[tuple[str, str]] = set()
    duplicate_units: set[tuple[str, str]] = set()

    for part in parts:
        frame = pd.read_parquet(part)
        aggregate, part_meta = aggregate_tick_ohlcv(frame)
        metadata["source_rows"] += int(part_meta["source_rows"])
        metadata["timestamp_parse_failure_count"] += int(
            part_meta["timestamp_parse_failure_count"]
        )
        metadata["close_missing_count"] += int(part_meta["close_missing_count"])
        metadata["volume_fallback_count"] += int(part_meta["volume_fallback_count"])
        metadata["turnover_fallback_count"] += int(part_meta["turnover_fallback_count"])
        _merge_count_map(metadata, part_meta, "close_source_counts")
        _merge_count_map(metadata, part_meta, "volume_source_counts")
        _merge_count_map(metadata, part_meta, "turnover_source_counts")
        if aggregate.empty:
            continue
        part_units = {
            (str(row.symbol_key), str(row.trading_date))
            for row in aggregate.loc[:, ["symbol_key", "trading_date"]].itertuples(
                index=False
            )
        }
        duplicate_units.update(seen_units & part_units)
        seen_units.update(part_units)
        rows.append(aggregate)

    if duplicate_units:
        sample = ", ".join(f"{symbol}/{date}" for symbol, date in sorted(duplicate_units)[:5])
        raise ValueError(
            "Cannot stream reconcile duplicate symbol-date units split across parquet parts: "
            f"{sample}."
        )

    if not rows:
        return pd.DataFrame(), metadata
    output = pd.concat(rows, ignore_index=True).sort_values(
        ["symbol_key", "trading_date"],
        ignore_index=True,
    )
    return output, metadata


def _normalize_daily_reference(frame: pd.DataFrame, *, source_symbol: str | None) -> pd.DataFrame:
    work = frame.copy()
    if work.empty:
        return work
    date_col = next(
        (column for column in ("trade_date", "trading_date", "date") if column in work.columns),
        None,
    )
    if date_col is None:
        raise ValueError("Daily reference frame is missing trade_date/trading_date/date.")
    if "symbol" not in work.columns:
        work["symbol"] = source_symbol
    if "order_book_id" not in work.columns:
        work["order_book_id"] = work["symbol"].map(canonical_tick_symbol)
    work["trading_date"] = work[date_col].map(_format_date)
    symbol_source = work["symbol"].where(work["symbol"].notna(), work["order_book_id"])
    work["symbol_key"] = symbol_source.map(canonical_daily_symbol)
    columns = [
        "symbol_key",
        "symbol",
        "order_book_id",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "total_turnover",
    ]
    for column in columns:
        if column not in work.columns:
            work[column] = pd.NA
    return work[columns].dropna(subset=["symbol_key", "trading_date"])


def load_daily_reference(
    daily_asset_dir: str | Path,
    *,
    symbol_keys: set[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    root = Path(daily_asset_dir)
    manifest = _read_manifest(root) if root.is_dir() else {}
    if root.is_file():
        frame = _normalize_daily_reference(pd.read_parquet(root), source_symbol=root.stem)
        return frame, {"path": str(root), "manifest": manifest, "files_read": [str(root)]}

    data_root = root / "data" if (root / "data").exists() else root
    parts: list[Path] = []
    if symbol_keys:
        for symbol in sorted(symbol_keys):
            part = data_root / f"{symbol}.parquet"
            if part.exists():
                parts.append(part)
    if not parts:
        parts = discover_parquet_parts(data_root)

    frames = []
    for part in parts:
        source_symbol = part.stem if part.parent == data_root else None
        frames.append(
            _normalize_daily_reference(
                pd.read_parquet(part),
                source_symbol=source_symbol,
            )
        )
    daily = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    metadata = {"path": str(root), "manifest": manifest, "files_read": [str(p) for p in parts]}
    return daily, metadata


def _within_tolerance(
    left: pd.Series,
    right: pd.Series,
    *,
    rtol: float,
    atol: float,
) -> pd.Series:
    delta = (left - right).abs()
    allowed = pd.Series(atol, index=left.index, dtype="float64").where(
        right.abs().isna(),
        right.abs() * rtol + atol,
    )
    return delta <= allowed


def _ohlc_invalid(frame: pd.DataFrame, prefix: str = "") -> pd.Series:
    open_col = f"{prefix}open" if prefix else "open"
    high_col = f"{prefix}high" if prefix else "high"
    low_col = f"{prefix}low" if prefix else "low"
    close_col = f"{prefix}close" if prefix else "close"
    values = {
        column: _numeric(frame, column)
        for column in (open_col, high_col, low_col, close_col)
    }
    high = values[high_col]
    low = values[low_col]
    maximum = pd.concat([values[open_col], low, values[close_col]], axis=1).max(axis=1)
    minimum = pd.concat([values[open_col], high, values[close_col]], axis=1).min(axis=1)
    return (high < maximum) | (low > minimum) | (high < low)


def _quote_ladder_invalid(work: pd.DataFrame) -> pd.Series:
    return quote_ladder_invalid(work)


def _extend_sample_records(
    records: list[dict[str, Any]],
    frame: pd.DataFrame,
    columns: list[str],
    *,
    limit: int,
) -> None:
    remaining = limit - len(records)
    if remaining <= 0 or frame.empty:
        return
    records.extend(_sample(frame.loc[:, columns], remaining))


def _inspect_raw_reconciliation_inputs(
    tick_input: str | Path,
    *,
    config: ReconcileConfig,
    session_start: time,
    session_end: time,
) -> dict[str, Any]:
    phase_counts = dict.fromkeys(session_phase_counts(pd.Series(dtype="datetime64[ns]")), 0)
    parse_failure_samples: list[dict[str, Any]] = []
    session_outlier_samples: list[dict[str, Any]] = []
    quote_invalid_samples: list[dict[str, Any]] = []
    parse_failures = 0
    session_outliers = 0
    quote_invalid_count = 0

    for part in discover_parquet_parts(tick_input):
        frame = pd.read_parquet(part)
        normalized = normalize_tick_for_reconciliation(frame)
        if normalized.empty:
            continue
        part_phase_counts = session_phase_counts(normalized["_timestamp"])
        for phase, count in part_phase_counts.items():
            phase_counts[phase] = int(phase_counts.get(phase, 0)) + int(count)

        parse_mask = normalized["_timestamp"].isna()
        parse_failures += int(parse_mask.sum())
        parse_columns = [column for column in ("order_book_id", "datetime") if column in normalized]
        _extend_sample_records(
            parse_failure_samples,
            normalized.loc[parse_mask],
            parse_columns,
            limit=config.sample_limit,
        )

        times = normalized["_timestamp"].dt.time
        outlier_mask = normalized["_timestamp"].notna() & (
            (times < session_start) | (times > session_end)
        )
        session_outliers += int(outlier_mask.sum())
        session_columns = [
            column
            for column in ("order_book_id", "trading_date", "datetime")
            if column in normalized
        ]
        _extend_sample_records(
            session_outlier_samples,
            normalized.loc[outlier_mask],
            session_columns,
            limit=config.sample_limit,
        )

        quote_invalid = _quote_ladder_invalid(normalized)
        quote_invalid_count += int(quote_invalid.sum())
        quote_columns = [
            column
            for column in ("order_book_id", "trading_date", "datetime", "a1", "b1", "a2", "b2")
            if column in normalized.columns
        ]
        _extend_sample_records(
            quote_invalid_samples,
            normalized.loc[quote_invalid],
            quote_columns,
            limit=config.sample_limit,
        )

    return {
        "session_phase_counts": phase_counts,
        "timestamp_parse_failures": parse_failures,
        "timestamp_parse_failure_samples": parse_failure_samples,
        "session_time_outliers": session_outliers,
        "session_time_outlier_samples": session_outlier_samples,
        "quote_ladder_invalid": quote_invalid_count,
        "quote_ladder_invalid_samples": quote_invalid_samples,
    }


def inspect_tick_daily_reconciliation(
    tick_input: str | Path,
    daily_asset_dir: str | Path,
    *,
    config: ReconcileConfig | None = None,
) -> dict[str, Any]:
    cfg = config or ReconcileConfig()
    reference_policy = _normalize_reference_policy(cfg.reference_policy)
    session_start = _session_time(cfg.session_start)
    session_end = _session_time(cfg.session_end)
    tick_daily, aggregate_meta = aggregate_tick_ohlcv_parts(tick_input)
    symbol_keys = set(tick_daily.get("symbol_key", pd.Series(dtype="object")).dropna().astype(str))
    daily, reference_meta = load_daily_reference(daily_asset_dir, symbol_keys=symbol_keys)
    checks: list[dict[str, Any]] = []

    if int(aggregate_meta["source_rows"]) == 0:
        _append_check(
            checks,
            check="empty_tick_dataset",
            severity="error",
            message="Raw tick dataset has no rows.",
            affected=1,
            sample_limit=cfg.sample_limit,
        )

    if int(aggregate_meta["source_rows"]) > 0:
        raw_checks = _inspect_raw_reconciliation_inputs(
            tick_input,
            config=cfg,
            session_start=session_start,
            session_end=session_end,
        )
        aggregate_meta["session_phase_counts"] = raw_checks["session_phase_counts"]
        _append_check(
            checks,
            check="timestamp_parse_failures",
            severity="warning",
            message="Some tick timestamps could not be parsed.",
            affected=int(raw_checks["timestamp_parse_failures"]),
            samples=pd.DataFrame(raw_checks["timestamp_parse_failure_samples"]),
            sample_limit=cfg.sample_limit,
        )
        _append_check(
            checks,
            check="session_time_outlier",
            severity="warning",
            message="Tick timestamps fall outside the accepted HK tick session window.",
            affected=int(raw_checks["session_time_outliers"]),
            samples=pd.DataFrame(raw_checks["session_time_outlier_samples"]),
            sample_limit=cfg.sample_limit,
        )
        _append_check(
            checks,
            check="quote_ladder_invalid",
            severity="warning",
            message="Quote depth ladder or quote volume rules were violated.",
            affected=int(raw_checks["quote_ladder_invalid"]),
            samples=pd.DataFrame(raw_checks["quote_ladder_invalid_samples"]),
            sample_limit=cfg.sample_limit,
        )

    if not tick_daily.empty:
        tick_invalid = _ohlc_invalid(tick_daily, prefix="tick_")
        _append_check(
            checks,
            check="tick_ohlc_bounds_invalid",
            severity="warning",
            message="Tick-derived OHLC values violate high/low/open/close bounds.",
            affected=int(tick_invalid.sum()),
            samples=tick_daily.loc[tick_invalid],
            sample_limit=cfg.sample_limit,
        )
    if not daily.empty:
        daily_invalid = _ohlc_invalid(daily)
        _append_check(
            checks,
            check="daily_ohlc_bounds_invalid",
            severity="warning",
            message="Daily reference OHLC values violate high/low/open/close bounds.",
            affected=int(daily_invalid.sum()),
            samples=daily.loc[daily_invalid],
            sample_limit=cfg.sample_limit,
        )

    if not tick_daily.empty and not daily.empty:
        date_min = str(tick_daily["trading_date"].min())
        date_max = str(tick_daily["trading_date"].max())
        daily_scoped = daily.loc[
            daily["symbol_key"].isin(symbol_keys)
            & (daily["trading_date"].astype(str) >= date_min)
            & (daily["trading_date"].astype(str) <= date_max)
        ].copy()
        merged = daily_scoped.merge(
            tick_daily,
            on=["symbol_key", "trading_date"],
            how="outer",
            suffixes=("_daily", "_tick"),
            indicator=True,
        )
        daily_active = (_numeric(merged, "volume") > 0) | (_numeric(merged, "total_turnover") > 0)
        missing_tick = (merged["_merge"] == "left_only") & daily_active
        _append_check(
            checks,
            check="daily_active_missing_tick",
            severity="warning",
            message="Daily reference has positive volume or turnover but no matching tick rows.",
            affected=int(missing_tick.sum()),
            samples=merged.loc[
                missing_tick,
                ["symbol_key", "trading_date", "volume", "total_turnover"],
            ],
            sample_limit=cfg.sample_limit,
        )

        matched = merged.loc[merged["_merge"] == "both"].copy()
        if not matched.empty:
            close_ok = _within_tolerance(
                _numeric(matched, "tick_close"),
                _numeric(matched, "close"),
                rtol=cfg.price_rtol,
                atol=cfg.price_atol,
            )
            close_bad = (
                ~close_ok
                & _numeric(matched, "tick_close").notna()
                & _numeric(matched, "close").notna()
            )
            matched["close_delta"] = _numeric(matched, "tick_close") - _numeric(matched, "close")
            _append_check(
                checks,
                check="tick_close_mismatch",
                severity=_policy_check_severity(
                    check="tick_close_mismatch",
                    severity="warning",
                    reference_policy=reference_policy,
                ),
                message="Tick close differs from daily close beyond tolerance.",
                affected=int(close_bad.sum()),
                samples=matched.loc[
                    close_bad,
                    ["symbol_key", "trading_date", "tick_close", "close", "close_delta"],
                ],
                sample_limit=cfg.sample_limit,
            )

            volume_ok = _within_tolerance(
                _numeric(matched, "tick_volume"),
                _numeric(matched, "volume"),
                rtol=cfg.volume_rtol,
                atol=cfg.volume_atol,
            )
            volume_bad = (
                ~volume_ok
                & _numeric(matched, "tick_volume").notna()
                & _numeric(matched, "volume").notna()
            )
            matched["volume_delta"] = _numeric(matched, "tick_volume") - _numeric(matched, "volume")
            _append_check(
                checks,
                check="tick_volume_mismatch",
                severity=_policy_check_severity(
                    check="tick_volume_mismatch",
                    severity="warning",
                    reference_policy=reference_policy,
                ),
                message="Tick cumulative volume differs from daily volume beyond tolerance.",
                affected=int(volume_bad.sum()),
                samples=matched.loc[
                    volume_bad,
                    ["symbol_key", "trading_date", "tick_volume", "volume", "volume_delta"],
                ],
                sample_limit=cfg.sample_limit,
            )

            turnover_ok = _within_tolerance(
                _numeric(matched, "tick_total_turnover"),
                _numeric(matched, "total_turnover"),
                rtol=cfg.turnover_rtol,
                atol=cfg.turnover_atol,
            )
            turnover_bad = (
                ~turnover_ok
                & _numeric(matched, "tick_total_turnover").notna()
                & _numeric(matched, "total_turnover").notna()
            )
            matched["turnover_delta"] = _numeric(matched, "tick_total_turnover") - _numeric(
                matched,
                "total_turnover",
            )
            _append_check(
                checks,
                check="tick_turnover_mismatch",
                severity=_policy_check_severity(
                    check="tick_turnover_mismatch",
                    severity="warning",
                    reference_policy=reference_policy,
                ),
                message=(
                    "Tick cumulative total_turnover differs from daily total_turnover "
                    "beyond tolerance."
                ),
                affected=int(turnover_bad.sum()),
                samples=matched.loc[
                    turnover_bad,
                    [
                        "symbol_key",
                        "trading_date",
                        "tick_total_turnover",
                        "total_turnover",
                        "turnover_delta",
                    ],
                ],
                sample_limit=cfg.sample_limit,
            )

        unmatched_symbols = sorted(symbol_keys - set(daily["symbol_key"].dropna().astype(str)))
    else:
        merged = pd.DataFrame()
        unmatched_symbols = sorted(symbol_keys)

    if unmatched_symbols:
        _append_check(
            checks,
            check="unmatched_daily_reference_symbols",
            severity="warning",
            message="Depth snapshot symbols could not be matched in the daily benchmark data.",
            affected=len(unmatched_symbols),
            samples=pd.DataFrame({"symbol_key": unmatched_symbols}),
            sample_limit=cfg.sample_limit,
        )

    verdict = _quality_verdict(checks, fail_on_severity=cfg.fail_on_severity)
    summary = {
        "tick_rows": int(aggregate_meta["source_rows"]),
        "tick_symbol_days": int(len(tick_daily)),
        "daily_reference_rows": int(len(daily)),
        "matched_symbol_days": (
            int((merged.get("_merge") == "both").sum()) if not merged.empty else 0
        ),
        "unmatched_symbol_count": len(unmatched_symbols),
        **aggregate_meta,
    }
    return {
        "input_paths": {
            "tick_input": str(tick_input),
            "daily_asset_dir": str(daily_asset_dir),
        },
        "reference_policy": _reference_policy_metadata(reference_policy),
        "tolerance": cfg.to_dict(),
        "reference": reference_meta,
        "summary": summary,
        "quality_checks": checks,
        "quality_verdict": verdict,
        "status": "fail" if verdict["overall_severity"] == "error" else "pass",
    }


def write_reconciliation_report(
    tick_input: str | Path,
    daily_asset_dir: str | Path,
    out: str | Path,
    *,
    config: ReconcileConfig | None = None,
) -> dict[str, Any]:
    report = inspect_tick_daily_reconciliation(tick_input, daily_asset_dir, config=config)
    write_json(out, report)
    report["report_path"] = str(out)
    return report
