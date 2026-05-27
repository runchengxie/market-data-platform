from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .asset_health_daily_rules import (
    build_daily_rule_quality_checks as _build_daily_rule_quality_checks,
    init_daily_rule_stats as _init_daily_rule_stats,
    record_daily_rule_stats as _record_daily_rule_stats,
)
from .asset_health_rendering import render_asset_health_text as _render_asset_health_text
from .health_shared import (
    format_date as _format_date,
    load_symbols_from_text as _load_symbols_from_text,
    normalize_symbol_list as _normalize_symbol_list,
    parse_compact_date as _parse_compact_date,
)
from .quality_gate import quality_gate_exit_code, summarize_quality_checks
from .shared import (
    DEFAULT_HK_DAILY_FIELDS,
    DEFAULT_HK_VALUATION_FIELDS,
    _coerce_bool,
    _dedupe_preserve_order,
    _load_manifest,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _resolve_path,
    _resolve_universe_by_date_columns,
)

DATE_COLUMN_CANDIDATES = (
    "trade_date",
    "date",
    "info_date",
    "ex_date",
    "declaration_announcement_date",
    "start_date",
)
AUDIT_LATEST_DATE_COLUMNS = ("max_trade_date", "max_date", "max_info_date")
AUDIT_SYMBOL_COLUMNS = ("symbol", "ts_code", "order_book_id")
KEY_COLUMNS = {
    "symbol",
    "ts_code",
    "stock_ticker",
    "order_book_id",
    "trade_date",
    "date",
    "info_date",
    "ex_date",
    "ex_end_date",
    "announcement_date",
    "declaration_announcement_date",
    "ex_dividend_date",
    "book_closure_date",
    "payable_date",
    "advance_date",
    "start_date",
    "cancel_date",
    "quarter",
    "fiscal_year",
    "rice_create_tm",
    "standard",
    "if_adjusted",
    "round_lot",
    "unique_id",
    "index",
}
PLACEHOLDER_TOKENS = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "nan",
    "nat",
    "n.a.",
    "null",
    "none",
    "#n/a",
}
VALUATION_STALE_RUN_MIN_LENGTH = 5
VALUATION_DELIST_BOUNDARY_MAX_DAYS = 5
VALUATION_SHARES_EVENT_LEAD_DAYS = 2
VALUATION_PROVIDER_LIKE_REASON_LABELS = {
    "no_daily_reference_window": "no_daily_reference_window",
    "no_finite_daily_close": "no_finite_daily_close",
    "no_daily_trading_activity": "no_daily_trading_activity",
    "daily_reference_stale": "daily_reference_stale",
    "no_daily_price_change": "no_daily_price_change",
    "daily_price_changed": "daily_price_changed",
    "no_daily_reference": "no_daily_reference",
    "ex_factor_event_in_window": "ex_factor_event_in_window",
    "shares_event_in_window": "shares_event_in_window",
    "shares_event_near_window": "shares_event_near_window",
    "delisted_instrument_boundary": "delisted_instrument_boundary",
}
VALUATION_FRESH_TARGET_GAP_REASON_LABELS = {
    "target_market_val_present": "target_market_val_present",
    "target_market_val_changed": "target_market_val_changed",
}
CONSTANT_CROSS_SECTION_FIELD_EXEMPTIONS = {
    "southbound": {"eligible", "trading_type"},
}


@dataclass(frozen=True)
class _ValuationReferenceFrames:
    daily: pd.DataFrame | None = None
    ex_factor: pd.DataFrame | None = None
    shares: pd.DataFrame | None = None
    instrument: pd.DataFrame | None = None


class _ValuationReferenceLoader:
    def __init__(
        self,
        *,
        symbol: str,
        daily_asset_dir: Path | None,
        ex_factor_asset_dir: Path | None,
        shares_asset_dir: Path | None,
        instrument_by_symbol: Mapping[str, pd.DataFrame],
    ) -> None:
        self._symbol = symbol
        self._daily_asset_dir = daily_asset_dir
        self._ex_factor_asset_dir = ex_factor_asset_dir
        self._shares_asset_dir = shares_asset_dir
        self._instrument_by_symbol = instrument_by_symbol
        self._frames: _ValuationReferenceFrames | None = None

    def load(self) -> _ValuationReferenceFrames:
        if self._frames is None:
            self._frames = _ValuationReferenceFrames(
                daily=_load_daily_reference_frame(self._daily_asset_dir, self._symbol),
                ex_factor=_load_ex_factor_reference_frame(self._ex_factor_asset_dir, self._symbol),
                shares=_load_shares_reference_frame(self._shares_asset_dir, self._symbol),
                instrument=self._instrument_by_symbol.get(self._symbol),
            )
        return self._frames


