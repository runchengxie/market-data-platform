from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from market_data_platform.config_utils import get_research_universe_config, resolve_pipeline_config
from market_data_platform.pit_feature_stats import (
    compute_calendar_cagr,
    compute_trailing_calendar_window_stat,
)
from market_data_platform.rebalance import get_rebalance_dates
from market_data_platform.symbols import drop_legacy_symbol_columns, ensure_symbol_columns
from .build import (
    _default_pipeline_fundamentals_path,
    _load_universe_by_date_frame,
    _pipeline_fundamentals_manifest_path,
    _resolve_build_fields,
)
from .coverage_rendering import render_hk_pit_coverage_text as _render_hk_pit_coverage_text
from .health_shared import (
    format_date as _format_date,
    load_symbols_from_text as _load_symbols_from_text,
    normalize_symbol_list as _normalize_symbol_list,
    parse_compact_date as _parse_compact_date,
)
from .quality_gate import (
    normalize_fail_on_severity,
    quality_gate_exit_code,
    summarize_quality_checks,
)
from .shared import (
    DEFAULT_PIPELINE_FUNDAMENTALS_NAME,
    DERIVED_PIT_FEATURES,
    _coerce_bool,
    _load_manifest,
    _normalize_field_list,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _resolve_fields_with_overrides,
    _resolve_path,
    _resolve_universe_by_date_columns,
)

PIT_HEALTH_STALE_INFO_DAYS = 180
PIT_HEALTH_STALE_WARNING_DAYS = 365


def _resolve_fields(args) -> tuple[list[str], dict]:
    package = sys.modules.get("market_data_platform.hk_assets")
    override = getattr(package, "_load_hk_financial_fields", None) if package is not None else None
    return _resolve_fields_with_overrides(
        args,
        load_hk_financial_fields_override=override,
    )

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

def _load_health_universe_by_date_frame(path_text: str | Path) -> tuple[Path, pd.DataFrame]:
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
    df["symbol"] = df["symbol"].map(_normalize_hk_symbol)
    df = df[df["trade_date"].notna()].copy()
    df = df[df["symbol"] != ""].copy()
    return path, df.loc[:, ["trade_date", "symbol"]].drop_duplicates().reset_index(drop=True)


def _resolve_health_by_date_path(
    *,
    args,
    config_data: Mapping[str, object] | None,
) -> tuple[Path | None, str | None]:
    explicit = getattr(args, "by_date_file", None)
    if explicit:
        return _resolve_path(explicit), "explicit_by_date_file"
    if isinstance(config_data, Mapping):
        universe_cfg = get_research_universe_config(config_data)
        if isinstance(universe_cfg, Mapping):
            path_text = universe_cfg.get("by_date_file")
            if path_text:
                return _resolve_path(str(path_text)), "config_universe_by_date_file"
    return None, None


def _resolve_health_target_date(
    *,
    args,
    by_date_frame: pd.DataFrame | None,
    by_date_source: str | None,
    frame: pd.DataFrame,
) -> tuple[pd.Timestamp, str]:
    explicit = getattr(args, "target_date", None)
    if explicit:
        return _parse_compact_date(explicit, label="--target-date"), "explicit"
    if by_date_frame is not None and not by_date_frame.empty:
        source = "by_date_file_max_trade_date"
        if by_date_source == "config_universe_by_date_file":
            source = "config_universe_by_date_max_trade_date"
        return pd.Timestamp(by_date_frame["trade_date"].max()).normalize(), source
    return pd.Timestamp(frame["trade_date"].max()).normalize(), "fundamentals_max_trade_date"


def _resolve_health_symbol_filter(
    *,
    args,
    target_date: pd.Timestamp,
    by_date_frame: pd.DataFrame | None,
    by_date_source: str | None,
) -> dict[str, object]:
    symbols: list[str] = []
    sources: list[str] = []

    symbols_file = getattr(args, "symbols_file", None)
    if symbols_file:
        sources.append("symbols_file")
        symbols.extend(_load_symbols_from_text(symbols_file))

    if by_date_frame is not None:
        if by_date_source == "config_universe_by_date_file":
            sources.append("config_universe_by_date_target_date")
        else:
            sources.append("by_date_file_target_date")
        matched = by_date_frame.loc[by_date_frame["trade_date"] == target_date, "symbol"].drop_duplicates().tolist()
        if not matched:
            raise SystemExit(
                "No symbols matched PIT health universe on target date "
                f"{_format_date(target_date)}."
            )
        symbols.extend(matched)

    normalized = _normalize_symbol_list(symbols)
    return {
        "source": "+".join(sources) if sources else "all_fundamentals_symbols",
        "symbols": normalized,
        "symbols_file": str(_resolve_path(symbols_file)) if symbols_file else None,
    }


def _make_sample_symbols(values: Sequence[object], *, limit: int) -> list[str]:
    samples: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in samples:
            continue
        samples.append(text)
        if len(samples) >= limit:
            break
    return samples


def _make_oldest_samples(
    latest_dates: pd.Series,
    *,
    target_date: pd.Timestamp,
    sample_limit: int,
) -> list[dict[str, object]]:
    if latest_dates.empty:
        return []
    latest_dates = latest_dates.sort_values()
    samples: list[dict[str, object]] = []
    for symbol, last_date in latest_dates.head(sample_limit).items():
        samples.append(
            {
                "symbol": str(symbol),
                "last_observed_date": _format_date(last_date),
                "age_days": int((target_date - pd.Timestamp(last_date)).days),
            }
        )
    return samples


