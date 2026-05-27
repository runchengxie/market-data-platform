"""Health checks for raw Hong Kong depth snapshot parquet parts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from market_data_platform.hk_depth.quality import (
    SESSION_PHASES,
    append_quality_check,
    numeric,
    quality_verdict,
    quote_ladder_flags,
    session_phase_counts,
)
from market_data_platform.hk_depth.storage import (
    atomic_write_parquet,
    discover_parquet_parts,
    metadata_path,
    write_json,
)

REQUIRED_RAW_COLUMNS = ("order_book_id", "datetime")
CUMULATIVE_RESET_RATIO = 0.50
DEFAULT_UNIT_DIAGNOSTIC_SAMPLE_LIMIT = 20
UNIT_AGGREGATE_METRICS = (
    "duplicate_row_count",
    "duplicate_key_count",
    "exact_duplicate_row_count",
    "same_timestamp_conflict_count",
    "timestamp_non_monotonic_count",
    "negative_volume_count",
    "negative_turnover_count",
    "volume_decrease_count",
    "turnover_decrease_count",
    "volume_large_drop_count",
    "turnover_large_drop_count",
    "volume_missing_then_resumed_count",
    "turnover_missing_then_resumed_count",
    *(f"{phase}_rows" for phase in SESSION_PHASES),
)


def _clean_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _clean_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _clean_scalar(value) for key, value in row.items()} for row in records]


def _prepare_work(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    if "order_book_id" not in work.columns:
        work["order_book_id"] = pd.NA
    if "datetime" in work.columns:
        work["_timestamp"] = pd.to_datetime(work["datetime"], errors="coerce")
    else:
        work["_timestamp"] = pd.Series(pd.NaT, index=work.index)
    if "trading_date" not in work.columns:
        work["trading_date"] = work["_timestamp"].dt.strftime("%Y%m%d")
    else:
        work["trading_date"] = work["trading_date"].astype("string")
    return work


def _duplicate_stats(frame: pd.DataFrame) -> dict[str, int]:
    if not {"order_book_id", "datetime"}.issubset(frame.columns) or frame.empty:
        return {
            "duplicate_row_count": 0,
            "duplicate_key_count": 0,
            "exact_duplicate_row_count": 0,
            "same_timestamp_conflict_count": 0,
        }
    key_cols = ["order_book_id", "datetime"]
    duplicated = frame.duplicated(key_cols, keep=False)
    duplicate_key_count = int(frame.loc[duplicated, key_cols].drop_duplicates().shape[0])
    exact_duplicate_row_count = int(frame.duplicated(keep=False).sum())
    conflict_count = 0
    if duplicated.any():
        value_cols = [
            column
            for column in frame.columns
            if column not in {"_timestamp"} and column not in key_cols
        ]
        if value_cols:
            grouped = frame.loc[duplicated, [*key_cols, *value_cols]].groupby(
                key_cols,
                dropna=False,
            )
            for _, group in grouped:
                unique_counts = group[value_cols].nunique(dropna=False)
                if bool((unique_counts > 1).any()):
                    conflict_count += 1
    return {
        "duplicate_row_count": int(duplicated.sum()),
        "duplicate_key_count": duplicate_key_count,
        "exact_duplicate_row_count": exact_duplicate_row_count,
        "same_timestamp_conflict_count": int(conflict_count),
    }


def _timestamp_non_monotonic_count(frame: pd.DataFrame) -> int:
    if "_timestamp" not in frame.columns:
        return 0
    timestamps = frame["_timestamp"]
    diffs = timestamps.diff()
    return int((diffs < pd.Timedelta(0)).sum())


def _missing_then_resumed_count(values: pd.Series) -> int:
    seen_valid = False
    in_missing_after_valid = False
    resumed = 0
    for value in values:
        if pd.isna(value):
            if seen_valid:
                in_missing_after_valid = True
            continue
        if in_missing_after_valid:
            resumed += 1
            in_missing_after_valid = False
        seen_valid = True
    return resumed


def _cumulative_stats(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if column not in frame.columns:
        return {
            "negative_count": 0,
            "decrease_count": 0,
            "large_drop_count": 0,
            "missing_then_resumed_count": 0,
        }
    ordered = (
        frame.sort_values("_timestamp", na_position="last")
        if "_timestamp" in frame
        else frame
    )
    values = numeric(ordered, column)
    diffs = values.diff()
    previous = values.shift()
    decreases = diffs < 0
    large_drops = decreases & (previous > 0) & (
        (diffs.abs() >= previous.abs() * CUMULATIVE_RESET_RATIO)
        | (values <= previous * CUMULATIVE_RESET_RATIO)
    )
    return {
        "negative_count": int((values < 0).sum()),
        "decrease_count": int(decreases.sum()),
        "large_drop_count": int(large_drops.sum()),
        "missing_then_resumed_count": _missing_then_resumed_count(values),
    }


def _quote_stats(frame: pd.DataFrame) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "quote_coverage_ratio": None,
        "bad_quote_ratio": None,
        "best_bid_missing_count": 0,
        "best_ask_missing_count": 0,
        "best_spread_cross_count": 0,
        "best_spread_zero_count": 0,
        "ask_ladder_inversion_count": 0,
        "bid_ladder_inversion_count": 0,
        "negative_depth_volume_count": 0,
        "quote_ladder_invalid_count": 0,
    }
    if {"a1", "b1"}.issubset(frame.columns):
        ask = numeric(frame, "a1")
        bid = numeric(frame, "b1")
        ask_missing = ask.isna() | (ask <= 0)
        bid_missing = bid.isna() | (bid <= 0)
        positive = ~ask_missing & ~bid_missing
        flags = quote_ladder_flags(frame)
        stats.update(
            {
                "quote_coverage_ratio": float(positive.mean()) if len(frame) else None,
                "bad_quote_ratio": (
                    float((~positive | flags["crossed_best_spread"]).mean())
                    if len(frame)
                    else None
                ),
                "best_bid_missing_count": int(bid_missing.sum()),
                "best_ask_missing_count": int(ask_missing.sum()),
                "best_spread_cross_count": int(flags["crossed_best_spread"].sum()),
                "best_spread_zero_count": int(flags["zero_best_spread"].sum()),
                "ask_ladder_inversion_count": int(flags["ask_ladder_inversion"].sum()),
                "bid_ladder_inversion_count": int(flags["bid_ladder_inversion"].sum()),
                "negative_depth_volume_count": int(flags["negative_depth_volume"].sum()),
                "quote_ladder_invalid_count": int(flags["quote_ladder_invalid"].sum()),
            }
        )
    return stats


def _unit_check_names(row: dict[str, Any]) -> list[str]:
    checks = []
    metric_to_check = {
        "timestamp_parse_failure_count": "timestamp_parse_failures",
        "duplicate_key_count": "duplicate_symbol_timestamp_rows",
        "same_timestamp_conflict_count": "same_timestamp_conflicts",
        "timestamp_non_monotonic_count": "timestamp_non_monotonic",
        "best_spread_cross_count": "invalid_best_quote_spread",
        "ask_ladder_inversion_count": "quote_ladder_invalid",
        "bid_ladder_inversion_count": "quote_ladder_invalid",
        "negative_depth_volume_count": "negative_depth_volume",
        "negative_volume_count": "negative_volume_count",
        "negative_turnover_count": "negative_turnover_count",
        "volume_decrease_count": "volume_decrease_count",
        "turnover_decrease_count": "turnover_decrease_count",
        "volume_large_drop_count": "volume_large_drop_count",
        "turnover_large_drop_count": "turnover_large_drop_count",
        "volume_missing_then_resumed_count": "volume_missing_then_resumed",
        "turnover_missing_then_resumed_count": "turnover_missing_then_resumed",
        "outside_session_rows": "session_time_outlier",
    }
    for metric, check in metric_to_check.items():
        if int(row.get(metric) or 0) > 0 and check not in checks:
            checks.append(check)
    return checks


def _unit_diagnostics(
    work: pd.DataFrame,
    sample_limit: int = 5,
    *,
    sample_clean_units: bool = False,
) -> list[dict[str, Any]]:
    if work.empty:
        return []
    diagnostics: list[dict[str, Any]] = []
    grouped = work.groupby(["order_book_id", "trading_date"], sort=True, dropna=False)
    for (symbol, trade_date), group in grouped:
        timestamps = group["_timestamp"]
        duplicate_stats = _duplicate_stats(group)
        quote_stats = _quote_stats(group)
        volume_stats = _cumulative_stats(group, "volume")
        turnover_stats = _cumulative_stats(group, "total_turnover")
        phase_counts = session_phase_counts(group["_timestamp"])
        row: dict[str, Any] = {
            "order_book_id": _clean_scalar(symbol),
            "trading_date": _clean_scalar(trade_date),
            "row_count": int(len(group)),
            "timestamp_start": (
                timestamps.min().isoformat() if timestamps.notna().any() else None
            ),
            "timestamp_end": (
                timestamps.max().isoformat() if timestamps.notna().any() else None
            ),
            "timestamp_parse_failure_count": int(timestamps.isna().sum()),
            "timestamp_non_monotonic_count": _timestamp_non_monotonic_count(group),
            **duplicate_stats,
            **quote_stats,
            "negative_volume_count": volume_stats["negative_count"],
            "volume_decrease_count": volume_stats["decrease_count"],
            "volume_large_drop_count": volume_stats["large_drop_count"],
            "volume_missing_then_resumed_count": volume_stats["missing_then_resumed_count"],
            "negative_turnover_count": turnover_stats["negative_count"],
            "turnover_decrease_count": turnover_stats["decrease_count"],
            "turnover_large_drop_count": turnover_stats["large_drop_count"],
            "turnover_missing_then_resumed_count": turnover_stats["missing_then_resumed_count"],
            **{f"{phase}_rows": phase_counts[phase] for phase in SESSION_PHASES},
        }
        row["check_names"] = _unit_check_names(row)
        row["severity"] = "warning" if row["check_names"] else "none"
        row["sample_rows"] = []
        if row["check_names"] or sample_clean_units:
            sample_cols = [
                column
                for column in (
                    "order_book_id",
                    "trading_date",
                    "datetime",
                    "last",
                    "a1",
                    "b1",
                    "volume",
                    "total_turnover",
                )
                if column in group.columns
            ]
            row["sample_rows"] = _clean_records(
                group.loc[:, sample_cols].head(sample_limit).to_dict("records")
            )
        diagnostics.append(row)
    return diagnostics


def _accumulate_unit_metrics(totals: dict[str, int], units: list[dict[str, Any]]) -> None:
    for unit in units:
        for metric in UNIT_AGGREGATE_METRICS:
            totals[metric] += int(unit.get(metric) or 0)


def _append_unit_diagnostics_csv(
    path: Path,
    units: list[dict[str, Any]],
    *,
    append: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(units)
    frame.to_csv(path, mode="a" if append else "w", header=not append, index=False)


def _build_quality_checks(report: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if report["row_count"] == 0:
        append_quality_check(
            checks,
            check="empty_dataset",
            severity="error",
            message="Raw tick dataset has no rows.",
            include_zero=True,
        )
    if report["missing_required_columns"]:
        append_quality_check(
            checks,
            check="missing_required_columns",
            severity="error",
            message="Raw tick dataset is missing identity columns.",
            columns=list(report["missing_required_columns"]),
            include_zero=True,
        )
    warning_metrics = {
        "timestamp_parse_failure_count": "timestamp_parse_failures",
        "duplicate_key_count": "duplicate_symbol_timestamp_rows",
        "same_timestamp_conflict_count": "same_timestamp_conflicts",
        "timestamp_non_monotonic_count": "timestamp_non_monotonic",
        "best_spread_cross_count": "invalid_best_quote_spread",
        "quote_ladder_invalid_count": "quote_ladder_invalid",
        "negative_depth_volume_count": "negative_depth_volume",
        "negative_volume_count": "negative_volume_count",
        "negative_turnover_count": "negative_turnover_count",
        "volume_decrease_count": "volume_decrease_count",
        "turnover_decrease_count": "turnover_decrease_count",
        "volume_large_drop_count": "volume_large_drop_count",
        "turnover_large_drop_count": "turnover_large_drop_count",
        "volume_missing_then_resumed_count": "volume_missing_then_resumed",
        "turnover_missing_then_resumed_count": "turnover_missing_then_resumed",
        "outside_session_rows": "session_time_outlier",
    }
    messages = {
        "timestamp_parse_failures": "Some tick timestamps could not be parsed.",
        "duplicate_symbol_timestamp_rows": "Duplicate order_book_id/datetime keys were found.",
        "same_timestamp_conflicts": "Some duplicate timestamps contain conflicting values.",
        "timestamp_non_monotonic": "Timestamps move backwards inside some symbol-date units.",
        "invalid_best_quote_spread": "Best ask is below best bid for some rows.",
        "quote_ladder_invalid": "Quote depth ladder rules were violated.",
        "negative_depth_volume": "Quote depth volume fields contain negative values.",
        "session_time_outlier": "Tick timestamps fall outside the accepted HK tick session window.",
    }
    for metric, check in warning_metrics.items():
        affected = int(report.get(metric) or 0)
        append_quality_check(
            checks,
            check=check,
            severity="warning",
            message=messages.get(check, f"{metric} is non-zero."),
            affected=affected,
        )
    return checks


def _empty_report(input_root: str | Path) -> dict[str, Any]:
    return {
        "input_root": str(input_root),
        "row_count": 0,
        "symbol_count": 0,
        "date_count": 0,
        "timestamp_start": None,
        "timestamp_end": None,
        "missing_required_columns": [],
        "field_missing_rates": {},
        "duplicate_row_count": 0,
        "duplicate_key_count": 0,
        "exact_duplicate_row_count": 0,
        "same_timestamp_conflict_count": 0,
        "timestamp_non_monotonic_count": 0,
        "invalid_best_spread_count": 0,
        "invalid_best_spread_groups": [],
        "quote_coverage_ratio": None,
        "bad_quote_ratio": None,
        "best_bid_missing_count": 0,
        "best_ask_missing_count": 0,
        "best_spread_cross_count": 0,
        "best_spread_zero_count": 0,
        "ask_ladder_inversion_count": 0,
        "bid_ladder_inversion_count": 0,
        "quote_ladder_invalid_count": 0,
        "negative_depth_volume_count": 0,
        "negative_volume_count": None,
        "negative_turnover_count": None,
        "volume_decrease_count": None,
        "turnover_decrease_count": None,
        "volume_large_drop_count": 0,
        "turnover_large_drop_count": 0,
        "volume_missing_then_resumed_count": 0,
        "turnover_missing_then_resumed_count": 0,
        **{f"{phase}_rows": 0 for phase in SESSION_PHASES},
        "unit_count": 0,
        "anomalous_unit_count": 0,
        "unit_diagnostic_sample_limit": DEFAULT_UNIT_DIAGNOSTIC_SAMPLE_LIMIT,
        "unit_diagnostics_truncated": False,
        "unit_diagnostics": [],
        "warnings": [],
        "failures": [],
        "quality_checks": [],
        "quality_verdict": {},
        "status": "pass",
    }


def _add_field_missing_counts(
    missing_counts: dict[str, int],
    seen_columns: set[str],
    frame: pd.DataFrame,
    *,
    prior_rows: int,
) -> None:
    frame_columns = set(frame.columns)
    frame_rows = int(len(frame))
    for column in seen_columns - frame_columns:
        missing_counts[column] += frame_rows
    for column in frame.columns:
        if column not in missing_counts:
            missing_counts[column] = prior_rows
        missing_counts[column] += int(frame[column].isna().sum())
    seen_columns.update(frame_columns)


def _append_invalid_spread_groups(
    groups: list[dict[str, Any]],
    work: pd.DataFrame,
    *,
    limit: int = 20,
) -> None:
    if len(groups) >= limit:
        return
    group_cols = [col for col in ("order_book_id", "trading_date") if col in work.columns]
    if not group_cols:
        return
    flags = quote_ladder_flags(work)
    crossed = work.loc[flags["crossed_best_spread"], group_cols]
    if crossed.empty:
        return
    for row in _clean_records(crossed.drop_duplicates().to_dict("records")):
        if row not in groups:
            groups.append(row)
        if len(groups) >= limit:
            break


def _finish_health_report(report: dict[str, Any], fail_on_severity: str) -> dict[str, Any]:
    report["quality_checks"] = _build_quality_checks(report)
    report["quality_verdict"] = quality_verdict(
        report["quality_checks"],
        fail_on_severity=fail_on_severity,
        include_sample_failing_checks=True,
    )
    return report


def inspect_raw_health(
    input_root: str | Path,
    *,
    fail_on_severity: str = "error",
    unit_sample_limit: int = DEFAULT_UNIT_DIAGNOSTIC_SAMPLE_LIMIT,
    units_output: str | Path | None = None,
) -> dict[str, Any]:
    if unit_sample_limit < 0:
        raise ValueError("unit_sample_limit must be non-negative.")
    parts = discover_parquet_parts(input_root)
    report: dict[str, Any] = {
        **_empty_report(input_root),
        "part_count": len(parts),
        "unit_diagnostic_sample_limit": unit_sample_limit,
    }
    units_path = Path(units_output) if units_output is not None else None
    stream_units_to_csv = units_path is not None and units_path.suffix.lower() != ".parquet"
    buffered_output_units: list[dict[str, Any]] | None = (
        [] if units_path and not stream_units_to_csv else None
    )
    wrote_csv_units = False

    if not parts:
        report["failures"].append("empty_dataset")
        report["status"] = "fail"
        if units_path is not None:
            _write_unit_diagnostics(units_path, [])
            report["unit_diagnostics_write_mode"] = (
                "streamed_csv" if stream_units_to_csv else "buffered_parquet"
            )
            report["unit_diagnostics_path"] = str(units_path)
        return _finish_health_report(report, fail_on_severity)

    seen_columns: set[str] = set()
    missing_counts: dict[str, int] = {}
    symbols: set[str] = set()
    dates: set[str] = set()
    timestamp_start: pd.Timestamp | None = None
    timestamp_end: pd.Timestamp | None = None
    quote_positive_count = 0
    quote_bad_count = 0
    rows_missing_a1_column = 0
    rows_missing_b1_column = 0
    rows_missing_quote_columns = 0
    sampled_units: list[dict[str, Any]] = []
    unit_totals = dict.fromkeys(UNIT_AGGREGATE_METRICS, 0)

    for part in parts:
        frame = pd.read_parquet(part)
        prior_rows = int(report["row_count"])
        frame_rows = int(len(frame))
        report["row_count"] = prior_rows + frame_rows
        _add_field_missing_counts(
            missing_counts,
            seen_columns,
            frame,
            prior_rows=prior_rows,
        )
        if frame_rows == 0:
            continue

        work = _prepare_work(frame)
        if "order_book_id" in frame.columns:
            symbols.update(frame["order_book_id"].dropna().astype(str).unique())
        if "trading_date" in work.columns:
            dates.update(work["trading_date"].dropna().astype(str).unique())

        timestamps = work["_timestamp"]
        report["timestamp_parse_failure_count"] = int(
            report.get("timestamp_parse_failure_count", 0)
        ) + int(timestamps.isna().sum())
        if timestamps.notna().any():
            part_start = timestamps.min()
            part_end = timestamps.max()
            timestamp_start = (
                part_start if timestamp_start is None else min(timestamp_start, part_start)
            )
            timestamp_end = part_end if timestamp_end is None else max(timestamp_end, part_end)

        part_units = _unit_diagnostics(work, sample_clean_units=False)
        report["unit_count"] += len(part_units)
        _accumulate_unit_metrics(unit_totals, part_units)
        anomalous_units = [unit for unit in part_units if unit["check_names"]]
        report["anomalous_unit_count"] += len(anomalous_units)
        remaining_sample_slots = max(unit_sample_limit - len(sampled_units), 0)
        sampled_units.extend(anomalous_units[:remaining_sample_slots])
        if units_path is not None:
            if stream_units_to_csv:
                _append_unit_diagnostics_csv(
                    units_path,
                    part_units,
                    append=wrote_csv_units,
                )
                wrote_csv_units = wrote_csv_units or bool(part_units)
            else:
                assert buffered_output_units is not None
                buffered_output_units.extend(part_units)
        quote_stats = _quote_stats(work)
        for metric in (
            "best_bid_missing_count",
            "best_ask_missing_count",
            "best_spread_cross_count",
            "best_spread_zero_count",
            "ask_ladder_inversion_count",
            "bid_ladder_inversion_count",
            "quote_ladder_invalid_count",
            "negative_depth_volume_count",
        ):
            report[metric] = int(report.get(metric) or 0) + int(quote_stats.get(metric) or 0)
        if {"a1", "b1"}.issubset(work.columns):
            quote_positive_count += int(
                round(float(quote_stats["quote_coverage_ratio"] or 0) * frame_rows)
            )
            quote_bad_count += int(round(float(quote_stats["bad_quote_ratio"] or 0) * frame_rows))
        else:
            rows_missing_quote_columns += frame_rows
        if "a1" not in work.columns:
            rows_missing_a1_column += frame_rows
        if "b1" not in work.columns:
            rows_missing_b1_column += frame_rows
        if quote_stats.get("best_spread_cross_count"):
            _append_invalid_spread_groups(report["invalid_best_spread_groups"], work)

    if report["row_count"] == 0:
        report["failures"].append("empty_dataset")
        report["status"] = "fail"

    missing = [col for col in REQUIRED_RAW_COLUMNS if col not in seen_columns]
    report["missing_required_columns"] = missing
    if missing:
        report["failures"].append("missing_required_columns")
        report["status"] = "fail"

    report["symbol_count"] = len(symbols)
    report["date_count"] = len(dates)
    if timestamp_start is not None:
        report["timestamp_start"] = timestamp_start.isoformat()
    if timestamp_end is not None:
        report["timestamp_end"] = timestamp_end.isoformat()
    if report["row_count"]:
        report["field_missing_rates"] = {
            str(column): float(count / int(report["row_count"]))
            for column, count in sorted(missing_counts.items())
        }

    report["unit_diagnostics"] = sampled_units
    report["unit_diagnostics_truncated"] = report["anomalous_unit_count"] > len(sampled_units)
    for metric, total in unit_totals.items():
        report[metric] = total
    if "a1" in seen_columns:
        report["best_ask_missing_count"] += rows_missing_a1_column
    if "b1" in seen_columns:
        report["best_bid_missing_count"] += rows_missing_b1_column
    if {"a1", "b1"}.issubset(seen_columns) and report["row_count"]:
        quote_bad_count += rows_missing_quote_columns
        report["quote_coverage_ratio"] = float(quote_positive_count / int(report["row_count"]))
        report["bad_quote_ratio"] = float(quote_bad_count / int(report["row_count"]))
    report["invalid_best_spread_count"] = report["best_spread_cross_count"]

    warning_metrics = [
        "timestamp_parse_failure_count",
        "duplicate_key_count",
        "same_timestamp_conflict_count",
        "timestamp_non_monotonic_count",
        "best_spread_cross_count",
        "quote_ladder_invalid_count",
        "negative_depth_volume_count",
        "negative_volume_count",
        "negative_turnover_count",
        "volume_decrease_count",
        "turnover_decrease_count",
        "volume_large_drop_count",
        "turnover_large_drop_count",
        "volume_missing_then_resumed_count",
        "turnover_missing_then_resumed_count",
        "outside_session_rows",
    ]
    report["warnings"] = [metric for metric in warning_metrics if int(report.get(metric) or 0)]
    if units_path is not None:
        if stream_units_to_csv:
            if not wrote_csv_units:
                _write_unit_diagnostics(units_path, [])
            report["unit_diagnostics_write_mode"] = "streamed_csv"
        else:
            assert buffered_output_units is not None
            _write_unit_diagnostics(units_path, buffered_output_units)
            report["unit_diagnostics_write_mode"] = "buffered_parquet"
        report["unit_diagnostics_path"] = str(units_path)

    return _finish_health_report(report, fail_on_severity)


def _write_unit_diagnostics(path: str | Path, units: list[dict[str, Any]]) -> Path:
    target = Path(path)
    frame = pd.DataFrame(units)
    if target.suffix.lower() == ".parquet":
        return atomic_write_parquet(frame, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False)
    return target


def write_health_report(
    input_root: str | Path,
    output_json: str | Path | None = None,
    *,
    fail_on_severity: str = "error",
    units_output: str | Path | None = None,
    unit_sample_limit: int = DEFAULT_UNIT_DIAGNOSTIC_SAMPLE_LIMIT,
) -> dict[str, Any]:
    report = inspect_raw_health(
        input_root,
        fail_on_severity=fail_on_severity,
        unit_sample_limit=unit_sample_limit,
        units_output=units_output,
    )
    path = (
        Path(output_json) if output_json else metadata_path(Path(input_root) / "health", "health")
    )
    write_json(path, report)
    report["report_path"] = str(path)
    return report


def format_health_summary(report: dict[str, Any]) -> str:
    verdict = report.get("quality_verdict")
    severity = (
        verdict.get("overall_severity", "unknown")
        if isinstance(verdict, dict)
        else "unknown"
    )
    return (
        f"status={report['status']} rows={report['row_count']} "
        f"symbols={report['symbol_count']} dates={report['date_count']} "
        f"warnings={len(report['warnings'])} failures={len(report['failures'])} "
        f"quality={severity}"
    )