def _clean_optional_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _serialize_scalar(value: object) -> int | float | str | bool | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return _format_date(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value_float = float(value)
        if not np.isfinite(value_float):
            return str(value_float)
        if value_float.is_integer():
            return int(value_float)
        return round(value_float, 8)
    text = str(value).strip()
    if not text:
        return None
    parsed_numeric = pd.to_numeric(pd.Series([text]), errors="coerce").iloc[0]
    if pd.notna(parsed_numeric) and np.isfinite(parsed_numeric):
        parsed_float = float(parsed_numeric)
        if parsed_float.is_integer():
            return int(parsed_float)
        return round(parsed_float, 8)
    return text


def _resolve_manifest_query_date(manifest: Mapping[str, object] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    query = manifest.get("query")
    if not isinstance(query, Mapping):
        return None
    for key in ("end_date", "date", "mapping_date", "as_of_date"):
        value = query.get(key)
        if value is None:
            continue
        try:
            return _format_date(_parse_compact_date(value, label=f"manifest.query.{key}"))
        except SystemExit:
            continue
    return None


def _infer_date_column(columns: Sequence[str], explicit: str | None) -> str:
    if explicit:
        if explicit not in columns:
            raise SystemExit(f"Date column not found in asset schema: {explicit}")
        return explicit
    for candidate in DATE_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    raise SystemExit(
        "Could not infer a date column. Pass --date-column explicitly. "
        f"Available columns: {', '.join(columns)}"
    )


def _manifest_field_coverage_by_field(
    manifest: Mapping[str, object] | None,
) -> dict[str, Mapping[str, object]]:
    if not isinstance(manifest, Mapping):
        return {}
    coverage_rows = manifest.get("field_coverage")
    if not isinstance(coverage_rows, Sequence) or isinstance(coverage_rows, (str, bytes)):
        return {}

    coverage_by_field: dict[str, Mapping[str, object]] = {}
    for row in coverage_rows:
        if not isinstance(row, Mapping):
            continue
        field = str(row.get("field") or "").strip()
        if field:
            coverage_by_field[field] = row
    return coverage_by_field


def _coverage_row_has_values(coverage: Mapping[str, object]) -> bool:
    symbols_with_values = pd.to_numeric(
        pd.Series([coverage.get("symbols_with_values")]),
        errors="coerce",
    ).iloc[0]
    nonnull_rows = pd.to_numeric(
        pd.Series([coverage.get("nonnull_rows")]),
        errors="coerce",
    ).iloc[0]
    return bool(
        (pd.notna(symbols_with_values) and float(symbols_with_values) > 0)
        or (pd.notna(nonnull_rows) and float(nonnull_rows) > 0)
    )


def _filter_fields_with_manifest_coverage(
    values: Sequence[str],
    *,
    source_label: str,
    manifest: Mapping[str, object] | None,
    columns: Sequence[str],
) -> tuple[list[str], str] | None:
    fields = [field for field in values if field in columns]
    if not fields:
        return None

    coverage_by_field = _manifest_field_coverage_by_field(manifest)
    if not coverage_by_field:
        return fields, source_label

    filtered: list[str] = []
    for field in fields:
        coverage = coverage_by_field.get(field)
        if coverage is None or _coverage_row_has_values(coverage):
            filtered.append(field)

    if filtered:
        if len(filtered) != len(fields):
            return filtered, f"{source_label}_nonzero_coverage"
        return filtered, source_label
    return fields, source_label


def _resolve_default_fields(
    *,
    dataset: str | None,
    manifest: Mapping[str, object] | None,
    columns: Sequence[str],
) -> tuple[list[str], str]:
    if dataset == "daily":
        resolved = _filter_fields_with_manifest_coverage(
            DEFAULT_HK_DAILY_FIELDS,
            source_label="default_daily_fields",
            manifest=manifest,
            columns=columns,
        )
        if resolved is not None:
            return resolved
    if dataset == "valuation":
        resolved = _filter_fields_with_manifest_coverage(
            DEFAULT_HK_VALUATION_FIELDS,
            source_label="default_valuation_fields",
            manifest=manifest,
            columns=columns,
        )
        if resolved is not None:
            return resolved

    if isinstance(manifest, Mapping):
        query = manifest.get("query")
        if isinstance(query, Mapping):
            manifest_fields = query.get("fields")
            if isinstance(manifest_fields, Sequence) and not isinstance(manifest_fields, (str, bytes)):
                resolved = _filter_fields_with_manifest_coverage(
                    [str(field).strip() for field in manifest_fields if str(field).strip()],
                    source_label="manifest_query_fields",
                    manifest=manifest,
                    columns=columns,
                )
                if resolved is not None:
                    return resolved

    inferred = [column for column in columns if column not in KEY_COLUMNS]
    if inferred:
        return inferred, "inferred_non_key_columns"
    raise SystemExit("No value fields resolved for asset health inspection.")


def _duplicate_key_columns(
    *,
    dataset: str | None,
    date_column: str,
    columns: Sequence[str],
) -> list[str]:
    keys = [date_column]
    if dataset == "southbound" and "trading_type" in columns:
        keys.append("trading_type")
    elif dataset == "financial_details":
        for column in (
            "field",
            "quarter",
            "fiscal_year",
            "relationship",
            "currency",
            "standard",
            "subject",
        ):
            if column in columns and column not in keys:
                keys.append(column)
    elif dataset == "dividends":
        for column in (
            "ex_dividend_date",
            "book_closure_date",
            "payable_date",
            "advance_date",
            "quarter",
            "dividend_cash_before_tax",
            "unique_id",
        ):
            if column in columns and column not in keys:
                keys.append(column)
    elif dataset in {"ex_factors", "shares"} and "unique_id" in columns:
        keys.append("unique_id")
    elif dataset == "industry_changes" and "industry_code" in columns:
        keys.append("industry_code")
    return keys


def _duplicate_key_read_columns(*, dataset: str | None) -> list[str]:
    if dataset == "southbound":
        return ["trading_type"]
    if dataset == "financial_details":
        return [
            "field",
            "quarter",
            "fiscal_year",
            "relationship",
            "currency",
            "standard",
            "subject",
        ]
    if dataset == "dividends":
        return [
            "ex_dividend_date",
            "book_closure_date",
            "payable_date",
            "advance_date",
            "quarter",
            "dividend_cash_before_tax",
            "unique_id",
        ]
    if dataset in {"ex_factors", "shares"}:
        return ["unique_id"]
    if dataset == "industry_changes":
        return ["industry_code"]
    return []


def _skip_constant_cross_section_quality_check(*, dataset: str | None, field: str) -> bool:
    exempt_fields = CONSTANT_CROSS_SECTION_FIELD_EXEMPTIONS.get(str(dataset or "").strip())
    if not exempt_fields:
        return False
    return field in exempt_fields


def _resolve_fields(
    *,
    requested_fields: Sequence[str],
    dataset: str | None,
    manifest: Mapping[str, object] | None,
    columns: Sequence[str],
) -> tuple[list[str], str]:
    explicit = [str(field).strip() for field in requested_fields if str(field).strip()]
    if explicit:
        missing = [field for field in explicit if field not in columns]
        if missing:
            raise SystemExit(
                "Requested field(s) not found in asset schema: "
                + ", ".join(missing)
                + ". Available columns: "
                + ", ".join(columns)
            )
        return explicit, "explicit"
    return _resolve_default_fields(dataset=dataset, manifest=manifest, columns=columns)


def _load_audit_frame(asset_dir: Path) -> tuple[pd.DataFrame | None, str | None]:
    audit_path = asset_dir / "audit.csv"
    if not audit_path.exists():
        return None, None
    audit = pd.read_csv(audit_path)
    audit = _normalize_frame_columns(audit)
    if audit.empty:
        return audit, None
    latest_col = next((column for column in AUDIT_LATEST_DATE_COLUMNS if column in audit.columns), None)
    if latest_col is None:
        return audit, None
    symbol_col = next((column for column in AUDIT_SYMBOL_COLUMNS if column in audit.columns), None)
    if symbol_col is None:
        return audit, latest_col

    audit = audit.copy()
    audit["symbol"] = audit[symbol_col].map(_normalize_hk_symbol)
    latest_text = audit[latest_col].astype(str).str.strip()
    latest_text = latest_text.str.replace(r"\.0+$", "", regex=True)
    audit["latest_date"] = pd.to_datetime(latest_text, errors="coerce").dt.normalize()
    if "status" in audit.columns:
        audit["status"] = audit["status"].fillna("").astype(str).str.strip()
    else:
        audit["status"] = ""
    audit = audit.dropna(subset=["symbol"])
    return audit, latest_col


def _categorize_audit_issue(*, status: str, error: str | None) -> str:
    status_text = str(status or "").strip() or "unknown"
    error_text = _clean_optional_text(error) or ""
    if not error_text:
        return status_text

    error_lower = error_text.lower()
    if "no permission to access day bar" in error_lower:
        return "no_permission_day_bar"
    if "no permission" in error_lower:
        return "no_permission"
    if "quota" in error_lower:
        return "quota_blocked"
    if "temporary failure in name resolution" in error_lower:
        return "dns_resolution_failure"
    return error_text


def _build_missing_asset_file_details(
    *,
    missing_asset_symbols: Sequence[str],
    audit_by_symbol: Mapping[str, Mapping[str, object]],
    sample_limit: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for symbol in missing_asset_symbols:
        audit_entry = audit_by_symbol.get(symbol)
        status = _clean_optional_text((audit_entry or {}).get("status")) or ""
        error = _clean_optional_text((audit_entry or {}).get("error"))
        if len(rows) >= sample_limit:
            continue
        rows.append(
            {
                "symbol": symbol,
                "status": status or None,
                "error": error,
            }
        )
    return rows


def _build_audit_issue_groups(
    *,
    audit_by_symbol: Mapping[str, Mapping[str, object]],
    scoped_symbols: Sequence[str],
    sample_limit: int,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for symbol in scoped_symbols:
        audit_entry = audit_by_symbol.get(symbol)
        if not audit_entry:
            continue
        status = _clean_optional_text(audit_entry.get("status")) or ""
        error = _clean_optional_text(audit_entry.get("error"))
        if not status:
            continue
        if status not in {"failed", "missing_remote", "missing_source_asset", "quota_blocked"}:
            continue
        category = _categorize_audit_issue(status=status, error=error)
        key = (status, category)
        row = grouped.get(key)
        if row is None:
            row = {
                "status": status,
                "issue_category": category,
                "error": error,
                "affected_symbols": 0,
                "sample_symbols": [],
            }
            grouped[key] = row
        elif not row.get("error") and error:
            row["error"] = error
        row["affected_symbols"] = int(row["affected_symbols"]) + 1
        _append_sample(row["sample_symbols"], symbol, limit=sample_limit)

    rows = list(grouped.values())
    rows.sort(
        key=lambda item: (
            -int(item.get("affected_symbols") or 0),
            str(item.get("status") or ""),
            str(item.get("issue_category") or ""),
        )
    )
    return rows


def _build_audit_status_counts(
    *,
    audit_by_symbol: Mapping[str, Mapping[str, object]],
    scoped_symbols: Sequence[str],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for symbol in scoped_symbols:
        audit_entry = audit_by_symbol.get(symbol)
        if not audit_entry:
            continue
        status = _clean_optional_text(audit_entry.get("status")) or ""
        if status:
            counts[status] += 1
    return counts


def _resolve_target_date(
    *,
    explicit_value: object,
    audit: pd.DataFrame | None,
    manifest: Mapping[str, object] | None,
    data_files: Sequence[Path],
    date_column: str,
) -> tuple[pd.Timestamp, str]:
    if explicit_value:
        return _parse_compact_date(explicit_value, label="--target-date"), "explicit"

    if audit is not None and "latest_date" in audit.columns and audit["latest_date"].notna().any():
        return pd.Timestamp(audit["latest_date"].dropna().max()).normalize(), "audit_latest_date"

    manifest_query_date = _resolve_manifest_query_date(manifest)
    if manifest_query_date:
        return pd.to_datetime(manifest_query_date).normalize(), "manifest_query_date"

    latest_dates: list[pd.Timestamp] = []
    for path in data_files:
        frame = pd.read_parquet(path, columns=[date_column])
        frame = _normalize_frame_columns(frame)
        if date_column not in frame.columns:
            continue
        parsed = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if not parsed.empty:
            latest_dates.append(parsed.max().normalize())
    if not latest_dates:
        raise SystemExit("Could not resolve a target date from audit, manifest, or parquet files.")
    return max(latest_dates), "file_scan_latest_date"


def _build_latest_date_counts(audit: pd.DataFrame | None, data_files: Sequence[Path]) -> tuple[Counter, Counter]:
    latest_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    if audit is None or audit.empty:
        return latest_counts, status_counts

    file_symbols = {_normalize_hk_symbol(path.stem) for path in data_files}
    for _, row in audit.iterrows():
        symbol = str(row.get("symbol") or "").strip()
        if not symbol or symbol not in file_symbols:
            continue
        latest_date = _format_date(row.get("latest_date"))
        if latest_date:
            latest_counts[latest_date] += 1
        status = str(row.get("status") or "").strip()
        if status:
            status_counts[status] += 1
    return latest_counts, status_counts

def _load_symbols_from_by_date_target_date(path_text: str | Path, *, target_date: pd.Timestamp) -> list[str]:
    path = _resolve_path(path_text)
    if not path.exists():
        raise SystemExit(f"Universe-by-date file not found: {path}")
    df = pd.read_csv(path)
    if df.empty:
        raise SystemExit(f"Universe-by-date file is empty: {path}")

    columns = {str(col).lower(): str(col) for col in df.columns}
    date_col, symbol_col = _resolve_universe_by_date_columns(df)

    selected_col = (
        columns.get("selected")
        or columns.get("selected_bool")
        or columns.get("selected_flag")
        or columns.get("is_selected")
    )
    if selected_col and selected_col in df.columns:
        df = df[df[selected_col].map(_coerce_bool)].copy()

    df = df.rename(columns={date_col: "trade_date", symbol_col: "symbol"})
    trade_date_text = df["trade_date"].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    digits_mask = trade_date_text.str.fullmatch(r"\d{8}")
    parsed = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    if digits_mask.any():
        parsed.loc[digits_mask] = pd.to_datetime(
            trade_date_text.loc[digits_mask],
            format="%Y%m%d",
            errors="coerce",
        )
    if (~digits_mask).any():
        parsed.loc[~digits_mask] = pd.to_datetime(
            trade_date_text.loc[~digits_mask],
            errors="coerce",
        )
    df["trade_date"] = parsed.dt.normalize()
    df = df[df["trade_date"] == target_date].copy()
    if df.empty:
        return []
    return _normalize_symbol_list(df["symbol"].tolist())


def _resolve_symbol_filter(args, *, target_date: pd.Timestamp) -> dict[str, object]:
    symbols: list[str] = []
    sources: list[str] = []

    symbols_file = getattr(args, "symbols_file", None)
    if symbols_file:
        sources.append("symbols_file")
        symbols.extend(_load_symbols_from_text(symbols_file))

    by_date_file = getattr(args, "by_date_file", None)
    if by_date_file:
        sources.append("by_date_file_target_date")
        by_date_symbols = _load_symbols_from_by_date_target_date(by_date_file, target_date=target_date)
        if not by_date_symbols:
            raise SystemExit(
                "No symbols matched --by-date-file on target date "
                f"{_format_date(target_date)}: {_resolve_path(by_date_file)}"
            )
        symbols.extend(by_date_symbols)

    normalized = _normalize_symbol_list(symbols)
    return {
        "source": "+".join(sources) if sources else "all_asset_symbols",
        "symbols": normalized,
        "symbols_file": str(_resolve_path(symbols_file)) if symbols_file else None,
        "by_date_file": str(_resolve_path(by_date_file)) if by_date_file else None,
    }


def _append_sample(values: list[str], item: str, *, limit: int) -> None:
    if item not in values and len(values) < limit:
        values.append(item)


def _combine_samples(*sample_lists: Sequence[object], limit: int) -> list[str]:
    combined: list[str] = []
    for sample_list in sample_lists:
        for item in sample_list:
            text = str(item or "").strip()
            if not text or text in combined:
                continue
            combined.append(text)
            if len(combined) >= limit:
                return combined
    return combined


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


def _placeholder_mask(series: pd.Series) -> pd.Series:
    text = series.map(lambda value: str(value).strip().lower() if not pd.isna(value) else "")
    return series.notna() & text.isin(PLACEHOLDER_TOKENS)


def _assess_target_series(target_series: pd.Series) -> dict[str, object]:
    raw_nonnull_mask = target_series.notna()
    placeholder_mask = _placeholder_mask(target_series)
    numeric = pd.to_numeric(target_series, errors="coerce")
    nonfinite_mask = raw_nonnull_mask & numeric.notna() & ~np.isfinite(numeric.to_numpy(dtype="float64"))
    clean_mask = raw_nonnull_mask & ~placeholder_mask & ~nonfinite_mask
    clean_series = target_series.loc[clean_mask]
    clean_numeric = numeric.loc[clean_mask & numeric.notna()]
    representative_clean_value = None
    if not clean_series.empty:
        first_clean_index = clean_series.index[0]
        numeric_value = numeric.loc[first_clean_index]
        if pd.notna(numeric_value) and np.isfinite(float(numeric_value)):
            representative_clean_value = _serialize_scalar(float(numeric_value))
        else:
            representative_clean_value = _serialize_scalar(clean_series.iloc[0])

    has_zero = False
    if not clean_numeric.empty:
        clean_numeric_values = clean_numeric.to_numpy(dtype="float64")
        has_zero = bool(np.all(np.isfinite(clean_numeric_values)) and np.all(clean_numeric_values == 0.0))

    return {
        "has_raw_nonnull": bool(raw_nonnull_mask.any()),
        "has_placeholder": bool(placeholder_mask.any()),
        "has_nonfinite": bool(nonfinite_mask.any()),
        "has_clean": bool(clean_mask.any()),
        "has_zero": has_zero,
        "representative_clean_value": representative_clean_value,
    }


def _build_history_clean_value_keys(series: pd.Series) -> pd.Series:
    value_keys = pd.Series(None, index=series.index, dtype=object)
    raw_nonnull_mask = series.notna()
    if not bool(raw_nonnull_mask.any()):
        return value_keys

    placeholder_mask = _placeholder_mask(series)
    numeric = pd.to_numeric(series, errors="coerce")
    finite_numeric_mask = raw_nonnull_mask & numeric.notna() & np.isfinite(numeric.to_numpy(dtype="float64"))
    if bool(finite_numeric_mask.any()):
        value_keys.loc[finite_numeric_mask] = numeric.loc[finite_numeric_mask].map(
            lambda item: _serialize_scalar(float(item))
        )

    clean_text_mask = raw_nonnull_mask & ~placeholder_mask & numeric.isna()
    if bool(clean_text_mask.any()):
        value_keys.loc[clean_text_mask] = series.loc[clean_text_mask].map(
            lambda value: _serialize_scalar(str(value).strip())
        )
    return value_keys


def _init_history_state(*, dataset: str | None) -> dict[str, object]:
    issue_template = {
        "daily_price_bounds_violation_any_date": {"severity": "error"},
        "daily_nonpositive_price_any_date": {"severity": "error"},
        "daily_negative_volume_any_date": {"severity": "error"},
        "daily_negative_total_turnover_any_date": {"severity": "error"},
    }
    issues = {}
    if dataset == "daily":
        for check, meta in issue_template.items():
            issues[check] = {
                "check": check,
                "severity": meta["severity"],
                "affected_symbols": set(),
                "affected_rows": 0,
                "sample_rows": [],
            }
    return {
        "dataset": dataset,
        "symbols_scanned": 0,
        "symbols_skipped": 0,
        "rows_scanned": 0,
        "date_min": None,
        "date_max": None,
        "issues": issues,
        "truncated": False,
        "truncation_reason": None,
    }


def _append_history_sample_row(
    values: list[dict[str, object]],
    row: Mapping[str, object],
    *,
    limit: int,
) -> None:
    if len(values) >= limit:
        return
    candidate = {key: _serialize_scalar(value) for key, value in row.items()}
    for existing in values:
        if existing == candidate:
            return
    values.append(candidate)


def _update_daily_history_state(
    *,
    work: pd.DataFrame,
    symbol: str,
    date_column: str,
    history_state: dict[str, object],
    sample_limit: int,
) -> None:
    required_price_fields = ("open", "high", "low", "close")
    missing_price_fields = [field for field in required_price_fields if field not in work.columns]
    price_frame = {
        field: pd.to_numeric(work[field], errors="coerce")
        for field in required_price_fields
        if field in work.columns
    }
    if not missing_price_fields:
        open_arr = price_frame["open"].to_numpy(dtype="float64")
        high_arr = price_frame["high"].to_numpy(dtype="float64")
        low_arr = price_frame["low"].to_numpy(dtype="float64")
        close_arr = price_frame["close"].to_numpy(dtype="float64")
        finite_mask = (
            np.isfinite(open_arr)
            & np.isfinite(high_arr)
            & np.isfinite(low_arr)
            & np.isfinite(close_arr)
        )
        upper_bound = np.maximum.reduce([open_arr, low_arr, close_arr])
        lower_bound = np.minimum.reduce([open_arr, high_arr, close_arr])
        bounds_mask = finite_mask & ((high_arr < upper_bound) | (low_arr > lower_bound))
        nonpositive_mask = finite_mask & (
            np.minimum.reduce([open_arr, high_arr, low_arr, close_arr]) <= 0.0
        )

        for check_name, mask in (
            ("daily_price_bounds_violation_any_date", bounds_mask),
            ("daily_nonpositive_price_any_date", nonpositive_mask),
        ):
            check_state = history_state["issues"][check_name]
            affected_rows = int(np.count_nonzero(mask))
            if affected_rows <= 0:
                continue
            check_state["affected_rows"] = int(check_state["affected_rows"]) + affected_rows
            check_state["affected_symbols"].add(symbol)
            sample_indices = np.flatnonzero(mask)[:sample_limit]
            for idx in sample_indices:
                _append_history_sample_row(
                    check_state["sample_rows"],
                    {
                        "symbol": symbol,
                        "trade_date": work.iloc[idx][date_column],
                        "open": open_arr[idx],
                        "high": high_arr[idx],
                        "low": low_arr[idx],
                        "close": close_arr[idx],
                    },
                    limit=sample_limit,
                )

    for field_name, check_name in (
        ("volume", "daily_negative_volume_any_date"),
        ("total_turnover", "daily_negative_total_turnover_any_date"),
    ):
        if field_name not in work.columns:
            continue
        numeric = pd.to_numeric(work[field_name], errors="coerce").to_numpy(dtype="float64")
        mask = np.isfinite(numeric) & (numeric < 0.0)
        affected_rows = int(np.count_nonzero(mask))
        if affected_rows <= 0:
            continue
        check_state = history_state["issues"][check_name]
        check_state["affected_rows"] = int(check_state["affected_rows"]) + affected_rows
        check_state["affected_symbols"].add(symbol)
        sample_indices = np.flatnonzero(mask)[:sample_limit]
        for idx in sample_indices:
            _append_history_sample_row(
                check_state["sample_rows"],
                {
                    "symbol": symbol,
                    "trade_date": work.iloc[idx][date_column],
                    field_name: numeric[idx],
                },
                limit=sample_limit,
            )


def _update_valuation_history_state(
    *,
    work: pd.DataFrame,
    symbol: str,
    date_column: str,
    fields: Sequence[str],
    history_state: dict[str, object],
    sample_limit: int,
    daily_reference: pd.DataFrame | None = None,
    ex_factor_reference: pd.DataFrame | None = None,
    shares_reference: pd.DataFrame | None = None,
    instrument_reference: pd.DataFrame | None = None,
    valuation_reference_loader: _ValuationReferenceLoader | None = None,
) -> None:
    deduped = (
        work.drop_duplicates(subset=[date_column], keep="last")
        .sort_values(date_column)
        .reset_index(drop=True)
    )
    if deduped.empty:
        return

    for field in fields:
        if field == date_column or field not in deduped.columns:
            continue
        value_keys = _build_history_clean_value_keys(deduped[field])
        if not bool(value_keys.notna().any()):
            continue
        change_mask = value_keys.ne(value_keys.shift()) | value_keys.isna()
        run_frame = pd.DataFrame(
            {
                "row_idx": np.arange(len(deduped)),
                date_column: deduped[date_column],
                "value_key": value_keys,
                "group_id": change_mask.cumsum(),
            }
        )
        run_frame = run_frame.loc[run_frame["value_key"].notna()].copy()
        if run_frame.empty:
            continue

        segments = (
            run_frame.groupby("group_id", sort=True)
            .agg(
                run_length=("row_idx", "size"),
                start_row=("row_idx", "min"),
                end_row=("row_idx", "max"),
                start_date=(date_column, "min"),
                end_date=(date_column, "max"),
                stale_value=("value_key", "first"),
            )
            .reset_index(drop=True)
        )
        segments = segments.loc[segments["run_length"] >= VALUATION_STALE_RUN_MIN_LENGTH].copy()
        if segments.empty:
            continue
        segments = segments.sort_values(
            ["run_length", "end_date", "start_date"],
            ascending=[False, False, False],
        ).reset_index(drop=True)
        if valuation_reference_loader is not None:
            references = valuation_reference_loader.load()
            daily_reference = references.daily
            ex_factor_reference = references.ex_factor
            shares_reference = references.shares
            instrument_reference = references.instrument

        grouped_segments: dict[str, list[dict[str, object]]] = {
            "actionable": [],
            "provider_like": [],
        }
        for _, segment in segments.iterrows():
            context = _classify_valuation_reference_window(
                start_date=segment["start_date"],
                end_date=segment["end_date"],
                daily_reference=daily_reference,
                ex_factor_reference=ex_factor_reference,
                shares_reference=shares_reference,
                instrument_reference=instrument_reference,
            )
            group = "provider_like" if context["provider_like"] else "actionable"
            grouped_segments[group].append(
                {
                    "symbol": symbol,
                    "start_date": segment["start_date"],
                    "end_date": segment["end_date"],
                    "run_length": int(segment["run_length"]),
                    "span_days": int(
                        (
                            pd.to_datetime(segment["end_date"])
                            - pd.to_datetime(segment["start_date"])
                        ).days
                    ),
                    "stale_value": segment["stale_value"],
                    "reference_context": context["reason"],
                }
            )

        for group, group_segments in grouped_segments.items():
            if not group_segments:
                continue
            check_name = (
                "valuation_stale_run_provider_like_any_date"
                if group == "provider_like"
                else "valuation_stale_run_any_date"
            )
            check_key = f"{check_name}::{field}"
            check_state = history_state["issues"].setdefault(
                check_key,
                {
                    "check": check_name,
                    "field": field,
                    "severity": "info" if group == "provider_like" else "warning",
                    "stale_run_min_length": VALUATION_STALE_RUN_MIN_LENGTH,
                    "affected_symbols": set(),
                    "affected_rows": 0,
                    "sample_rows": [],
                    "run_lengths": [],
                    "symbol_max_run_lengths": {},
                },
            )
            check_state["affected_symbols"].add(symbol)
            check_state["affected_rows"] = int(check_state["affected_rows"]) + int(
                sum(int(item["run_length"]) for item in group_segments)
            )
            check_state["run_lengths"].extend(int(item["run_length"]) for item in group_segments)
            symbol_max_run_lengths = check_state["symbol_max_run_lengths"]
            symbol_max_run_lengths[symbol] = max(
                int(symbol_max_run_lengths.get(symbol) or 0),
                max(int(item["run_length"]) for item in group_segments),
            )
            for segment in group_segments[:sample_limit]:
                _append_history_sample_row(
                    check_state["sample_rows"],
                    segment,
                    limit=sample_limit,
                )


def _classify_valuation_reference_window(
    *,
    start_date: object,
    end_date: object,
    daily_reference: pd.DataFrame | None,
    ex_factor_reference: pd.DataFrame | None = None,
    shares_reference: pd.DataFrame | None = None,
    instrument_reference: pd.DataFrame | None = None,
) -> dict[str, object]:
    start_ts = pd.to_datetime(start_date, errors="coerce").normalize()
    end_ts = pd.to_datetime(end_date, errors="coerce").normalize()
    if instrument_reference is not None and not instrument_reference.empty:
        delisted_rows = instrument_reference.loc[instrument_reference["de_listed_date"].notna()].copy()
        if not delisted_rows.empty:
            delisted_rows["boundary_days"] = (
                delisted_rows["de_listed_date"] - end_ts
            ).dt.days
            boundary_rows = delisted_rows.loc[
                (delisted_rows["boundary_days"] >= 0)
                & (delisted_rows["boundary_days"] <= VALUATION_DELIST_BOUNDARY_MAX_DAYS)
            ]
            if not boundary_rows.empty:
                return {
                    "provider_like": True,
                    "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["delisted_instrument_boundary"],
                }
    if ex_factor_reference is not None and not ex_factor_reference.empty:
        ex_window = ex_factor_reference.loc[
            (ex_factor_reference["ex_date"] >= start_ts) & (ex_factor_reference["ex_date"] <= end_ts)
        ]
        if not ex_window.empty:
            return {
                "provider_like": True,
                "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["ex_factor_event_in_window"],
            }
    if shares_reference is not None and not shares_reference.empty:
        shares_window = shares_reference.loc[
            (shares_reference["date"] >= start_ts) & (shares_reference["date"] <= end_ts)
        ]
        if not shares_window.empty:
            return {
                "provider_like": True,
                "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["shares_event_in_window"],
            }
        near_window_start = start_ts - pd.Timedelta(days=VALUATION_SHARES_EVENT_LEAD_DAYS)
        shares_near_window = shares_reference.loc[
            (shares_reference["date"] >= near_window_start) & (shares_reference["date"] < start_ts)
        ]
        if not shares_near_window.empty:
            return {
                "provider_like": True,
                "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["shares_event_near_window"],
            }

    if daily_reference is None or daily_reference.empty:
        return {
            "provider_like": False,
            "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["no_daily_reference"],
        }

    reference_window = daily_reference.loc[
        (daily_reference["trade_date"] >= start_ts) & (daily_reference["trade_date"] <= end_ts)
    ]
    if reference_window.empty:
        return {
            "provider_like": True,
            "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["no_daily_reference_window"],
        }

    close_values = pd.to_numeric(reference_window["close"], errors="coerce")
    finite_mask = np.isfinite(close_values.to_numpy(dtype="float64"))
    if not bool(finite_mask.any()):
        return {
            "provider_like": True,
            "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["no_finite_daily_close"],
        }

    reference_window = reference_window.loc[finite_mask].copy()
    close_values = close_values.loc[finite_mask]
    if {"volume", "total_turnover"}.issubset(reference_window.columns):
        volume_raw = pd.to_numeric(reference_window["volume"], errors="coerce")
        turnover_raw = pd.to_numeric(reference_window["total_turnover"], errors="coerce")
        has_trading_observations = bool(volume_raw.notna().any() or turnover_raw.notna().any())
        volume_values = volume_raw.fillna(0.0)
        turnover_values = turnover_raw.fillna(0.0)
        if has_trading_observations and not bool(((volume_values > 0) | (turnover_values > 0)).any()):
            return {
                "provider_like": True,
                "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["no_daily_trading_activity"],
            }
    latest_trade_date = pd.to_datetime(reference_window["trade_date"], errors="coerce").max()
    if pd.notna(latest_trade_date) and latest_trade_date.normalize() < end_ts:
        return {
            "provider_like": True,
            "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["daily_reference_stale"],
        }
    if int(close_values.nunique(dropna=True)) <= 1:
        return {
            "provider_like": True,
            "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["no_daily_price_change"],
        }
    return {
        "provider_like": False,
        "reason": VALUATION_PROVIDER_LIKE_REASON_LABELS["daily_price_changed"],
    }


def _classify_valuation_fresh_target_gap(
    *,
    field: str,
    work: pd.DataFrame,
    target_frame: pd.DataFrame,
    date_column: str,
    start_date: object,
    end_date: object,
) -> dict[str, object]:
    if field == "hk_total_market_val":
        return {"fresh_gap": False, "reason": None}
    if "hk_total_market_val" not in target_frame.columns or "hk_total_market_val" not in work.columns:
        return {"fresh_gap": False, "reason": None}

    target_market_val = pd.to_numeric(target_frame["hk_total_market_val"], errors="coerce")
    target_finite_mask = np.isfinite(target_market_val.to_numpy(dtype="float64"))
    if not bool(target_finite_mask.any()):
        return {"fresh_gap": False, "reason": None}

    start_ts = pd.to_datetime(start_date, errors="coerce").normalize()
    end_ts = pd.to_datetime(end_date, errors="coerce").normalize()
    market_window = work.loc[
        (work[date_column] >= start_ts) & (work[date_column] <= end_ts),
        "hk_total_market_val",
    ]
    market_values = pd.to_numeric(market_window, errors="coerce")
    finite_market_values = market_values[np.isfinite(market_values.to_numpy(dtype="float64"))]
    if finite_market_values.empty:
        return {
            "fresh_gap": True,
            "reason": VALUATION_FRESH_TARGET_GAP_REASON_LABELS["target_market_val_present"],
        }
    if int(finite_market_values.nunique(dropna=True)) > 1:
        return {
            "fresh_gap": True,
            "reason": VALUATION_FRESH_TARGET_GAP_REASON_LABELS["target_market_val_changed"],
        }
    return {
        "fresh_gap": True,
        "reason": VALUATION_FRESH_TARGET_GAP_REASON_LABELS["target_market_val_present"],
    }


def _finalize_history_payload(history_state: Mapping[str, object] | None) -> dict[str, object] | None:
    if not history_state:
        return None

    issues = history_state.get("issues") if isinstance(history_state.get("issues"), Mapping) else {}
    issue_rows: list[dict[str, object]] = []
    for check_name, check_state in issues.items():
        if not isinstance(check_state, Mapping):
            continue
        affected_symbols = check_state.get("affected_symbols")
        affected_symbol_count = len(affected_symbols) if isinstance(affected_symbols, set) else 0
        affected_rows = int(check_state.get("affected_rows") or 0)
        if affected_rows <= 0 and affected_symbol_count <= 0:
            continue
        row = {
            "check": check_state.get("check") or check_name,
            "severity": check_state.get("severity"),
            "affected_symbols": affected_symbol_count,
            "affected_rows": affected_rows,
            "sample_rows": list(check_state.get("sample_rows") or []),
        }
        if check_state.get("field"):
            row["field"] = check_state.get("field")
        if check_state.get("stale_run_min_length"):
            row["stale_run_min_length"] = int(check_state.get("stale_run_min_length") or 0)
        run_lengths = [int(item) for item in (check_state.get("run_lengths") or [])]
        if run_lengths:
            row["run_length_p50"] = _quantile_or_none(run_lengths, 0.5)
            row["run_length_p90"] = _quantile_or_none(run_lengths, 0.9)
            row["run_length_max"] = max(run_lengths)
            symbol_max_run_lengths = check_state.get("symbol_max_run_lengths")
            if isinstance(symbol_max_run_lengths, Mapping):
                row["run_length_gt_3_symbols"] = int(
                    sum(int(value) > 3 for value in symbol_max_run_lengths.values())
                )
                row["run_length_gt_5_symbols"] = int(
                    sum(int(value) > 5 for value in symbol_max_run_lengths.values())
                )
                row["run_length_gt_10_symbols"] = int(
                    sum(int(value) > 10 for value in symbol_max_run_lengths.values())
                )
        issue_rows.append(row)

    issue_rows.sort(
        key=lambda item: (
            -int(item.get("affected_rows") or 0),
            -int(item.get("affected_symbols") or 0),
            str(item.get("check") or ""),
        )
    )
    return {
        "summary": {
            "dataset": history_state.get("dataset"),
            "symbols_scanned": int(history_state.get("symbols_scanned") or 0),
            "rows_scanned": int(history_state.get("rows_scanned") or 0),
            "date_min": _format_date(history_state.get("date_min")),
            "date_max": _format_date(history_state.get("date_max")),
            "issue_count": len(issue_rows),
        },
        "issues": issue_rows,
    }


def _load_daily_reference_frame(daily_asset_dir: Path | None, symbol: str) -> pd.DataFrame | None:
    if daily_asset_dir is None:
        return None
    daily_path = daily_asset_dir / "data" / f"{symbol}.parquet"
    if not daily_path.exists():
        return None
    try:
        frame = pd.read_parquet(daily_path, columns=["trade_date", "close", "volume", "total_turnover"])
    except Exception:
        frame = pd.read_parquet(daily_path)
    frame = _normalize_frame_columns(frame)
    if "trade_date" not in frame.columns:
        return None
    if "close" not in frame.columns:
        frame["close"] = np.nan
    if "volume" not in frame.columns:
        frame["volume"] = np.nan
    if "total_turnover" not in frame.columns:
        frame["total_turnover"] = np.nan
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["total_turnover"] = pd.to_numeric(frame["total_turnover"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"]).copy()
    if frame.empty:
        return None
    return (
        frame[["trade_date", "close", "volume", "total_turnover"]]
        .drop_duplicates(subset=["trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def _resolve_default_hk_ex_factor_asset_dir(asset_dir: Path) -> Path | None:
    hk_root = asset_dir.parent.parent
    if hk_root.name != "hk":
        return None
    ex_factor_root = hk_root / "ex_factors"
    if not ex_factor_root.exists():
        return None
    preferred = ex_factor_root / "hk_all_ex_factors_latest"
    if preferred.exists() and (preferred / "data").exists():
        return preferred
    return None


def _resolve_default_hk_shares_asset_dir(asset_dir: Path) -> Path | None:
    hk_root = asset_dir.parent.parent
    if hk_root.name != "hk":
        return None
    shares_root = hk_root / "shares"
    if not shares_root.exists():
        return None
    preferred = shares_root / "hk_all_shares_latest"
    if preferred.exists() and (preferred / "data").exists():
        return preferred
    candidates = sorted(
        [path for path in shares_root.iterdir() if path.is_dir() and (path / "data").exists()],
        key=lambda path: (-path.stat().st_mtime, path.name),
    )
    return candidates[0] if candidates else None


def _resolve_default_hk_instruments_path(asset_dir: Path) -> Path | None:
    hk_root = asset_dir.parent.parent
    if hk_root.name != "hk":
        return None
    instruments_dir = hk_root / "instruments"
    if not instruments_dir.exists():
        return None
    preferred = instruments_dir / "hk_all_instruments_latest.parquet"
    if preferred.exists():
        return preferred
    candidates = sorted(
        instruments_dir.glob("hk_all_instruments*.parquet"),
        key=lambda path: (-path.stat().st_mtime, path.name),
    )
    return candidates[0] if candidates else None


def _load_ex_factor_reference_frame(ex_factor_asset_dir: Path | None, symbol: str) -> pd.DataFrame | None:
    if ex_factor_asset_dir is None:
        return None
    ex_factor_path = ex_factor_asset_dir / "data" / f"{symbol}.parquet"
    if not ex_factor_path.exists():
        return None
    try:
        frame = pd.read_parquet(ex_factor_path, columns=["ex_date"])
    except Exception:
        frame = pd.read_parquet(ex_factor_path)
    frame = _normalize_frame_columns(frame)
    if "ex_date" not in frame.columns:
        return None
    frame["ex_date"] = pd.to_datetime(frame["ex_date"], errors="coerce").dt.normalize()
    frame = frame.dropna(subset=["ex_date"]).copy()
    if frame.empty:
        return None
    return (
        frame[["ex_date"]]
        .drop_duplicates(subset=["ex_date"], keep="last")
        .sort_values("ex_date")
        .reset_index(drop=True)
    )


def _load_shares_reference_frame(shares_asset_dir: Path | None, symbol: str) -> pd.DataFrame | None:
    if shares_asset_dir is None:
        return None
    shares_path = shares_asset_dir / "data" / f"{symbol}.parquet"
    if not shares_path.exists():
        return None
    try:
        frame = pd.read_parquet(shares_path, columns=["date"])
    except Exception:
        frame = pd.read_parquet(shares_path)
    frame = _normalize_frame_columns(frame)
    if "date" not in frame.columns:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame = frame.dropna(subset=["date"]).copy()
    if frame.empty:
        return None
    return (
        frame[["date"]]
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _load_hk_instrument_reference_map(instruments_path: Path | None) -> dict[str, pd.DataFrame]:
    if instruments_path is None or not instruments_path.exists():
        return {}
    frame = _normalize_frame_columns(pd.read_parquet(instruments_path))
    if "symbol" not in frame.columns and "ts_code" in frame.columns:
        frame = frame.rename(columns={"ts_code": "symbol"})
    if "symbol" not in frame.columns:
        return {}
    frame["symbol"] = frame["symbol"].map(_normalize_hk_symbol)
    frame = frame[frame["symbol"] != ""].copy()
    if frame.empty:
        return {}
    if "de_listed_date" in frame.columns:
        de_listed_text = frame["de_listed_date"].astype(str).str.strip()
        de_listed_text = de_listed_text.mask(de_listed_text == "0000-00-00")
        frame["de_listed_date"] = pd.to_datetime(de_listed_text, errors="coerce").dt.normalize()
    else:
        frame["de_listed_date"] = pd.NaT
    if "listed_date" in frame.columns:
        frame["listed_date"] = pd.to_datetime(frame["listed_date"], errors="coerce").dt.normalize()
    else:
        frame["listed_date"] = pd.NaT
    if "status" not in frame.columns:
        frame["status"] = ""
    frame["status"] = frame["status"].fillna("").astype(str).str.strip()
    grouped: dict[str, pd.DataFrame] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        grouped[str(symbol)] = group[
            ["symbol", "listed_date", "de_listed_date", "status"]
        ].sort_values(["de_listed_date", "listed_date"], kind="mergesort").reset_index(drop=True)
    return grouped


def _build_clean_missing_quality_check(
    *,
    row: Mapping[str, object],
    field: str,
    dataset: str | None,
    denominator: int,
    unusable: int,
    provider_like_unusable: int,
    sample_limit: int,
) -> dict[str, object] | None:
    if denominator <= 0:
        return None
    if dataset == "valuation" and provider_like_unusable > 0 and provider_like_unusable == unusable:
        return {
            "check": "field_all_clean_missing_on_target_date_provider_like",
            "field": field,
            "severity": "info",
            "affected_symbols": denominator,
            "affected_pct": _round_pct(denominator, denominator),
            "sample_symbols": [
                str(item.get("symbol"))
                for item in (row.get("sample_provider_like_ffill_symbols") or [])
                if isinstance(item, Mapping)
            ][:sample_limit],
        }
    return {
        "check": "field_all_clean_missing_on_target_date",
        "field": field,
        "severity": "error",
        "affected_symbols": denominator,
        "affected_pct": _round_pct(denominator, denominator),
        "sample_symbols": _combine_samples(
            row.get("sample_unusable_symbols") or [],
            row.get("sample_prior_clean_symbols") or [],
            row.get("sample_missing_symbols") or [],
            limit=sample_limit,
        ),
    }


def _build_basic_field_quality_checks(
    *,
    row: Mapping[str, object],
    field: str,
    dataset: str | None,
    denominator: int,
    clean_nonmissing: int,
    placeholder_count: int,
    nonfinite_count: int,
    zero_count: int,
    sample_limit: int,
) -> list[dict[str, object]]:
    quality_checks: list[dict[str, object]] = []
    if placeholder_count > 0:
        quality_checks.append(
            {
                "check": "field_placeholder_values_on_target_date",
                "field": field,
                "severity": "warning",
                "affected_symbols": placeholder_count,
                "affected_pct": _round_pct(placeholder_count, denominator),
                "sample_symbols": list(row.get("sample_placeholder_symbols") or []),
            }
        )
    if nonfinite_count > 0:
        quality_checks.append(
            {
                "check": "field_nonfinite_values_on_target_date",
                "field": field,
                "severity": "error",
                "affected_symbols": nonfinite_count,
                "affected_pct": _round_pct(nonfinite_count, denominator),
                "sample_symbols": list(row.get("sample_nonfinite_symbols") or []),
            }
        )
    if (
        clean_nonmissing > 0
        and zero_count == clean_nonmissing
        and row.get("most_common_clean_value_on_target_date") == 0
    ):
        quality_checks.append(
            {
                "check": "field_all_clean_values_zero_on_target_date",
                "field": field,
                "severity": "warning",
                "affected_symbols": zero_count,
                "affected_pct": _round_pct(zero_count, clean_nonmissing),
                "sample_symbols": list(row.get("sample_zero_symbols") or []),
            }
        )
    constant_cross_section = bool(row.get("is_constant_across_clean_values_on_target_date"))
    if (
        clean_nonmissing > 1
        and constant_cross_section
        and not _skip_constant_cross_section_quality_check(dataset=dataset, field=field)
    ):
        quality_checks.append(
            {
                "check": "field_constant_cross_section_on_target_date",
                "field": field,
                "severity": "warning",
                "affected_symbols": clean_nonmissing,
                "affected_pct": _round_pct(clean_nonmissing, denominator),
                "sample_symbols": list(row.get("sample_clean_symbols") or [])[:sample_limit],
            }
        )
    return quality_checks


def _build_field_age_quality_checks(
    *,
    row: Mapping[str, object],
    field: str,
    unusable: int,
    sample_limit: int,
    provider_like: bool,
) -> list[dict[str, object]]:
    if provider_like:
        thresholds = ((10, "warning"), (5, "info"), (1, "info"))
        count_key = "provider_ffill_age_gt_{threshold}d_symbols"
        check_prefix = "field_provider_like_ffill_age_gt"
        sample_key = "sample_provider_like_ffill_symbols"
    else:
        thresholds = ((10, "error"), (5, "warning"), (1, "info"))
        count_key = "ffill_age_gt_{threshold}d_symbols"
        check_prefix = "field_ffill_age_gt"
        sample_key = "sample_oldest_ffill_symbols"

    quality_checks: list[dict[str, object]] = []
    for threshold, severity in thresholds:
        affected = int(row.get(count_key.format(threshold=threshold)) or 0)
        if affected <= 0:
            continue
        quality_checks.append(
            {
                "check": f"{check_prefix}_{threshold}d",
                "field": field,
                "severity": severity,
                "affected_symbols": affected,
                "affected_pct": _round_pct(affected, unusable),
                "sample_symbols": [
                    str(item.get("symbol"))
                    for item in (row.get(sample_key) or [])
                    if isinstance(item, Mapping)
                ][:sample_limit],
            }
        )
    return quality_checks


def _build_field_quality_checks(
    *,
    field_rows: list[dict[str, object]],
    dataset: str | None,
    sample_limit: int,
) -> list[dict[str, object]]:
    quality_checks: list[dict[str, object]] = []
    for row in field_rows:
        if not isinstance(row, Mapping):
            continue
        field = str(row.get("field") or "")
        denominator = int(row.get("symbols_with_target_date_row") or 0)
        clean_nonmissing = int(row.get("clean_nonmissing_on_target_date") or 0)
        unusable = int(row.get("unusable_on_target_date") or 0)
        provider_like_unusable = int(row.get("provider_like_unusable_on_target_date") or 0)
        placeholder_count = int(row.get("placeholder_on_target_date") or 0)
        nonfinite_count = int(row.get("nonfinite_on_target_date") or 0)
        zero_count = int(row.get("zero_on_target_date") or 0)
        if denominator > 0 and clean_nonmissing == 0:
            check = _build_clean_missing_quality_check(
                row=row,
                field=field,
                dataset=dataset,
                denominator=denominator,
                unusable=unusable,
                provider_like_unusable=provider_like_unusable,
                sample_limit=sample_limit,
            )
            if check is not None:
                quality_checks.append(
                    check
                )
        quality_checks.extend(
            _build_basic_field_quality_checks(
                row=row,
                field=field,
                dataset=dataset,
                denominator=denominator,
                clean_nonmissing=clean_nonmissing,
                placeholder_count=placeholder_count,
                nonfinite_count=nonfinite_count,
                zero_count=zero_count,
                sample_limit=sample_limit,
            )
        )
        quality_checks.extend(
            _build_field_age_quality_checks(
                row=row,
                field=field,
                unusable=unusable,
                sample_limit=sample_limit,
                provider_like=False,
            )
        )
        quality_checks.extend(
            _build_field_age_quality_checks(
                row=row,
                field=field,
                unusable=unusable,
                sample_limit=sample_limit,
                provider_like=True,
            )
        )
    return quality_checks


def _build_field_coverage_rows(
    *,
    selected_fields: Sequence[str],
    field_stats: Mapping[str, Mapping[str, object]],
    sample_limit: int,
) -> list[dict[str, object]]:
    field_rows: list[dict[str, object]] = []
    for field in selected_fields:
        stats = field_stats[field]
        denominator = int(stats["symbols_with_target_date_row"])
        nonnull = int(stats["nonnull_on_target_date"])
        clean_nonmissing = int(stats["clean_nonmissing_on_target_date"])
        missing = int(stats["missing_on_target_date"])
        missing_but_prior = int(stats["missing_but_prior_nonnull"])
        placeholder = int(stats["placeholder_on_target_date"])
        nonfinite = int(stats["nonfinite_on_target_date"])
        zero = int(stats["zero_on_target_date"])
        unusable = int(max(denominator - clean_nonmissing, 0))
        unusable_but_prior_clean = int(stats["unusable_but_prior_clean"])
        clean_value_counter = stats["clean_value_counter"]
        unique_clean_values = int(len(clean_value_counter))
        most_common_clean_value = None
        most_common_clean_value_symbols = 0
        if clean_value_counter:
            most_common_clean_value, most_common_clean_value_symbols = sorted(
                clean_value_counter.items(),
                key=lambda item: (-int(item[1]), str(item[0])),
            )[0]

        ffill_age_records = sorted(
            stats["ffill_age_records"],
            key=lambda item: (-int(item["age_days"]), str(item["symbol"])),
        )
        ffill_ages = [int(item["age_days"]) for item in ffill_age_records]
        provider_ffill_age_records = sorted(
            stats["provider_ffill_age_records"],
            key=lambda item: (-int(item["age_days"]), str(item["symbol"])),
        )
        provider_ffill_ages = [int(item["age_days"]) for item in provider_ffill_age_records]
        provider_like_unusable = int(len(provider_ffill_age_records))
        fresh_target_gap_records = sorted(
            stats["fresh_target_gap_records"],
            key=lambda item: (-int(item["age_days"]), str(item["symbol"])),
        )
        fresh_target_gap_count = int(len(fresh_target_gap_records))
        field_rows.append(
            {
                "field": field,
                "symbols_with_target_date_row": denominator,
                "nonnull_on_target_date": nonnull,
                "nonnull_pct_on_target_date": _round_pct(nonnull, denominator),
                "clean_nonmissing_on_target_date": clean_nonmissing,
                "clean_nonmissing_pct_on_target_date": _round_pct(clean_nonmissing, denominator),
                "unusable_on_target_date": unusable,
                "unusable_pct_on_target_date": _round_pct(unusable, denominator),
                "missing_on_target_date": missing,
                "missing_pct_on_target_date": _round_pct(missing, denominator),
                "missing_but_prior_nonnull": missing_but_prior,
                "missing_but_prior_nonnull_pct_of_missing": _round_pct(missing_but_prior, missing),
                "missing_and_never_nonnull": int(stats["missing_and_never_nonnull"]),
                "placeholder_on_target_date": placeholder,
                "placeholder_pct_on_target_date": _round_pct(placeholder, denominator),
                "nonfinite_on_target_date": nonfinite,
                "nonfinite_pct_on_target_date": _round_pct(nonfinite, denominator),
                "zero_on_target_date": zero,
                "zero_pct_of_clean_nonmissing": _round_pct(zero, clean_nonmissing),
                "unusable_but_prior_clean": unusable_but_prior_clean,
                "unusable_but_prior_clean_pct_of_unusable": _round_pct(
                    unusable_but_prior_clean,
                    unusable,
                ),
                "provider_like_unusable_on_target_date": provider_like_unusable,
                "provider_like_unusable_pct_of_unusable": _round_pct(
                    provider_like_unusable,
                    unusable,
                ),
                "fresh_target_gap_on_target_date": fresh_target_gap_count,
                "fresh_target_gap_pct_of_unusable": _round_pct(fresh_target_gap_count, unusable),
                "ffill_age_days_min": min(ffill_ages) if ffill_ages else None,
                "ffill_age_days_p50": _quantile_or_none(ffill_ages, 0.5),
                "ffill_age_days_p90": _quantile_or_none(ffill_ages, 0.9),
                "ffill_age_days_max": max(ffill_ages) if ffill_ages else None,
                "ffill_age_gt_1d_symbols": int(sum(age > 1 for age in ffill_ages)),
                "ffill_age_gt_5d_symbols": int(sum(age > 5 for age in ffill_ages)),
                "ffill_age_gt_10d_symbols": int(sum(age > 10 for age in ffill_ages)),
                "provider_ffill_age_days_min": (
                    min(provider_ffill_ages) if provider_ffill_ages else None
                ),
                "provider_ffill_age_days_p50": _quantile_or_none(provider_ffill_ages, 0.5),
                "provider_ffill_age_days_p90": _quantile_or_none(provider_ffill_ages, 0.9),
                "provider_ffill_age_days_max": (
                    max(provider_ffill_ages) if provider_ffill_ages else None
                ),
                "provider_ffill_age_gt_1d_symbols": int(
                    sum(age > 1 for age in provider_ffill_ages)
                ),
                "provider_ffill_age_gt_5d_symbols": int(
                    sum(age > 5 for age in provider_ffill_ages)
                ),
                "provider_ffill_age_gt_10d_symbols": int(
                    sum(age > 10 for age in provider_ffill_ages)
                ),
                "unique_clean_values_on_target_date": unique_clean_values,
                "most_common_clean_value_on_target_date": most_common_clean_value,
                "most_common_clean_value_symbols": int(most_common_clean_value_symbols),
                "most_common_clean_value_pct_of_clean_nonmissing": _round_pct(
                    int(most_common_clean_value_symbols),
                    clean_nonmissing,
                ),
                "is_constant_across_clean_values_on_target_date": bool(
                    clean_nonmissing > 0 and unique_clean_values == 1
                ),
                "sample_missing_symbols": list(stats["sample_missing_symbols"]),
                "sample_prior_nonnull_symbols": list(stats["sample_prior_nonnull_symbols"]),
                "sample_placeholder_symbols": list(stats["sample_placeholder_symbols"]),
                "sample_nonfinite_symbols": list(stats["sample_nonfinite_symbols"]),
                "sample_zero_symbols": list(stats["sample_zero_symbols"]),
                "sample_clean_symbols": list(stats["sample_clean_symbols"]),
                "sample_prior_clean_symbols": list(stats["sample_prior_clean_symbols"]),
                "sample_unusable_symbols": list(stats["sample_unusable_symbols"]),
                "sample_oldest_ffill_symbols": ffill_age_records[:sample_limit],
                "sample_provider_like_ffill_symbols": provider_ffill_age_records[:sample_limit],
                "sample_fresh_target_gap_symbols": fresh_target_gap_records[:sample_limit],
            }
        )
    return field_rows


def _build_asset_health_payload(
    *,
    asset_dir: Path,
    dataset: str | None,
    target_date: pd.Timestamp,
    target_date_source: str,
    date_column: str,
    selection_source: str,
    selected_fields: Sequence[str],
    manifest: Mapping[str, object] | None,
    manifest_path: Path,
    daily_reference_asset_dir: Path | None,
    symbol_filter: Mapping[str, object],
    symbols_scanned: int,
    all_data_files: Sequence[Path],
    missing_asset_symbols: Sequence[str],
    symbols_with_target_date_row: int,
    duplicate_date_stats: Mapping[str, object],
    latest_min: pd.Timestamp | None,
    latest_max: pd.Timestamp | None,
    status_counts: Counter[str],
    audit_issue_groups: Sequence[Mapping[str, object]],
    quality_checks: Sequence[Mapping[str, object]],
    quality_verdict: Mapping[str, object],
    include_history: bool,
    history_state: Mapping[str, object] | None,
    latest_rows: Sequence[Mapping[str, object]],
    sample_limit: int,
    missing_asset_file_details: Sequence[Mapping[str, object]],
    stale_rows: Sequence[Mapping[str, object]],
    field_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    summary = {
        "asset_dir": str(asset_dir),
        "dataset": dataset,
        "target_date": _format_date(target_date),
        "target_date_source": target_date_source,
        "date_column": date_column,
        "selection_source": selection_source,
        "selected_fields": list(selected_fields),
        "manifest_query_date": _resolve_manifest_query_date(manifest),
        "daily_reference_asset_dir": (
            str(daily_reference_asset_dir) if daily_reference_asset_dir else None
        ),
        "symbol_filter_source": str(symbol_filter["source"]),
        "symbols_file": symbol_filter["symbols_file"],
        "by_date_file": symbol_filter["by_date_file"],
        "symbols_scanned": symbols_scanned,
        "symbols_available_in_asset_dir": len(all_data_files),
        "symbols_missing_asset_file": len(missing_asset_symbols),
        "symbols_with_target_date_row": symbols_with_target_date_row,
        "symbols_without_target_date_row": int(symbols_scanned - symbols_with_target_date_row),
        "target_date_coverage_pct": _round_pct(symbols_with_target_date_row, symbols_scanned),
        "symbols_with_duplicate_dates": int(duplicate_date_stats["symbols"]),
        "duplicate_date_groups": int(duplicate_date_stats["duplicate_date_groups"]),
        "duplicate_date_rows": int(duplicate_date_stats["duplicate_rows"]),
        "latest_date_min": _format_date(latest_min),
        "latest_date_max": _format_date(latest_max),
        "audit_status_counts": dict(sorted(status_counts.items())),
        "audit_issue_group_count": len(audit_issue_groups),
        "quality_check_issue_count": len(quality_checks),
        "include_history": include_history,
        "audit_file": str(asset_dir / "audit.csv") if (asset_dir / "audit.csv").exists() else None,
        "manifest_file": str(manifest_path) if manifest_path.exists() else None,
    }
    history_payload = _finalize_history_payload(history_state)
    if history_payload is not None:
        raw_history_summary = history_payload.get("summary")
        history_summary = raw_history_summary if isinstance(raw_history_summary, Mapping) else {}
        summary["history_issue_count"] = int(history_summary.get("issue_count") or 0)
        summary["history_rows_scanned"] = int(history_summary.get("rows_scanned") or 0)
    return {
        "summary": summary,
        "quality_verdict": quality_verdict,
        "latest_date_distribution": list(latest_rows),
        "sample_missing_asset_file_symbols": list(missing_asset_symbols[:sample_limit]),
        "sample_missing_asset_file_details": list(missing_asset_file_details),
        "audit_issue_groups": list(audit_issue_groups),
        "sample_stale_symbols": list(stale_rows),
        "field_coverage": list(field_rows),
        "quality_checks": list(quality_checks),
        "history": history_payload,
    }


def _write_asset_health_output(args, payload: Mapping[str, object]) -> int:
    output_format = str(getattr(args, "format", "text") or "text").strip().lower()
    if output_format == "json":
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        rendered = _render_asset_health_text(payload)

    out_path = _resolve_path(args.out) if getattr(args, "out", None) else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return quality_gate_exit_code(payload["quality_verdict"])


def _resolve_asset_health_reference_assets(
    args,
    *,
    dataset: str | None,
    asset_dir: Path,
) -> tuple[Path | None, Path | None, Path | None, dict[str, pd.DataFrame]]:
    daily_reference_asset_dir: Path | None = None
    if getattr(args, "daily_asset_dir", None):
        daily_reference_asset_dir = _resolve_path(args.daily_asset_dir)
        if not daily_reference_asset_dir.exists():
            raise SystemExit(
                f"Daily reference asset directory not found: {daily_reference_asset_dir}"
            )
        if not (daily_reference_asset_dir / "data").exists():
            raise SystemExit(
                f"Daily reference asset directory is missing data/: {daily_reference_asset_dir}"
            )

    ex_factor_reference_asset_dir: Path | None = None
    shares_reference_asset_dir: Path | None = None
    instrument_reference_by_symbol: dict[str, pd.DataFrame] = {}
    if dataset == "valuation":
        ex_factor_reference_asset_dir = _resolve_default_hk_ex_factor_asset_dir(asset_dir)
        shares_reference_asset_dir = _resolve_default_hk_shares_asset_dir(asset_dir)
        instrument_reference_by_symbol = _load_hk_instrument_reference_map(
            _resolve_default_hk_instruments_path(asset_dir)
        )
    return (
        daily_reference_asset_dir,
        ex_factor_reference_asset_dir,
        shares_reference_asset_dir,
        instrument_reference_by_symbol,
    )


def _init_asset_health_field_stats(
    selected_fields: Sequence[str],
) -> dict[str, dict[str, object]]:
    return {
        field: {
            "field": field,
            "symbols_with_target_date_row": 0,
            "nonnull_on_target_date": 0,
            "clean_nonmissing_on_target_date": 0,
            "missing_on_target_date": 0,
            "missing_but_prior_nonnull": 0,
            "missing_and_never_nonnull": 0,
            "placeholder_on_target_date": 0,
            "nonfinite_on_target_date": 0,
            "zero_on_target_date": 0,
            "unusable_but_prior_clean": 0,
            "sample_missing_symbols": [],
            "sample_prior_nonnull_symbols": [],
            "sample_placeholder_symbols": [],
            "sample_nonfinite_symbols": [],
            "sample_zero_symbols": [],
            "sample_clean_symbols": [],
            "sample_prior_clean_symbols": [],
            "sample_unusable_symbols": [],
            "clean_value_counter": Counter(),
            "ffill_age_records": [],
            "provider_ffill_age_records": [],
            "fresh_target_gap_records": [],
        }
        for field in selected_fields
    }


def _read_asset_health_symbol_work_frame(
    *,
    path: Path,
    date_column: str,
    selected_fields: Sequence[str],
    dataset: str | None,
) -> pd.DataFrame:
    read_columns = _dedupe_preserve_order(
        [
            date_column,
            *selected_fields,
            *(
                ["hk_total_market_val"]
                if dataset == "valuation" and "hk_total_market_val" not in selected_fields
                else []
            ),
            *_duplicate_key_read_columns(dataset=dataset),
        ],
        strip=True,
    )
    try:
        frame = pd.read_parquet(path, columns=read_columns)
    except Exception:
        frame = pd.read_parquet(path)
    frame = _normalize_frame_columns(frame)
    if date_column not in frame.columns:
        raise SystemExit(f"Date column {date_column} not found in {path}")

    work = frame.copy()
    parsed_dates = pd.to_datetime(work[date_column], errors="coerce").dt.normalize()
    valid = parsed_dates.notna()
    work = work.loc[valid].copy()
    work[date_column] = parsed_dates.loc[valid]
    return work


def _deduplicate_asset_health_symbol_work_frame(
    *,
    work: pd.DataFrame,
    symbol: str,
    dataset: str | None,
    date_column: str,
    duplicate_date_stats: dict[str, object],
    sample_limit: int,
) -> pd.DataFrame:
    duplicate_key_columns = _duplicate_key_columns(
        dataset=dataset,
        date_column=date_column,
        columns=work.columns,
    )
    duplicate_counts = work.value_counts(subset=duplicate_key_columns)
    duplicate_date_count = int((duplicate_counts > 1).sum())
    if duplicate_date_count <= 0:
        return work

    duplicate_date_stats["symbols"] = int(duplicate_date_stats["symbols"]) + 1
    duplicate_date_stats["duplicate_date_groups"] = (
        int(duplicate_date_stats["duplicate_date_groups"]) + duplicate_date_count
    )
    duplicate_date_stats["duplicate_rows"] = int(duplicate_date_stats["duplicate_rows"]) + int(
        duplicate_counts.loc[duplicate_counts > 1].sum()
    )
    _append_sample(
        duplicate_date_stats["sample_symbols"],
        symbol,
        limit=sample_limit,
    )
    # Prefer the last observed row per logical key so downstream health checks stay stable.
    return (
        work.drop_duplicates(subset=duplicate_key_columns, keep="last")
        .sort_values(date_column)
        .reset_index(drop=True)
    )


def _update_asset_health_symbol_history_state(
    *,
    work: pd.DataFrame,
    symbol: str,
    date_column: str,
    dataset: str | None,
    selected_fields: Sequence[str],
    history_state: dict[str, object] | None,
    sample_limit: int,
    daily_reference: pd.DataFrame | None,
    ex_factor_reference: pd.DataFrame | None,
    shares_reference: pd.DataFrame | None,
    instrument_reference: pd.DataFrame | None,
    valuation_reference_loader: _ValuationReferenceLoader | None = None,
) -> None:
    if history_state is None:
        return
    if work.empty:
        return

    history_state["symbols_scanned"] = int(history_state["symbols_scanned"]) + 1
    history_state["rows_scanned"] = int(history_state["rows_scanned"]) + int(len(work))
    history_date_min = work[date_column].min()
    history_date_max = work[date_column].max()
    history_state["date_min"] = (
        history_date_min
        if history_state["date_min"] is None or history_date_min < history_state["date_min"]
        else history_state["date_min"]
    )
    history_state["date_max"] = (
        history_date_max
        if history_state["date_max"] is None or history_date_max > history_state["date_max"]
        else history_state["date_max"]
    )
    if dataset == "daily":
        _update_daily_history_state(
            work=work,
            symbol=symbol,
            date_column=date_column,
            history_state=history_state,
            sample_limit=sample_limit,
        )
    elif dataset == "valuation":
        _update_valuation_history_state(
            work=work,
            symbol=symbol,
            date_column=date_column,
            fields=selected_fields,
            history_state=history_state,
            sample_limit=sample_limit,
            daily_reference=daily_reference,
            ex_factor_reference=ex_factor_reference,
            shares_reference=shares_reference,
            instrument_reference=instrument_reference,
            valuation_reference_loader=valuation_reference_loader,
        )


def _parse_history_date(value: object, *, label: str) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _parse_compact_date(text, label=label).normalize()


def _resolve_history_window(
    args,
    *,
    target_date: pd.Timestamp,
) -> dict[str, object]:
    tail_days_raw = getattr(args, "history_tail_days", None)
    tail_days = int(tail_days_raw) if tail_days_raw not in (None, "") else None
    if tail_days is not None and tail_days <= 0:
        raise SystemExit("--history-tail-days must be > 0 when provided.")

    start = _parse_history_date(
        getattr(args, "history_start_date", None),
        label="--history-start-date",
    )
    end = _parse_history_date(
        getattr(args, "history_end_date", None),
        label="--history-end-date",
    )
    if tail_days is not None and start is None:
        start = (target_date - pd.Timedelta(days=tail_days - 1)).normalize()
    if start is not None and end is not None and start > end:
        raise SystemExit("--history-start-date must be <= --history-end-date.")

    timeout_raw = getattr(args, "history_timeout_seconds", None)
    timeout_seconds = float(timeout_raw) if timeout_raw not in (None, "") else None
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise SystemExit("--history-timeout-seconds must be > 0 when provided.")

    max_symbols_raw = getattr(args, "history_max_symbols", None)
    max_symbols = int(max_symbols_raw) if max_symbols_raw not in (None, "") else None
    if max_symbols is not None and max_symbols <= 0:
        raise SystemExit("--history-max-symbols must be > 0 when provided.")

    progress_raw = getattr(args, "history_progress_every_symbols", 0)
    progress_every = int(progress_raw or 0)
    if progress_every < 0:
        raise SystemExit("--history-progress-every-symbols must be >= 0.")

    return {
        "start": start,
        "end": end,
        "tail_days": tail_days,
        "timeout_seconds": timeout_seconds,
        "max_symbols": max_symbols,
        "progress_every": progress_every,
        "started_at": time.monotonic(),
    }


def _filter_history_work_frame(
    work: pd.DataFrame,
    *,
    date_column: str,
    history_window: Mapping[str, object],
) -> pd.DataFrame:
    start = history_window.get("start")
    end = history_window.get("end")
    if start is None and end is None:
        return work
    mask = pd.Series(True, index=work.index, dtype=bool)
    if start is not None:
        mask &= work[date_column] >= start
    if end is not None:
        mask &= work[date_column] <= end
    return work.loc[mask].copy()


def _history_scan_exhausted(
    *,
    history_state: dict[str, object] | None,
    history_window: Mapping[str, object],
) -> str | None:
    if history_state is None:
        return None
    max_symbols = history_window.get("max_symbols")
    if max_symbols is not None and int(history_state.get("symbols_scanned") or 0) >= int(max_symbols):
        return "max_symbols"
    timeout_seconds = history_window.get("timeout_seconds")
    if timeout_seconds is not None:
        elapsed = time.monotonic() - float(history_window.get("started_at") or time.monotonic())
        if elapsed >= float(timeout_seconds):
            return "timeout_seconds"
    return None


def _mark_history_scan_truncated(
    *,
    history_state: dict[str, object] | None,
    reason: str,
) -> None:
    if history_state is None:
        return
    history_state["truncated"] = True
    history_state["truncation_reason"] = reason
    history_state["symbols_skipped"] = int(history_state.get("symbols_skipped") or 0) + 1


def _maybe_log_history_progress(
    *,
    history_state: dict[str, object] | None,
    history_window: Mapping[str, object],
    total_symbols: int,
    asset_dir: Path,
) -> None:
    if history_state is None:
        return
    progress_every = int(history_window.get("progress_every") or 0)
    scanned = int(history_state.get("symbols_scanned") or 0)
    if progress_every <= 0 or scanned <= 0 or scanned % progress_every != 0:
        return
    elapsed = time.monotonic() - float(history_window.get("started_at") or time.monotonic())
    print(
        "asset history progress:",
        f"asset={asset_dir.name}",
        f"symbols={scanned}/{total_symbols}",
        f"rows={int(history_state.get('rows_scanned') or 0)}",
        f"elapsed_seconds={elapsed:.1f}",
        file=sys.stderr,
    )


def _maybe_update_asset_health_history_for_symbol(
    *,
    work: pd.DataFrame,
    symbol: str,
    date_column: str,
    dataset: str | None,
    selected_fields: Sequence[str],
    history_state: dict[str, object] | None,
    history_window: Mapping[str, object],
    history_sample_limit: int,
    daily_reference: pd.DataFrame | None,
    ex_factor_reference: pd.DataFrame | None,
    shares_reference: pd.DataFrame | None,
    instrument_reference: pd.DataFrame | None,
    valuation_reference_loader: _ValuationReferenceLoader | None,
    total_symbols: int,
    asset_dir: Path,
) -> None:
    if history_state is None:
        return
    exhaustion_reason = _history_scan_exhausted(
        history_state=history_state,
        history_window=history_window,
    )
    if exhaustion_reason:
        _mark_history_scan_truncated(
            history_state=history_state,
            reason=exhaustion_reason,
        )
        return

    history_work = _filter_history_work_frame(
        work,
        date_column=date_column,
        history_window=history_window,
    )
    _update_asset_health_symbol_history_state(
        work=history_work,
        symbol=symbol,
        date_column=date_column,
        dataset=dataset,
        selected_fields=selected_fields,
        history_state=history_state,
        sample_limit=history_sample_limit,
        daily_reference=daily_reference,
        ex_factor_reference=ex_factor_reference,
        shares_reference=shares_reference,
        instrument_reference=instrument_reference,
        valuation_reference_loader=valuation_reference_loader,
    )
    _maybe_log_history_progress(
        history_state=history_state,
        history_window=history_window,
        total_symbols=total_symbols,
        asset_dir=asset_dir,
    )


def _record_asset_health_missing_target_field(
    *,
    stats: dict[str, object],
    symbol: str,
    sample_limit: int,
) -> None:
    stats["missing_on_target_date"] = int(stats["missing_on_target_date"]) + 1
    stats["missing_and_never_nonnull"] = int(stats["missing_and_never_nonnull"]) + 1
    _append_sample(stats["sample_missing_symbols"], symbol, limit=sample_limit)
    _append_sample(stats["sample_unusable_symbols"], symbol, limit=sample_limit)


def _record_asset_health_target_assessment(
    *,
    stats: dict[str, object],
    symbol: str,
    prior_series: pd.Series,
    assessment: Mapping[str, object],
    sample_limit: int,
) -> bool:
    if assessment["has_raw_nonnull"]:
        stats["nonnull_on_target_date"] = int(stats["nonnull_on_target_date"]) + 1
    else:
        stats["missing_on_target_date"] = int(stats["missing_on_target_date"]) + 1
        _append_sample(stats["sample_missing_symbols"], symbol, limit=sample_limit)
        _append_sample(stats["sample_unusable_symbols"], symbol, limit=sample_limit)
        if prior_series.notna().any():
            stats["missing_but_prior_nonnull"] = int(stats["missing_but_prior_nonnull"]) + 1
            _append_sample(stats["sample_prior_nonnull_symbols"], symbol, limit=sample_limit)
        else:
            stats["missing_and_never_nonnull"] = int(stats["missing_and_never_nonnull"]) + 1

    if assessment["has_placeholder"]:
        stats["placeholder_on_target_date"] = int(stats["placeholder_on_target_date"]) + 1
        _append_sample(stats["sample_placeholder_symbols"], symbol, limit=sample_limit)
        _append_sample(stats["sample_unusable_symbols"], symbol, limit=sample_limit)

    if assessment["has_nonfinite"]:
        stats["nonfinite_on_target_date"] = int(stats["nonfinite_on_target_date"]) + 1
        _append_sample(stats["sample_nonfinite_symbols"], symbol, limit=sample_limit)
        _append_sample(stats["sample_unusable_symbols"], symbol, limit=sample_limit)

    if not assessment["has_clean"]:
        return False

    stats["clean_nonmissing_on_target_date"] = (
        int(stats["clean_nonmissing_on_target_date"]) + 1
    )
    _append_sample(stats["sample_clean_symbols"], symbol, limit=sample_limit)
    clean_value = assessment["representative_clean_value"]
    if clean_value is not None:
        stats["clean_value_counter"][clean_value] += 1
    if assessment["has_zero"]:
        stats["zero_on_target_date"] = int(stats["zero_on_target_date"]) + 1
        _append_sample(stats["sample_zero_symbols"], symbol, limit=sample_limit)
    return True


def _record_asset_health_prior_clean_gap(
    *,
    stats: dict[str, object],
    symbol: str,
    field: str,
    work: pd.DataFrame,
    prior_frame: pd.DataFrame,
    prior_series: pd.Series,
    target_frame: pd.DataFrame,
    date_column: str,
    target_date: pd.Timestamp,
    dataset: str | None,
    sample_limit: int,
    daily_reference: pd.DataFrame | None,
    ex_factor_reference: pd.DataFrame | None,
    shares_reference: pd.DataFrame | None,
    instrument_reference: pd.DataFrame | None,
) -> None:
    prior_placeholder_mask = _placeholder_mask(prior_series)
    prior_numeric = pd.to_numeric(prior_series, errors="coerce")
    prior_nonfinite_mask = (
        prior_series.notna()
        & prior_numeric.notna()
        & ~np.isfinite(prior_numeric.to_numpy(dtype="float64"))
    )
    prior_clean_mask = prior_series.notna() & ~prior_placeholder_mask & ~prior_nonfinite_mask
    if not bool(prior_clean_mask.any()):
        return

    last_nonnull_date = (
        pd.to_datetime(prior_frame.loc[prior_clean_mask, date_column]).max().normalize()
    )
    age_days = int((target_date - last_nonnull_date).days)
    stats["unusable_but_prior_clean"] = int(stats["unusable_but_prior_clean"]) + 1
    _append_sample(stats["sample_prior_clean_symbols"], symbol, limit=sample_limit)
    ffill_record = {
        "symbol": symbol,
        "last_nonnull_date": _format_date(last_nonnull_date),
        "age_days": age_days,
    }
    if dataset != "valuation":
        stats["ffill_age_records"].append(ffill_record)
        return

    fresh_gap = _classify_valuation_fresh_target_gap(
        field=field,
        work=work,
        target_frame=target_frame,
        date_column=date_column,
        start_date=last_nonnull_date,
        end_date=target_date,
    )
    if fresh_gap["fresh_gap"]:
        ffill_record["reference_context"] = fresh_gap["reason"]
        stats["fresh_target_gap_records"].append(ffill_record)
        return

    context = _classify_valuation_reference_window(
        start_date=last_nonnull_date,
        end_date=target_date,
        daily_reference=daily_reference,
        ex_factor_reference=ex_factor_reference,
        shares_reference=shares_reference,
        instrument_reference=instrument_reference,
    )
    ffill_record["reference_context"] = context["reason"]
    if context["provider_like"]:
        stats["provider_ffill_age_records"].append(ffill_record)
    else:
        stats["ffill_age_records"].append(ffill_record)


def _target_field_stats_need_valuation_references(
    *,
    selected_fields: Sequence[str],
    work: pd.DataFrame,
    target_frame: pd.DataFrame,
    prior_frame: pd.DataFrame,
) -> bool:
    for field in selected_fields:
        if field not in work.columns:
            continue
        assessment = _assess_target_series(target_frame[field])
        if assessment["has_clean"]:
            continue
        prior_series = prior_frame[field]
        prior_placeholder_mask = _placeholder_mask(prior_series)
        prior_numeric = pd.to_numeric(prior_series, errors="coerce")
        prior_nonfinite_mask = (
            prior_series.notna()
            & prior_numeric.notna()
            & ~np.isfinite(prior_numeric.to_numpy(dtype="float64"))
        )
        prior_clean_mask = prior_series.notna() & ~prior_placeholder_mask & ~prior_nonfinite_mask
        if bool(prior_clean_mask.any()):
            return True
    return False


def _update_asset_health_target_field_stats(
    *,
    field_stats: dict[str, dict[str, object]],
    selected_fields: Sequence[str],
    symbol: str,
    work: pd.DataFrame,
    target_frame: pd.DataFrame,
    prior_frame: pd.DataFrame,
    date_column: str,
    target_date: pd.Timestamp,
    dataset: str | None,
    sample_limit: int,
    daily_reference: pd.DataFrame | None,
    ex_factor_reference: pd.DataFrame | None,
    shares_reference: pd.DataFrame | None,
    instrument_reference: pd.DataFrame | None,
) -> None:
    for field in selected_fields:
        stats = field_stats[field]
        stats["symbols_with_target_date_row"] = int(stats["symbols_with_target_date_row"]) + 1
        if field not in work.columns:
            _record_asset_health_missing_target_field(
                stats=stats,
                symbol=symbol,
                sample_limit=sample_limit,
            )
            continue

        target_series = target_frame[field]
        prior_series = prior_frame[field]
        assessment = _assess_target_series(target_series)
        if _record_asset_health_target_assessment(
            stats=stats,
            symbol=symbol,
            prior_series=prior_series,
            assessment=assessment,
            sample_limit=sample_limit,
        ):
            continue

        _record_asset_health_prior_clean_gap(
            stats=stats,
            symbol=symbol,
            field=field,
            work=work,
            prior_frame=prior_frame,
            prior_series=prior_series,
            target_frame=target_frame,
            date_column=date_column,
            target_date=target_date,
            dataset=dataset,
            sample_limit=sample_limit,
            daily_reference=daily_reference,
            ex_factor_reference=ex_factor_reference,
            shares_reference=shares_reference,
            instrument_reference=instrument_reference,
        )


def inspect_hk_asset_health(args) -> int:
    asset_dir = _resolve_path(args.asset_dir)
    data_dir = asset_dir / "data"
    if not data_dir.exists():
        raise SystemExit(f"Asset directory is missing data/: {asset_dir}")

    all_data_files = sorted(data_dir.glob("*.parquet"))
    if not all_data_files:
        raise SystemExit(f"No parquet files found under {data_dir}")

    manifest_path = asset_dir / "manifest.yml"
    manifest = _load_manifest(manifest_path) if manifest_path.exists() else None
    dataset = str(manifest.get("dataset") or "").strip() if isinstance(manifest, Mapping) else ""
    dataset = dataset or None

    sample_frame = _normalize_frame_columns(pd.read_parquet(all_data_files[0]))
    sample_columns = sample_frame.columns.tolist()
    date_column = _infer_date_column(sample_columns, getattr(args, "date_column", None))
    selected_fields, selection_source = _resolve_fields(
        requested_fields=getattr(args, "field", []) or [],
        dataset=dataset,
        manifest=manifest,
        columns=sample_columns,
    )

    audit, _ = _load_audit_frame(asset_dir)
    target_date, target_date_source = _resolve_target_date(
        explicit_value=getattr(args, "target_date", None),
        audit=audit,
        manifest=manifest,
        data_files=all_data_files,
        date_column=date_column,
    )

    symbol_filter = _resolve_symbol_filter(args, target_date=target_date)
    data_files_by_symbol = {_normalize_hk_symbol(path.stem): path for path in all_data_files}
    candidate_symbols = symbol_filter["symbols"] or sorted(data_files_by_symbol)
    if not candidate_symbols:
        raise SystemExit("No symbols resolved for asset health inspection.")

    missing_asset_symbols = [symbol for symbol in candidate_symbols if symbol not in data_files_by_symbol]
    sample_limit = max(1, int(getattr(args, "sample_limit", 5) or 5))
    history_sample_limit = max(
        1,
        int(getattr(args, "history_sample_limit", sample_limit) or sample_limit),
    )
    include_history = bool(getattr(args, "include_history", False))
    history_state = _init_history_state(dataset=dataset) if include_history else None
    history_window = (
        _resolve_history_window(args, target_date=target_date)
        if include_history
        else {
            "start": None,
            "end": None,
            "tail_days": None,
            "timeout_seconds": None,
            "max_symbols": None,
            "progress_every": 0,
            "started_at": None,
        }
    )
    (
        daily_reference_asset_dir,
        ex_factor_reference_asset_dir,
        shares_reference_asset_dir,
        instrument_reference_by_symbol,
    ) = _resolve_asset_health_reference_assets(
        args,
        dataset=dataset,
        asset_dir=asset_dir,
    )

    field_stats = _init_asset_health_field_stats(selected_fields)
    daily_rule_stats = _init_daily_rule_stats()
    duplicate_date_stats = {
        "symbols": 0,
        "duplicate_date_groups": 0,
        "duplicate_rows": 0,
        "sample_symbols": [],
    }

    stale_rows: list[dict[str, str | None]] = []
    symbols_with_target_date_row = 0
    latest_min: pd.Timestamp | None = None
    latest_max: pd.Timestamp | None = None

    audit_by_symbol: dict[str, dict[str, object]] = {}
    if audit is not None and not audit.empty:
        audit_by_symbol = audit.set_index("symbol", drop=False).to_dict(orient="index")

    latest_counts: Counter[str] = Counter()
    status_counts = _build_audit_status_counts(
        audit_by_symbol=audit_by_symbol,
        scoped_symbols=candidate_symbols,
    )
    missing_asset_file_details = _build_missing_asset_file_details(
        missing_asset_symbols=missing_asset_symbols,
        audit_by_symbol=audit_by_symbol,
        sample_limit=sample_limit,
    )
    audit_issue_groups = _build_audit_issue_groups(
        audit_by_symbol=audit_by_symbol,
        scoped_symbols=candidate_symbols,
        sample_limit=sample_limit,
    )

    for symbol in candidate_symbols:
        path = data_files_by_symbol.get(symbol)
        audit_entry = audit_by_symbol.get(symbol)
        status = ""
        if audit_entry:
            status = _clean_optional_text(audit_entry.get("status")) or ""
        daily_reference = None
        ex_factor_reference = None
        shares_reference = None
        instrument_reference = None
        valuation_reference_loader: _ValuationReferenceLoader | None = None
        if dataset == "valuation":
            valuation_reference_loader = _ValuationReferenceLoader(
                symbol=symbol,
                daily_asset_dir=daily_reference_asset_dir,
                ex_factor_asset_dir=ex_factor_reference_asset_dir,
                shares_asset_dir=shares_reference_asset_dir,
                instrument_by_symbol=instrument_reference_by_symbol,
            )

        if path is None:
            continue

        work = _read_asset_health_symbol_work_frame(
            path=path,
            date_column=date_column,
            selected_fields=selected_fields,
            dataset=dataset,
        )
        if work.empty:
            if len(stale_rows) < sample_limit:
                stale_rows.append(
                    {
                        "symbol": symbol,
                        "latest_date": None,
                        "status": status,
                    }
                )
            continue

        work = _deduplicate_asset_health_symbol_work_frame(
            work=work,
            symbol=symbol,
            dataset=dataset,
            date_column=date_column,
            duplicate_date_stats=duplicate_date_stats,
            sample_limit=sample_limit,
        )
        _maybe_update_asset_health_history_for_symbol(
            work=work,
            symbol=symbol,
            date_column=date_column,
            dataset=dataset,
            selected_fields=selected_fields,
            history_state=history_state,
            history_window=history_window,
            history_sample_limit=history_sample_limit,
            daily_reference=daily_reference,
            ex_factor_reference=ex_factor_reference,
            shares_reference=shares_reference,
            instrument_reference=instrument_reference,
            valuation_reference_loader=valuation_reference_loader,
            total_symbols=len(candidate_symbols),
            asset_dir=asset_dir,
        )

        latest_ts = work[date_column].max()
        latest_min = latest_ts if latest_min is None or latest_ts < latest_min else latest_min
        latest_max = latest_ts if latest_max is None or latest_ts > latest_max else latest_max
        latest_counts[_format_date(latest_ts) or ""] += 1

        target_mask = work[date_column] == target_date
        if not bool(target_mask.any()):
            if len(stale_rows) < sample_limit:
                stale_rows.append(
                    {
                        "symbol": symbol,
                        "latest_date": _format_date(latest_ts),
                        "status": status,
                    }
                )
            continue

        symbols_with_target_date_row += 1
        target_frame = work.loc[target_mask]
        prior_frame = work.loc[work[date_column] < target_date]

        if (
            dataset == "valuation"
            and valuation_reference_loader is not None
            and _target_field_stats_need_valuation_references(
                selected_fields=selected_fields,
                work=work,
                target_frame=target_frame,
                prior_frame=prior_frame,
            )
        ):
            references = valuation_reference_loader.load()
            daily_reference = references.daily
            ex_factor_reference = references.ex_factor
            shares_reference = references.shares
            instrument_reference = references.instrument

        if dataset == "daily":
            _record_daily_rule_stats(
                target_frame=target_frame,
                symbol=symbol,
                stats=daily_rule_stats,
                sample_limit=sample_limit,
            )

        _update_asset_health_target_field_stats(
            field_stats=field_stats,
            selected_fields=selected_fields,
            symbol=symbol,
            work=work,
            target_frame=target_frame,
            prior_frame=prior_frame,
            date_column=date_column,
            target_date=target_date,
            dataset=dataset,
            sample_limit=sample_limit,
            daily_reference=daily_reference,
            ex_factor_reference=ex_factor_reference,
            shares_reference=shares_reference,
            instrument_reference=instrument_reference,
        )

    symbols_scanned = len(candidate_symbols)
    latest_rows = [
        {"latest_date": date_text, "symbols": int(count)}
        for date_text, count in sorted(
            latest_counts.items(),
            key=lambda item: (-int(item[1]), str(item[0])),
        )[: max(1, int(getattr(args, "top_latest_dates", 5) or 5))]
        if date_text
    ]

    field_rows = _build_field_coverage_rows(
        selected_fields=selected_fields,
        field_stats=field_stats,
        sample_limit=sample_limit,
    )

    quality_checks = _build_field_quality_checks(
        field_rows=field_rows,
        dataset=dataset,
        sample_limit=sample_limit,
    )

    if dataset == "daily":
        quality_checks.extend(
            _build_daily_rule_quality_checks(
                daily_rule_stats=daily_rule_stats,
                symbols_with_target_date_row=symbols_with_target_date_row,
                sample_limit=sample_limit,
            )
        )

    if int(duplicate_date_stats["symbols"]) > 0:
        quality_checks.append(
            {
                "check": "symbol_duplicate_dates_in_asset_file",
                "field": None,
                "severity": "error",
                "affected_symbols": int(duplicate_date_stats["symbols"]),
                "affected_pct": _round_pct(int(duplicate_date_stats["symbols"]), symbols_scanned),
                "duplicate_date_groups": int(duplicate_date_stats["duplicate_date_groups"]),
                "duplicate_rows": int(duplicate_date_stats["duplicate_rows"]),
                "sample_symbols": list(duplicate_date_stats["sample_symbols"]),
            }
        )

    quality_verdict = summarize_quality_checks(
        quality_checks,
        fail_on_severity=getattr(args, "fail_on_severity", "none"),
    )

    payload = _build_asset_health_payload(
        asset_dir=asset_dir,
        dataset=dataset,
        target_date=target_date,
        target_date_source=target_date_source,
        date_column=date_column,
        selection_source=selection_source,
        selected_fields=selected_fields,
        manifest=manifest,
        manifest_path=manifest_path,
        daily_reference_asset_dir=daily_reference_asset_dir,
        symbol_filter=symbol_filter,
        symbols_scanned=symbols_scanned,
        all_data_files=all_data_files,
        missing_asset_symbols=missing_asset_symbols,
        symbols_with_target_date_row=symbols_with_target_date_row,
        duplicate_date_stats=duplicate_date_stats,
        latest_min=latest_min,
        latest_max=latest_max,
        status_counts=status_counts,
        audit_issue_groups=audit_issue_groups,
        quality_checks=quality_checks,
        quality_verdict=quality_verdict,
        include_history=include_history,
        history_state=history_state,
        latest_rows=latest_rows,
        sample_limit=sample_limit,
        missing_asset_file_details=missing_asset_file_details,
        stale_rows=stale_rows,
        field_rows=field_rows,
    )
    if history_state is not None:
        payload["history_scan_control"] = {
            "start_date": _format_date(history_window.get("start")),
            "end_date": _format_date(history_window.get("end")),
            "tail_days": history_window.get("tail_days"),
            "timeout_seconds": history_window.get("timeout_seconds"),
            "max_symbols": history_window.get("max_symbols"),
            "progress_every_symbols": history_window.get("progress_every"),
            "truncated": bool(history_state.get("truncated")),
            "truncation_reason": history_state.get("truncation_reason"),
            "symbols_skipped": int(history_state.get("symbols_skipped") or 0),
        }
        payload["summary"]["history_scan_truncated"] = bool(history_state.get("truncated"))
        payload["summary"]["history_scan_truncation_reason"] = history_state.get("truncation_reason")
        payload["summary"]["history_symbols_skipped"] = int(history_state.get("symbols_skipped") or 0)
    return _write_asset_health_output(args, payload)