def _build_pit_health_section(
    *,
    args,
    config_data: Mapping[str, object] | None,
    asset_manifest: Mapping[str, object] | None,
    frame: pd.DataFrame,
    feature_frame: pd.DataFrame,
    selected_features: Sequence[str],
    min_symbols: int,
) -> dict[str, object]:
    by_date_path, by_date_source = _resolve_health_by_date_path(args=args, config_data=config_data)
    by_date_frame = None
    if by_date_path is not None:
        _, by_date_frame = _load_health_universe_by_date_frame(by_date_path)

    target_date, target_date_source = _resolve_health_target_date(
        args=args,
        by_date_frame=by_date_frame,
        by_date_source=by_date_source,
        frame=frame,
    )
    symbol_filter = _resolve_health_symbol_filter(
        args=args,
        target_date=target_date,
        by_date_frame=by_date_frame,
        by_date_source=by_date_source,
    )
    sample_limit = int(getattr(args, "health_sample_limit", 5) or 5)

    all_fundamentals_symbols = sorted(frame["symbol"].dropna().astype(str).unique().tolist())
    requested_symbols = list(symbol_filter["symbols"]) or all_fundamentals_symbols
    requested_set = set(requested_symbols)
    available_set = set(all_fundamentals_symbols)
    available_symbols = [symbol for symbol in requested_symbols if symbol in available_set]
    missing_asset_symbols = [symbol for symbol in requested_symbols if symbol not in available_set]
    manifest_missing_remote_symbols = set(
        _normalize_symbol_list(
            asset_manifest.get("missing_symbols")
            if isinstance(asset_manifest, Mapping)
            else []
        )
    )

    scope_mask = frame["symbol"].isin(requested_set) & (frame["trade_date"] <= target_date)
    scoped_frame = frame.loc[scope_mask].copy()
    latest_rows = (
        scoped_frame.sort_values(["symbol", "trade_date"])
        .groupby("symbol", group_keys=False)
        .tail(1)
        .set_index("symbol")
    )
    latest_row_dates = latest_rows["trade_date"] if "trade_date" in latest_rows.columns else pd.Series(dtype="datetime64[ns]")
    latest_row_ages = (
        (target_date - latest_row_dates).dt.days.astype(int)
        if not latest_row_dates.empty
        else pd.Series(dtype="int64")
    )

    feature_date_map: dict[str, pd.Series] = {}
    feature_health_rows: list[dict[str, object]] = []
    quality_checks: list[dict[str, object]] = []
    total_symbols = len(requested_symbols)

    for feature in selected_features:
        clean_mask = scope_mask & feature_frame[feature].notna()
        available_rows = frame.loc[clean_mask, ["symbol", "trade_date"]].copy()
        latest_feature_dates = (
            available_rows.sort_values(["symbol", "trade_date"])
            .groupby("symbol", group_keys=False)
            .tail(1)
            .set_index("symbol")["trade_date"]
            if not available_rows.empty
            else pd.Series(dtype="datetime64[ns]")
        )
        latest_feature_dates = latest_feature_dates.reindex(available_symbols).dropna()
        feature_date_map[feature] = latest_feature_dates

        age_days = (
            (target_date - latest_feature_dates).dt.days.astype(int)
            if not latest_feature_dates.empty
            else pd.Series(dtype="int64")
        )
        stale_warning_symbols = age_days.loc[
            age_days > PIT_HEALTH_STALE_WARNING_DAYS
        ].sort_values(ascending=False).index.tolist()
        stale_info_symbols = age_days.loc[
            (age_days > PIT_HEALTH_STALE_INFO_DAYS)
            & (age_days <= PIT_HEALTH_STALE_WARNING_DAYS)
        ].sort_values(ascending=False).index.tolist()
        available_count = int(latest_feature_dates.index.nunique())
        missing_symbols = [symbol for symbol in requested_symbols if symbol not in set(latest_feature_dates.index)]
        row = {
            "feature": feature,
            "symbols_with_clean_value_asof_target_date": available_count,
            "coverage_pct": _round_pct(available_count, total_symbols),
            "missing_symbols_asof_target_date": int(total_symbols - available_count),
            "age_days_min": int(age_days.min()) if not age_days.empty else None,
            "age_days_p50": _quantile_or_none(age_days.tolist(), 0.5),
            "age_days_p90": _quantile_or_none(age_days.tolist(), 0.9),
            "age_days_max": int(age_days.max()) if not age_days.empty else None,
            "age_gt_45d_symbols": int((age_days > 45).sum()) if not age_days.empty else 0,
            "age_gt_90d_symbols": int((age_days > 90).sum()) if not age_days.empty else 0,
            "age_gt_180d_symbols": int((age_days > 180).sum()) if not age_days.empty else 0,
            "sample_oldest_symbols": _make_oldest_samples(
                latest_feature_dates,
                target_date=target_date,
                sample_limit=sample_limit,
            ),
            "sample_missing_symbols": _make_sample_symbols(missing_symbols, limit=sample_limit),
        }
        feature_health_rows.append(row)

        if available_count == 0 and total_symbols > 0:
            quality_checks.append(
                {
                    "check": "feature_unavailable_asof_target_date",
                    "field": feature,
                    "severity": "error",
                    "affected_symbols": total_symbols,
                    "affected_pct": 100.0,
                    "sample_symbols": _make_sample_symbols(requested_symbols, limit=sample_limit),
                }
            )
        elif stale_warning_symbols:
            quality_checks.append(
                {
                    "check": f"feature_stale_gt_{PIT_HEALTH_STALE_WARNING_DAYS}d_asof_target_date",
                    "field": feature,
                    "severity": "warning",
                    "affected_symbols": len(stale_warning_symbols),
                    "affected_pct": _round_pct(len(stale_warning_symbols), available_count),
                    "sample_symbols": _make_sample_symbols(stale_warning_symbols, limit=sample_limit),
                }
            )
        if stale_info_symbols:
            quality_checks.append(
                {
                    "check": f"feature_stale_gt_{PIT_HEALTH_STALE_INFO_DAYS}d_asof_target_date",
                    "field": feature,
                    "severity": "info",
                    "affected_symbols": len(stale_info_symbols),
                    "affected_pct": _round_pct(len(stale_info_symbols), available_count),
                    "sample_symbols": _make_sample_symbols(stale_info_symbols, limit=sample_limit),
                }
            )

    feature_health_rows.sort(
        key=lambda item: (
            float(item["coverage_pct"]),
            -(int(item["age_days_max"]) if item["age_days_max"] is not None else -1),
            str(item["feature"]),
        )
    )

    feature_date_frame = (
        pd.concat(
            {
                feature: latest_dates.reindex(requested_symbols)
                for feature, latest_dates in feature_date_map.items()
            },
            axis=1,
        )
        if feature_date_map
        else pd.DataFrame(index=requested_symbols)
    )
    any_feature_mask = feature_date_frame.notna().any(axis=1) if not feature_date_frame.empty else pd.Series(False, index=requested_symbols)
    all_feature_mask = feature_date_frame.notna().all(axis=1) if not feature_date_frame.empty else pd.Series(False, index=requested_symbols)
    complete_oldest_dates = (
        feature_date_frame.loc[all_feature_mask].min(axis=1)
        if not feature_date_frame.empty and bool(all_feature_mask.any())
        else pd.Series(dtype="datetime64[ns]")
    )
    complete_oldest_ages = (
        (target_date - complete_oldest_dates).dt.days.astype(int)
        if not complete_oldest_dates.empty
        else pd.Series(dtype="int64")
    )

    recent_disclosures = scoped_frame.loc[:, ["trade_date", "symbol"]].copy()
    recent_windows: dict[str, int] = {}
    for days in (30, 90, 180):
        window_start = target_date - pd.Timedelta(days=days)
        window_frame = recent_disclosures.loc[recent_disclosures["trade_date"] >= window_start]
        recent_windows[f"rows_last_{days}d"] = int(len(window_frame))
        recent_windows[f"symbols_updated_last_{days}d"] = int(window_frame["symbol"].nunique()) if not window_frame.empty else 0
        recent_windows[f"disclosure_dates_last_{days}d"] = int(window_frame["trade_date"].nunique()) if not window_frame.empty else 0

    recent_disclosure_rows = (
        recent_disclosures.groupby("trade_date")["symbol"]
        .agg(["count", "nunique"])
        .reset_index()
        .rename(columns={"count": "rows", "nunique": "symbols"})
        .sort_values("trade_date")
    )
    recent_disclosure_rows = recent_disclosure_rows.tail(10)
    recent_disclosure_payload = [
        {
            "trade_date": _format_date(row.trade_date),
            "rows": int(row.rows),
            "symbols": int(row.symbols),
        }
        for row in recent_disclosure_rows.itertuples(index=False)
    ]

    symbols_without_rows = [symbol for symbol in requested_symbols if symbol not in set(latest_row_dates.index)]
    provider_missing_symbols = [
        symbol for symbol in symbols_without_rows if symbol in manifest_missing_remote_symbols
    ]
    local_missing_symbols = [
        symbol for symbol in symbols_without_rows if symbol not in manifest_missing_remote_symbols
    ]
    if provider_missing_symbols:
        quality_checks.append(
            {
                "check": "symbol_without_remote_pit_row_before_target_date",
                "field": None,
                "severity": "warning",
                "classification": "provider-boundary",
                "affected_symbols": len(provider_missing_symbols),
                "affected_pct": _round_pct(len(provider_missing_symbols), total_symbols),
                "sample_symbols": _make_sample_symbols(provider_missing_symbols, limit=sample_limit),
            }
        )
    if local_missing_symbols:
        quality_checks.append(
            {
                "check": "symbol_without_any_pit_row_before_target_date",
                "field": None,
                "severity": "error",
                "classification": "local-gap",
                "affected_symbols": len(local_missing_symbols),
                "affected_pct": _round_pct(len(local_missing_symbols), total_symbols),
                "sample_symbols": _make_sample_symbols(local_missing_symbols, limit=sample_limit),
            }
        )

    complete_count = int(all_feature_mask.sum()) if len(all_feature_mask) else 0
    if complete_count == 0 and total_symbols > 0:
        quality_checks.append(
            {
                "check": "selected_feature_set_unavailable_asof_target_date",
                "field": None,
                "severity": "error",
                "affected_symbols": total_symbols,
                "affected_pct": 100.0,
                "sample_symbols": _make_sample_symbols(requested_symbols, limit=sample_limit),
            }
        )
    elif complete_count < min_symbols:
        quality_checks.append(
            {
                "check": "selected_feature_set_below_min_symbols_asof_target_date",
                "field": None,
                "severity": "warning",
                "affected_symbols": total_symbols - complete_count,
                "affected_pct": _round_pct(total_symbols - complete_count, total_symbols),
                "sample_symbols": _make_sample_symbols(
                    all_feature_mask.loc[~all_feature_mask].index.tolist(),
                    limit=sample_limit,
                ),
            }
        )

    quality_verdict = summarize_quality_checks(
        quality_checks,
        fail_on_severity=getattr(args, "fail_on_severity", "none"),
    )

    return {
        "source": {
            "target_date": _format_date(target_date),
            "target_date_source": target_date_source,
            "symbol_filter_source": symbol_filter["source"],
            "symbols_file": symbol_filter.get("symbols_file"),
            "by_date_file": str(by_date_path) if by_date_path is not None else None,
        },
        "summary": {
            "symbols_scanned": total_symbols,
            "symbols_available_in_fundamentals": len(available_symbols),
            "symbols_missing_in_fundamentals": len(missing_asset_symbols),
            "symbols_missing_remote_in_manifest": len(provider_missing_symbols),
            "symbols_missing_local_without_manifest_evidence": len(local_missing_symbols),
            "symbols_with_any_row_before_target_date": int(latest_row_dates.index.nunique()),
            "symbols_without_any_row_before_target_date": len(symbols_without_rows),
            "symbols_with_any_selected_features_asof_target_date": int(any_feature_mask.sum()) if len(any_feature_mask) else 0,
            "symbols_with_all_selected_features_asof_target_date": complete_count,
            "all_selected_features_coverage_pct": _round_pct(complete_count, total_symbols),
            "latest_report_age_days_min": int(latest_row_ages.min()) if not latest_row_ages.empty else None,
            "latest_report_age_days_p50": _quantile_or_none(latest_row_ages.tolist(), 0.5),
            "latest_report_age_days_p90": _quantile_or_none(latest_row_ages.tolist(), 0.9),
            "latest_report_age_days_max": int(latest_row_ages.max()) if not latest_row_ages.empty else None,
            "latest_report_age_gt_45d_symbols": int((latest_row_ages > 45).sum()) if not latest_row_ages.empty else 0,
            "latest_report_age_gt_90d_symbols": int((latest_row_ages > 90).sum()) if not latest_row_ages.empty else 0,
            "latest_report_age_gt_180d_symbols": int((latest_row_ages > 180).sum()) if not latest_row_ages.empty else 0,
            "complete_symbol_oldest_feature_age_days_max": int(complete_oldest_ages.max()) if not complete_oldest_ages.empty else None,
            "complete_symbol_oldest_feature_age_gt_90d_symbols": int((complete_oldest_ages > 90).sum()) if not complete_oldest_ages.empty else 0,
            "complete_symbol_oldest_feature_age_gt_180d_symbols": int((complete_oldest_ages > 180).sum()) if not complete_oldest_ages.empty else 0,
            **recent_windows,
        },
        "sample_symbols_without_rows": _make_sample_symbols(symbols_without_rows, limit=sample_limit),
        "sample_missing_asset_symbols": _make_sample_symbols(missing_asset_symbols, limit=sample_limit),
        "recent_disclosures": recent_disclosure_payload,
        "feature_health": feature_health_rows,
        "quality_verdict": quality_verdict,
        "quality_checks": quality_checks,
    }


def _is_supported_pit_coverage_feature(feature: str, available_columns: set[str]) -> bool:
    if feature in available_columns:
        return True
    if feature == "days_since_report":
        return True
    if feature.startswith("delta_") or feature.startswith("growth_"):
        return _is_supported_pit_coverage_feature(feature.split("_", 1)[1], available_columns)
    return feature in DERIVED_PIT_FEATURES


def _compute_pit_coverage_series(
    frame: pd.DataFrame,
    feature: str,
    *,
    cache: dict[str, pd.Series],
) -> pd.Series:
    cached = cache.get(feature)
    if cached is not None:
        return cached

    index = frame.index

    def _nan_series() -> pd.Series:
        return pd.Series(np.nan, index=index, dtype=float)

    def _numeric(name: str) -> pd.Series:
        if name not in frame.columns:
            return _nan_series()
        return pd.to_numeric(frame[name], errors="coerce")

    def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
        valid_denominator = denominator.where(denominator.notna() & (denominator != 0))
        ratio = numerator / valid_denominator
        return ratio.replace([np.inf, -np.inf], np.nan)

    def _get(name: str) -> pd.Series:
        return _compute_pit_coverage_series(frame, name, cache=cache)

    if feature in frame.columns:
        series = _numeric(feature)
    elif feature == "sales":
        series = _get("revenue").combine_first(_get("operating_revenue"))
    elif feature == "debt":
        short_term_debt = _get("short_term_debt")
        long_term_loans = _get("long_term_loans")
        debt = short_term_debt.fillna(0.0) + long_term_loans.fillna(0.0)
        series = debt.where(~(short_term_debt.isna() & long_term_loans.isna()))
    elif feature == "profit_margin":
        series = _safe_ratio(_get("net_profit"), _get("sales"))
    elif feature == "operating_margin":
        series = _safe_ratio(_get("operating_profit"), _get("sales"))
    elif feature == "cfo_margin":
        series = _safe_ratio(_get("cash_flow_from_operating_activities"), _get("sales"))
    elif feature == "cfo_to_profit":
        series = _safe_ratio(_get("cash_flow_from_operating_activities"), _get("net_profit"))
    elif feature == "asset_turnover":
        series = _safe_ratio(_get("revenue"), _get("total_assets"))
    elif feature == "roa":
        series = _safe_ratio(_get("net_profit"), _get("total_assets"))
    elif feature == "leverage":
        series = _safe_ratio(_get("total_liabilities"), _get("total_assets"))
    elif feature == "cfo_to_assets":
        series = _safe_ratio(_get("cash_flow_from_operating_activities"), _get("total_assets"))
    elif feature == "debt_to_assets":
        series = _safe_ratio(_get("debt"), _get("total_assets"))
    elif feature == "debt_to_equity":
        series = _safe_ratio(_get("debt"), _get("total_equity"))
    elif feature == "cash_to_assets":
        series = _safe_ratio(_get("cash_and_equivalents"), _get("total_assets"))
    elif feature == "goodwill_to_assets":
        series = _safe_ratio(_get("goodwill"), _get("total_assets"))
    elif feature == "accrual_ratio":
        numerator = _get("net_profit") - _get("cash_flow_from_operating_activities")
        series = _safe_ratio(numerator, _get("total_assets"))
    elif feature == "receivables_to_revenue":
        series = _safe_ratio(_get("accounts_receivable"), _get("revenue"))
    elif feature == "inventory_to_revenue":
        series = _safe_ratio(_get("inventory"), _get("revenue"))
    elif feature == "working_capital_to_assets":
        working_capital = _get("accounts_receivable") + _get("inventory") - _get("accounts_payable")
        series = _safe_ratio(working_capital, _get("total_assets"))
    elif feature == "net_debt_to_assets":
        net_debt = _get("debt") - _get("cash_and_equivalents")
        series = _safe_ratio(net_debt, _get("total_assets"))
    elif feature == "days_since_report":
        series = pd.Series(0.0, index=index, dtype=float)
    elif feature == "sales_cagr_3y":
        series = compute_calendar_cagr(frame, _get("sales"), years=3)
    elif feature == "eps_cagr_3y":
        series = compute_calendar_cagr(frame, _get("basic_earnings_per_share"), years=3)
    elif feature == "cfo_margin_avg_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cfo_margin"),
            years=3,
            stat="mean",
            min_periods=3,
        )
    elif feature == "profit_margin_std_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("profit_margin"),
            years=3,
            stat="std",
            min_periods=3,
        )
    elif feature == "cfo_to_profit_median_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cfo_to_profit"),
            years=3,
            stat="median",
            min_periods=3,
        )
    elif feature == "positive_cfo_ratio_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cash_flow_from_operating_activities"),
            years=3,
            stat="positive_ratio",
            min_periods=3,
        )
    elif feature == "positive_cfo_ratio_2y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cash_flow_from_operating_activities"),
            years=2,
            stat="positive_ratio",
            min_periods=2,
        )
    elif feature == "positive_cfo_ratio_3y_min2":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cash_flow_from_operating_activities"),
            years=3,
            stat="positive_ratio",
            min_periods=2,
        )
    elif feature.startswith("delta_"):
        base_feature = feature.removeprefix("delta_")
        base_series = _get(base_feature)
        series = base_series.groupby(frame["symbol"]).diff()
    elif feature.startswith("growth_"):
        base_feature = feature.removeprefix("growth_")
        current = _get(base_feature)
        previous = current.groupby(frame["symbol"]).shift()
        scale = ((current.abs() + previous.abs()) / 2.0).where(
            lambda values: values.notna() & (values != 0)
        )
        growth = (current - previous) / scale
        series = growth.replace([np.inf, -np.inf], np.nan)
    else:
        series = _nan_series()

    cache[feature] = series
    return series


def _resolve_pit_coverage_features(
    *,
    args,
    config_data: Mapping[str, object] | None,
    manifest: Mapping[str, object] | None,
    available_columns: Sequence[str],
) -> tuple[list[str], dict[str, object]]:
    if getattr(args, "field_profile", None) or getattr(args, "field", None) or getattr(args, "fields_file", None):
        raw_features, metadata = _resolve_fields(args)
        features = _normalize_field_list(raw_features)
        source = "explicit"
        requested_config_features: list[str] = []
    else:
        requested_config_features = []
        if config_data:
            fundamentals_cfg = config_data.get("fundamentals")
            if isinstance(fundamentals_cfg, Mapping):
                raw_config_features = fundamentals_cfg.get("features")
                if isinstance(raw_config_features, Sequence) and not isinstance(raw_config_features, str):
                    requested_config_features = _normalize_field_list(raw_config_features)
        if requested_config_features:
            features = requested_config_features
            metadata = {
                "count": len(features),
                "field_profile": [],
                "fields_file": [],
                "source": "config.fundamentals.features",
            }
            source = "config.fundamentals.features"
        else:
            features, metadata = _resolve_build_fields(
                args=args,
                manifest=manifest,
                available_columns=available_columns,
            )
            source = str(metadata.get("source") or "asset_manifest")

    available_set = set(_normalize_field_list(available_columns))
    supported_features = [
        feature for feature in features if _is_supported_pit_coverage_feature(feature, available_set)
    ]
    ignored_features = [feature for feature in features if feature not in supported_features]
    if not supported_features:
        raise SystemExit(
            "No PIT coverage features could be resolved. "
            "Check --field/--config or confirm the fundamentals file columns."
        )
    metadata = dict(metadata)
    metadata.update(
        {
            "source": source,
            "requested_features": features,
            "supported_features": supported_features,
            "ignored_features": ignored_features,
        }
    )
    return supported_features, metadata


def _resolve_trainable_pit_features(
    *,
    args,
    config_data: Mapping[str, object] | None,
    available_columns: Sequence[str],
    fallback_features: Sequence[str],
    fallback_metadata: Mapping[str, object],
) -> tuple[list[str], dict[str, object]]:
    explicit_requested = bool(
        getattr(args, "field_profile", None)
        or getattr(args, "field", None)
        or getattr(args, "fields_file", None)
    )
    available_set = set(_normalize_field_list(available_columns))
    if explicit_requested or not isinstance(config_data, Mapping):
        requested_features = list(fallback_metadata.get("requested_features") or fallback_features)
        supported_features = [
            feature
            for feature in fallback_features
            if _is_supported_pit_coverage_feature(feature, available_set)
        ]
        ignored_features = [feature for feature in requested_features if feature not in supported_features]
        return supported_features, {
            "source": str(fallback_metadata.get("source") or "explicit"),
            "requested_features": requested_features,
            "supported_features": supported_features,
            "ignored_features": ignored_features,
            "non_pit_ignored_features": ignored_features,
        }

    features_cfg = config_data.get("features")
    features_cfg = features_cfg if isinstance(features_cfg, Mapping) else {}
    fundamentals_cfg = config_data.get("fundamentals")
    fundamentals_cfg = fundamentals_cfg if isinstance(fundamentals_cfg, Mapping) else {}

    model_features = _normalize_field_list(features_cfg.get("list") or [])
    source = "config.features.list"
    if bool(fundamentals_cfg.get("enabled", False)) and bool(
        fundamentals_cfg.get("auto_add_features", True)
    ):
        fundamentals_features = _normalize_field_list(fundamentals_cfg.get("features") or [])
        if fundamentals_features:
            model_features = list(dict.fromkeys(model_features + fundamentals_features))
            source = "config.features.list+fundamentals.auto_add_features"

    if not model_features:
        requested_features = list(fallback_metadata.get("requested_features") or fallback_features)
        supported_features = [
            feature
            for feature in fallback_features
            if _is_supported_pit_coverage_feature(feature, available_set)
        ]
        ignored_features = [feature for feature in requested_features if feature not in supported_features]
        if not supported_features:
            raise SystemExit(
                "No PIT-backed model features resolved for trainable estimate. "
                "Pass --field/--config with PIT features or use --mode strict."
            )
        return supported_features, {
            "source": str(fallback_metadata.get("source") or "config.fallback"),
            "requested_features": requested_features,
            "supported_features": supported_features,
            "ignored_features": ignored_features,
            "non_pit_ignored_features": ignored_features,
        }

    supported_features = [
        feature for feature in model_features if _is_supported_pit_coverage_feature(feature, available_set)
    ]
    ignored_features = [feature for feature in model_features if feature not in supported_features]
    if not supported_features:
        raise SystemExit(
            "No PIT-backed model features resolved for trainable estimate. "
            "The config feature list only contains non-PIT features."
        )
    return supported_features, {
        "source": source,
        "requested_features": model_features,
        "supported_features": supported_features,
        "ignored_features": ignored_features,
        "non_pit_ignored_features": ignored_features,
    }


def _resolve_trainable_pit_settings(
    config_data: Mapping[str, object] | None,
    *,
    selected_features: Sequence[str],
) -> dict[str, object]:
    features_cfg = config_data.get("features") if isinstance(config_data, Mapping) else None
    features_cfg = features_cfg if isinstance(features_cfg, Mapping) else {}
    fundamentals_cfg = config_data.get("fundamentals") if isinstance(config_data, Mapping) else None
    fundamentals_cfg = fundamentals_cfg if isinstance(fundamentals_cfg, Mapping) else {}
    eval_cfg = config_data.get("eval") if isinstance(config_data, Mapping) else None
    eval_cfg = eval_cfg if isinstance(eval_cfg, Mapping) else {}
    label_cfg = config_data.get("label") if isinstance(config_data, Mapping) else None
    label_cfg = label_cfg if isinstance(label_cfg, Mapping) else {}

    missing_cfg = features_cfg.get("missing")
    missing_cfg = missing_cfg if isinstance(missing_cfg, Mapping) else {}
    missing_method = str(missing_cfg.get("method", "none")).strip().lower()
    if missing_method not in {"none", "zero", "cross_sectional_median"}:
        raise SystemExit(
            "features.missing.method must be one of: none, zero, cross_sectional_median."
        )
    missing_features = _normalize_field_list(missing_cfg.get("features") or [])
    if missing_features:
        missing_features = [feature for feature in missing_features if feature in selected_features]
    else:
        missing_features = list(selected_features)

    rebalance_frequency = str(
        eval_cfg.get("rebalance_frequency")
        or label_cfg.get("rebalance_frequency")
        or "Q"
    ).strip().upper()
    if not rebalance_frequency:
        rebalance_frequency = "Q"

    ffill_limit = fundamentals_cfg.get("ffill_limit")
    if ffill_limit in {"", "null"}:
        ffill_limit = None
    if ffill_limit is not None:
        try:
            ffill_limit = int(ffill_limit)
        except (TypeError, ValueError) as exc:
            raise SystemExit("fundamentals.ffill_limit must be an integer or null.") from exc

    return {
        "missing_method": missing_method,
        "missing_features": missing_features,
        "add_indicators": bool(missing_cfg.get("add_indicators", False)),
        "indicator_suffix": str(missing_cfg.get("indicator_suffix", "_missing")).strip() or "_missing",
        "rebalance_frequency": rebalance_frequency,
        "sample_on_rebalance_dates": bool(eval_cfg.get("sample_on_rebalance_dates", False)),
        "fundamentals_ffill": bool(fundamentals_cfg.get("ffill", True)),
        "fundamentals_ffill_limit": ffill_limit,
    }


def _build_trainable_period_grid(
    *,
    frame: pd.DataFrame,
    rebalance_frequency: str,
    sample_on_rebalance_dates: bool,
    universe_by_date: pd.DataFrame | None,
) -> tuple[pd.DataFrame, str]:
    if universe_by_date is not None and not universe_by_date.empty:
        universe = universe_by_date.copy()
        if sample_on_rebalance_dates:
            rebalance_dates = pd.to_datetime(
                get_rebalance_dates(sorted(universe["trade_date"].unique()), rebalance_frequency)
            )
            if len(rebalance_dates) > 0:
                universe = universe[universe["trade_date"].isin(set(rebalance_dates))].copy()
        universe["__period"] = universe["trade_date"].dt.to_period(rebalance_frequency)
        universe = universe.sort_values(["symbol", "trade_date"])
        grid = (
            universe.groupby(["symbol", "__period"], group_keys=False)
            .tail(1)[["trade_date", "symbol", "__period"]]
            .reset_index(drop=True)
        )
        return grid, "universe_by_date"

    disclosure_periods = frame[["trade_date", "symbol"]].copy()
    disclosure_periods["__period"] = disclosure_periods["trade_date"].dt.to_period(rebalance_frequency)
    disclosure_periods = disclosure_periods.sort_values(["symbol", "trade_date"])
    disclosure_periods = disclosure_periods.groupby(["symbol", "__period"], group_keys=False).tail(1)

    parts: list[pd.DataFrame] = []
    for symbol, symbol_periods in disclosure_periods.groupby("symbol"):
        start_period = symbol_periods["__period"].min()
        end_period = symbol_periods["__period"].max()
        if pd.isna(start_period) or pd.isna(end_period):
            continue
        period_range = pd.period_range(start=start_period, end=end_period, freq=rebalance_frequency)
        symbol_grid = pd.DataFrame({"__period": period_range})
        symbol_grid["symbol"] = symbol
        symbol_grid["trade_date"] = symbol_grid["__period"].dt.to_timestamp(how="end").dt.normalize()
        parts.append(symbol_grid[["trade_date", "symbol", "__period"]])
    if not parts:
        return disclosure_periods.iloc[0:0][["trade_date", "symbol", "__period"]].copy(), "disclosure_period_range"
    grid = pd.concat(parts, ignore_index=True)
    grid = grid.sort_values(["symbol", "__period"]).reset_index(drop=True)
    return grid, "disclosure_period_range"


def _estimate_trainable_pit_coverage(
    *,
    frame: pd.DataFrame,
    feature_frame: pd.DataFrame,
    selected_features: Sequence[str],
    config_data: Mapping[str, object] | None,
    min_symbols: int,
    feature_source: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    settings = _resolve_trainable_pit_settings(config_data, selected_features=selected_features)
    rebalance_frequency = str(settings["rebalance_frequency"])

    universe_cfg = get_research_universe_config(config_data)
    universe_cfg = universe_cfg if isinstance(universe_cfg, Mapping) else {}
    universe_by_date = None
    universe_by_date_file = universe_cfg.get("by_date_file")
    if universe_by_date_file:
        candidate = _resolve_path(str(universe_by_date_file))
        if candidate.exists():
            universe_by_date = _load_universe_by_date_frame(candidate)

    period_grid, grid_source = _build_trainable_period_grid(
        frame=frame,
        rebalance_frequency=rebalance_frequency,
        sample_on_rebalance_dates=bool(settings["sample_on_rebalance_dates"]),
        universe_by_date=universe_by_date,
    )
    if period_grid.empty:
        return (
            {
                "feature_source": feature_source,
                "pit_features_considered": len(selected_features),
                "non_pit_features_ignored": [],
                "rebalance_frequency": rebalance_frequency,
                "sample_on_rebalance_dates": bool(settings["sample_on_rebalance_dates"]),
                "grid_source": grid_source,
                "fundamentals_ffill": bool(settings["fundamentals_ffill"]),
                "fundamentals_ffill_limit": settings["fundamentals_ffill_limit"],
                "missing_method": str(settings["missing_method"]),
                "missing_features_considered": len(settings["missing_features"]),
                "indicator_features_added": len(settings["missing_features"])
                if bool(settings["add_indicators"])
                else 0,
                "active_rows": 0,
                "active_symbols": 0,
                "periods": 0,
                "rows_with_all_selected_features_after_ffill": 0,
                "rows_with_all_selected_features_after_missing_fill": 0,
                "period_symbols_median_after_ffill": 0,
                "period_symbols_max_after_ffill": 0,
                "period_count_meeting_min_symbols_after_ffill": 0,
                "period_symbols_median_after_missing_fill": 0,
                "period_symbols_max_after_missing_fill": 0,
                "period_count_meeting_min_symbols_after_missing_fill": 0,
            },
            [],
        )

    disclosure = (
        frame.loc[:, ["trade_date", "symbol"]]
        .assign(__period=frame["trade_date"].dt.to_period(rebalance_frequency))
        .join(feature_frame[selected_features])
        .sort_values(["symbol", "trade_date"])
        .groupby(["symbol", "__period"], group_keys=False)
        .tail(1)[["symbol", "__period", *selected_features]]
        .reset_index(drop=True)
    )

    pre_fill = period_grid.merge(disclosure, on=["symbol", "__period"], how="left")
    pre_fill = pre_fill.sort_values(["symbol", "__period"]).reset_index(drop=True)
    if bool(settings["fundamentals_ffill"]) and selected_features:
        pre_fill[selected_features] = pre_fill.groupby("symbol")[selected_features].ffill(
            limit=settings["fundamentals_ffill_limit"]
        )

    post_fill = pre_fill.copy()
    missing_features = list(settings["missing_features"])
    if missing_features:
        for feature in missing_features:
            post_fill[feature] = pd.to_numeric(post_fill[feature], errors="coerce")
        if str(settings["missing_method"]) == "zero":
            post_fill[missing_features] = post_fill[missing_features].fillna(0.0)
        elif str(settings["missing_method"]) == "cross_sectional_median":
            period_medians = post_fill.groupby("__period")[missing_features].transform("median")
            post_fill[missing_features] = post_fill[missing_features].fillna(period_medians)

    any_mask = (
        pre_fill[selected_features].notna().any(axis=1)
        if selected_features
        else pd.Series(True, index=pre_fill.index)
    )
    pre_all_mask = (
        pre_fill[selected_features].notna().all(axis=1)
        if selected_features
        else pd.Series(True, index=pre_fill.index)
    )
    post_all_mask = (
        post_fill[selected_features].notna().all(axis=1)
        if selected_features
        else pd.Series(True, index=post_fill.index)
    )

    period_table = (
        period_grid.groupby("__period")["symbol"].nunique().rename("active_symbols").to_frame()
    )
    period_table["symbols_with_any_selected_features_after_ffill"] = (
        pre_fill.loc[any_mask].groupby("__period")["symbol"].nunique()
    )
    period_table["symbols_with_all_selected_features_after_ffill"] = (
        pre_fill.loc[pre_all_mask].groupby("__period")["symbol"].nunique()
    )
    period_table["symbols_with_all_selected_features_after_missing_fill"] = (
        post_fill.loc[post_all_mask].groupby("__period")["symbol"].nunique()
    )
    period_table = period_table.fillna(0).astype(int).reset_index()
    period_table = period_table.sort_values("__period").reset_index(drop=True)
    period_table["period"] = period_table["__period"].astype(str)

    after_ffill_counts = period_table["symbols_with_all_selected_features_after_ffill"]
    after_missing_counts = period_table["symbols_with_all_selected_features_after_missing_fill"]

    estimate = {
        "feature_source": feature_source,
        "pit_features_considered": len(selected_features),
        "rebalance_frequency": rebalance_frequency,
        "sample_on_rebalance_dates": bool(settings["sample_on_rebalance_dates"]),
        "grid_source": grid_source,
        "fundamentals_ffill": bool(settings["fundamentals_ffill"]),
        "fundamentals_ffill_limit": settings["fundamentals_ffill_limit"],
        "missing_method": str(settings["missing_method"]),
        "missing_features_considered": len(missing_features),
        "indicator_features_added": len(missing_features) if bool(settings["add_indicators"]) else 0,
        "active_rows": int(len(period_grid)),
        "active_symbols": int(period_grid["symbol"].nunique()),
        "periods": int(period_table["period"].nunique()),
        "rows_with_all_selected_features_after_ffill": int(pre_all_mask.sum()),
        "rows_with_all_selected_features_after_missing_fill": int(post_all_mask.sum()),
        "period_symbols_median_after_ffill": int(after_ffill_counts.median()) if not period_table.empty else 0,
        "period_symbols_max_after_ffill": int(after_ffill_counts.max()) if not period_table.empty else 0,
        "period_count_meeting_min_symbols_after_ffill": int((after_ffill_counts >= min_symbols).sum()),
        "period_symbols_median_after_missing_fill": int(after_missing_counts.median())
        if not period_table.empty
        else 0,
        "period_symbols_max_after_missing_fill": int(after_missing_counts.max())
        if not period_table.empty
        else 0,
        "period_count_meeting_min_symbols_after_missing_fill": int(
            (after_missing_counts >= min_symbols).sum()
        ),
    }

    output_rows = period_table[
        [
            "period",
            "active_symbols",
            "symbols_with_any_selected_features_after_ffill",
            "symbols_with_all_selected_features_after_ffill",
            "symbols_with_all_selected_features_after_missing_fill",
        ]
    ]
    return estimate, output_rows.to_dict(orient="records")


def _assess_trainable_fill_dependence(
    *,
    trainable_estimate: Mapping[str, object],
    non_pit_features_ignored: Sequence[str],
) -> dict[str, object]:
    after_ffill = int(trainable_estimate.get("period_count_meeting_min_symbols_after_ffill") or 0)
    after_missing_fill = int(
        trainable_estimate.get("period_count_meeting_min_symbols_after_missing_fill") or 0
    )
    route_type = "hybrid" if list(non_pit_features_ignored) else "pit_only"
    thresholds = {
        "pit_only": {"green": 0.60, "yellow": 0.30},
        "hybrid": {"green": 0.40, "yellow": 0.15},
    }
    route_thresholds = thresholds[route_type]
    recovered_periods = max(after_missing_fill - after_ffill, 0)
    retention_ratio = (
        round(float(after_ffill / after_missing_fill), 4) if after_missing_fill > 0 else 0.0
    )
    fill_dependency_ratio = (
        round(float(recovered_periods / after_missing_fill), 4) if after_missing_fill > 0 else 0.0
    )

    if after_missing_fill <= 0:
        status = "red"
        message = "缺失填补后仍然没有季度样本达到 min_symbols。先停下来检查资产或特征集。"
        next_step = "先重建 PIT 资产或缩窄 PIT 特征，再决定是否继续这条研究线。"
    elif retention_ratio >= route_thresholds["green"]:
        status = "green"
        message = "这条配置对横截面填补的依赖在可接受范围内。"
        next_step = "可以继续跑基线或模型比较，同时保留这份体检结果。"
    elif retention_ratio >= route_thresholds["yellow"]:
        status = "yellow"
        message = "这条配置能训练，但对横截面填补有明显依赖。"
        next_step = "先看拖后腿字段，再考虑缩窄 PIT 特征或补资产覆盖。"
    else:
        status = "red"
        message = "这条配置主要靠横截面填补在维持季度样本。"
        next_step = "先收窄 PIT 特征或补资产覆盖，再做模型比较。"

    return {
        "route_type": route_type,
        "status": status,
        "periods_after_ffill": after_ffill,
        "periods_after_missing_fill": after_missing_fill,
        "recovered_periods_from_missing_fill": recovered_periods,
        "retention_ratio_after_ffill": retention_ratio,
        "fill_dependency_ratio_from_missing_fill": fill_dependency_ratio,
        "green_threshold": route_thresholds["green"],
        "yellow_threshold": route_thresholds["yellow"],
        "message": message,
        "next_step": next_step,
    }


def inspect_hk_pit_coverage(args) -> int:
    resolved_config = resolve_pipeline_config(args.config) if getattr(args, "config", None) else None
    config_data = resolved_config.data if resolved_config else None

    asset_dir = _resolve_path(args.asset_dir) if getattr(args, "asset_dir", None) else None
    fundamentals_file = _resolve_path(args.fundamentals_file) if getattr(args, "fundamentals_file", None) else None
    if config_data and fundamentals_file is None:
        fundamentals_cfg = config_data.get("fundamentals") if isinstance(config_data, Mapping) else None
        if isinstance(fundamentals_cfg, Mapping):
            fundamentals_file_ref = fundamentals_cfg.get("file")
            if fundamentals_cfg.get("source", "file") == "file" and fundamentals_file_ref:
                fundamentals_file = _resolve_path(str(fundamentals_file_ref))

    if asset_dir is None and fundamentals_file is not None and fundamentals_file.name == DEFAULT_PIPELINE_FUNDAMENTALS_NAME:
        asset_dir = fundamentals_file.parent
    if asset_dir is not None and fundamentals_file is None:
        fundamentals_file = _default_pipeline_fundamentals_path(asset_dir)

    if fundamentals_file is None:
        raise SystemExit(
            "No fundamentals source resolved. Pass --config, --asset-dir, or --fundamentals-file."
        )
    if not fundamentals_file.exists():
        raise SystemExit(f"Fundamentals file not found: {fundamentals_file}")

    if asset_dir is None:
        manifest_candidate = _pipeline_fundamentals_manifest_path(fundamentals_file)
        pipeline_manifest = _load_manifest(manifest_candidate) if manifest_candidate.exists() else None
        if isinstance(pipeline_manifest, Mapping):
            source_asset_dir = pipeline_manifest.get("source_asset_dir")
            if source_asset_dir:
                candidate = _resolve_path(str(source_asset_dir))
                if candidate.exists():
                    asset_dir = candidate
    asset_manifest = _load_manifest(asset_dir / "manifest.yml") if asset_dir and (asset_dir / "manifest.yml").exists() else None
    pipeline_manifest_path = _pipeline_fundamentals_manifest_path(fundamentals_file)
    pipeline_manifest = _load_manifest(pipeline_manifest_path) if pipeline_manifest_path.exists() else None

    if fundamentals_file.suffix.lower() in {".parquet", ".pq"}:
        frame = pd.read_parquet(fundamentals_file)
    else:
        frame = pd.read_csv(fundamentals_file)
    frame = _normalize_frame_columns(frame)
    frame = ensure_symbol_columns(frame, context=f"Fundamentals file {fundamentals_file.name}")
    if "trade_date" not in frame.columns or "symbol" not in frame.columns:
        raise SystemExit(
            "Fundamentals file must include trade_date and a canonical symbol column "
            f"(legacy ts_code inputs remain compatible): {fundamentals_file}"
        )

    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    valid_trade_date = trade_dates.notna()
    invalid_trade_dates = int((~valid_trade_date).sum())
    frame = frame.loc[valid_trade_date].copy()
    trade_dates = trade_dates.loc[valid_trade_date].dt.normalize()
    frame["trade_date"] = trade_dates
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame = drop_legacy_symbol_columns(frame)
    frame = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    trade_dates = frame["trade_date"]
    available_columns = frame.columns.tolist()

    mode = str(getattr(args, "mode", "strict") or "strict").strip().lower()
    if mode not in {"strict", "trainable", "both"}:
        raise SystemExit("mode must be one of: strict, trainable, both")

    manifest_for_fields = pipeline_manifest if isinstance(pipeline_manifest, Mapping) else asset_manifest
    strict_features, strict_selection_meta = _resolve_pit_coverage_features(
        args=args,
        config_data=config_data,
        manifest=manifest_for_fields if isinstance(manifest_for_fields, Mapping) else None,
        available_columns=available_columns,
    )
    if mode == "strict":
        selected_features = strict_features
        selection_meta = strict_selection_meta
    else:
        selected_features, selection_meta = _resolve_trainable_pit_features(
            args=args,
            config_data=config_data,
            available_columns=available_columns,
            fallback_features=strict_features,
            fallback_metadata=strict_selection_meta,
        )
    min_symbols = getattr(args, "min_symbols", None)
    if min_symbols is None and isinstance(config_data, Mapping):
        universe_cfg = get_research_universe_config(config_data)
        if isinstance(universe_cfg, Mapping):
            min_symbols = universe_cfg.get("min_symbols_per_date")
    if min_symbols is None:
        min_symbols = 5
    min_symbols = int(min_symbols)

    feature_cache: dict[str, pd.Series] = {}
    feature_series = {
        feature: _compute_pit_coverage_series(frame, feature, cache=feature_cache)
        for feature in selected_features
    }
    feature_frame = pd.DataFrame(feature_series, index=frame.index)

    total_rows = int(len(frame))
    total_symbols = int(frame["symbol"].nunique())
    total_dates = int(trade_dates.nunique())
    quarter_labels = trade_dates.dt.to_period("Q").astype(str)
    total_quarters = int(pd.Index(quarter_labels).nunique())
    date_counts = frame.groupby("trade_date")["symbol"].nunique()
    fail_on_severity = normalize_fail_on_severity(getattr(args, "fail_on_severity", "none"))
    include_health = bool(
        getattr(args, "include_health", False)
        or getattr(args, "target_date", None)
        or getattr(args, "symbols_file", None)
        or getattr(args, "by_date_file", None)
        or fail_on_severity != "none"
    )

    if selected_features:
        complete_rows_mask = feature_frame.notna().all(axis=1)
    else:
        complete_rows_mask = pd.Series(True, index=frame.index)
    complete_rows = int(complete_rows_mask.sum())
    complete_symbols = int(frame.loc[complete_rows_mask, "symbol"].nunique())

    quarter_latest = (
        frame.loc[:, ["trade_date", "symbol"]]
        .assign(__quarter=quarter_labels)
        .join(feature_frame)
        .sort_values(["__quarter", "symbol", "trade_date"])
        .groupby(["__quarter", "symbol"], group_keys=False)
        .tail(1)
        .reset_index(drop=True)
    )
    quarter_feature_frame = (
        quarter_latest[selected_features] if selected_features else pd.DataFrame(index=quarter_latest.index)
    )
    quarter_any_mask = quarter_feature_frame.notna().any(axis=1) if selected_features else pd.Series(True, index=quarter_latest.index)
    quarter_complete_mask = (
        quarter_feature_frame.notna().all(axis=1)
        if selected_features
        else pd.Series(True, index=quarter_latest.index)
    )
    quarter_table = (
        quarter_latest.groupby("__quarter")["symbol"].nunique().rename("symbols_in_file").to_frame()
    )
    quarter_table["symbols_with_any_selected_feature"] = (
        quarter_latest.loc[quarter_any_mask].groupby("__quarter")["symbol"].nunique()
    )
    quarter_table["symbols_with_all_selected_features"] = (
        quarter_latest.loc[quarter_complete_mask].groupby("__quarter")["symbol"].nunique()
    )
    quarter_table = quarter_table.fillna(0).astype(int).reset_index().rename(columns={"__quarter": "quarter"})
    quarter_table = quarter_table.sort_values("quarter").reset_index(drop=True)

    complete_quarters = int((quarter_table["symbols_with_all_selected_features"] > 0).sum())
    quarter_count_meeting_min_symbols = int(
        (quarter_table["symbols_with_all_selected_features"] >= min_symbols).sum()
    )

    field_rows: list[dict[str, object]] = []
    base_complete_rows = complete_rows
    for feature in selected_features:
        series = feature_series[feature]
        mask = series.notna()
        relaxed_features = [item for item in selected_features if item != feature]
        if relaxed_features:
            relaxed_complete = int(feature_frame[relaxed_features].notna().all(axis=1).sum())
        else:
            relaxed_complete = total_rows
        quarters_with_values = int(pd.Index(quarter_labels[mask]).nunique()) if mask.any() else 0
        field_rows.append(
            {
                "feature": feature,
                "nonnull_rows": int(mask.sum()),
                "row_coverage_pct": round(float(mask.mean() * 100.0), 2),
                "symbols_with_values": int(frame.loc[mask, "symbol"].nunique()),
                "symbol_coverage_pct": round(
                    float(frame.loc[mask, "symbol"].nunique() / total_symbols * 100.0) if total_symbols else 0.0,
                    2,
                ),
                "quarters_with_values": quarters_with_values,
                "quarter_coverage_pct": round(
                    float(quarters_with_values / total_quarters * 100.0) if total_quarters else 0.0,
                    2,
                ),
                "complete_case_row_lift_if_dropped": int(relaxed_complete - base_complete_rows),
            }
        )
    field_rows.sort(
        key=lambda item: (
            float(item["row_coverage_pct"]),
            -int(item["complete_case_row_lift_if_dropped"]),
            str(item["feature"]),
        )
    )

    trainable_estimate: dict[str, object] | None = None
    trainable_period_rows: list[dict[str, object]] | None = None
    fill_dependence_assessment: dict[str, object] | None = None
    if mode in {"trainable", "both"}:
        trainable_estimate, trainable_period_rows = _estimate_trainable_pit_coverage(
            frame=frame,
            feature_frame=feature_frame,
            selected_features=selected_features,
            config_data=config_data,
            min_symbols=min_symbols,
            feature_source=str(selection_meta.get("source") or "explicit"),
        )
        non_pit_ignored = list(selection_meta.get("non_pit_ignored_features") or [])
        if non_pit_ignored:
            trainable_estimate["non_pit_features_ignored"] = non_pit_ignored
        fill_dependence_assessment = _assess_trainable_fill_dependence(
            trainable_estimate=trainable_estimate,
            non_pit_features_ignored=non_pit_ignored,
        )
    health_section = (
        _build_pit_health_section(
            args=args,
            config_data=config_data,
            asset_manifest=asset_manifest if isinstance(asset_manifest, Mapping) else None,
            frame=frame,
            feature_frame=feature_frame,
            selected_features=selected_features,
            min_symbols=min_symbols,
        )
        if include_health
        else None
    )

    payload = {
        "source": {
            "config": resolved_config.source if resolved_config else None,
            "fundamentals_file": str(fundamentals_file),
            "asset_dir": str(asset_dir) if asset_dir else None,
        },
        "selection": {
            "mode": mode,
            "source": selection_meta.get("source"),
            "count": len(selected_features),
            "requested_features": list(selection_meta.get("requested_features") or []),
            "selected_features": selected_features,
            "ignored_features": list(selection_meta.get("ignored_features") or []),
            "min_symbols_threshold": min_symbols,
        },
        "summary": {
            "rows": total_rows,
            "symbols": total_symbols,
            "dates": total_dates,
            "quarters": total_quarters,
            "min_trade_date": trade_dates.min().strftime("%Y-%m-%d") if total_rows else None,
            "max_trade_date": trade_dates.max().strftime("%Y-%m-%d") if total_rows else None,
            "median_symbols_per_date": int(date_counts.median()) if not date_counts.empty else 0,
            "max_symbols_per_date": int(date_counts.max()) if not date_counts.empty else 0,
            "invalid_trade_dates_dropped": invalid_trade_dates,
        },
        "pipeline_manifest_totals": (
            dict(pipeline_manifest.get("totals"))
            if isinstance(pipeline_manifest, Mapping) and isinstance(pipeline_manifest.get("totals"), Mapping)
            else None
        ),
        "complete_case": {
            "complete_rows": complete_rows,
            "complete_row_pct": round(float(complete_rows / total_rows * 100.0) if total_rows else 0.0, 2),
            "complete_symbols": complete_symbols,
            "complete_quarters": complete_quarters,
            "quarter_complete_symbols_median": int(quarter_table["symbols_with_all_selected_features"].median())
            if not quarter_table.empty
            else 0,
            "quarter_complete_symbols_max": int(quarter_table["symbols_with_all_selected_features"].max())
            if not quarter_table.empty
            else 0,
            "quarter_count_meeting_min_symbols": quarter_count_meeting_min_symbols,
        },
        "field_coverage": field_rows,
        "quarter_coverage": quarter_table.to_dict(orient="records"),
        "trainable_estimate": trainable_estimate,
        "fill_dependence_assessment": fill_dependence_assessment,
        "trainable_period_coverage": trainable_period_rows,
        "health": health_section,
        "quality_verdict": (
            health_section.get("quality_verdict")
            if isinstance(health_section, Mapping) and isinstance(health_section.get("quality_verdict"), Mapping)
            else None
        ),
    }

    output_format = str(getattr(args, "format", "text") or "text").strip().lower()
    if output_format not in {"text", "json"}:
        raise SystemExit("format must be one of: text, json")
    if output_format == "json":
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        rendered = _render_hk_pit_coverage_text(
            payload,
            top=int(getattr(args, "top", 10) or 10),
            quarter_limit=int(getattr(args, "quarter_limit", 12) or 12),
        )

    out_path = _resolve_path(args.out) if getattr(args, "out", None) else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return quality_gate_exit_code(
        payload.get("quality_verdict") if isinstance(payload.get("quality_verdict"), Mapping) else None
    )
