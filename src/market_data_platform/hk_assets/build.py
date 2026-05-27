from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd

from market_data_platform.config_utils import resolve_pipeline_config
from market_data_platform.pit_feature_stats import (
    compute_calendar_cagr,
    compute_trailing_calendar_window_stat,
)
from market_data_platform.symbols import drop_legacy_symbol_columns, ensure_symbol_columns
from .build_paths import (
    default_pipeline_fundamentals_path as _default_pipeline_fundamentals_path,
    pipeline_fundamentals_manifest_path as _pipeline_fundamentals_manifest_path,
    resolve_pipeline_fundamentals_out_path as _resolve_pipeline_fundamentals_out_path,
    write_symbol_list as _write_symbol_list,
)
from .shared import (
    DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX,
    DEFAULT_PIPELINE_FUNDAMENTALS_NAME,
    DERIVED_PIT_FEATURES,
    HK_INDUSTRY_HIERARCHY_COLUMNS,
    PIT_METADATA_COLUMNS,
    _coerce_bool,
    _git_metadata,
    _load_manifest,
    _normalize_absolute_date,
    _normalize_field_list,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _resolve_fields_with_overrides,
    _resolve_path,
    _resolve_universe_by_date_columns,
    _write_manifest,
)


def _resolve_fields(args) -> tuple[list[str], dict]:
    package = sys.modules.get("market_data_platform.hk_assets")
    override = getattr(package, "_load_hk_financial_fields", None) if package is not None else None
    return _resolve_fields_with_overrides(
        args,
        load_hk_financial_fields_override=override,
    )


def _resolve_pit_asset_dir(path_text: str | Path) -> tuple[Path, dict | None]:
    asset_dir = _resolve_path(path_text)
    if not asset_dir.exists():
        raise SystemExit(f"PIT asset directory not found: {asset_dir}")
    data_dir = asset_dir / "data"
    if not data_dir.is_dir():
        raise SystemExit(f"PIT asset directory is missing data/: {asset_dir}")
    manifest = _load_manifest(asset_dir / "manifest.yml")
    if manifest and manifest.get("dataset") not in {None, "pit_financials"}:
        raise SystemExit(
            f"Expected a pit_financials asset directory, got dataset={manifest.get('dataset')!r}: {asset_dir}"
        )
    return asset_dir, manifest


def _resolve_industry_changes_asset_dir(path_text: str | Path) -> tuple[Path, dict | None]:
    asset_dir = _resolve_path(path_text)
    if not asset_dir.exists():
        raise SystemExit(f"Industry changes asset directory not found: {asset_dir}")
    data_dir = asset_dir / "data"
    if not data_dir.is_dir():
        raise SystemExit(f"Industry changes asset directory is missing data/: {asset_dir}")
    manifest = _load_manifest(asset_dir / "manifest.yml")
    if manifest and manifest.get("dataset") not in {None, "industry_changes"}:
        raise SystemExit(
            f"Expected an industry_changes asset directory, got dataset={manifest.get('dataset')!r}: {asset_dir}"
        )
    return asset_dir, manifest


def _default_hk_industry_labels_path(asset_dir: Path, frequency: str) -> Path:
    suffix = str(frequency or "D").strip().upper()
    return asset_dir / f"{DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX}_{suffix.lower()}.parquet"


def _resolve_hk_industry_labels_out_path(args, asset_dir: Path) -> Path:
    out = getattr(args, "out", None)
    if out:
        return _resolve_path(out)
    frequency = str(getattr(args, "frequency", "D") or "D").strip().upper()
    return _default_hk_industry_labels_path(asset_dir, frequency)


def _industry_labels_manifest_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}.manifest.yml")


def _resolve_hk_label_frequency(args) -> str:
    frequency = str(getattr(args, "frequency", "D") or "D").strip().upper()
    if frequency not in {"D", "M", "Q"}:
        raise SystemExit("--frequency must be one of: D, M, Q.")
    return frequency


def _resolve_optional_absolute_date(value: object, *, label: str) -> str | None:
    if value in {None, ""}:
        return None
    return _normalize_absolute_date(value, label=label)


def _load_trade_date_grid_from_daily_asset_dir(
    daily_asset_dir: Path,
    *,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    data_dir = daily_asset_dir / "data"
    if not data_dir.exists():
        raise SystemExit(f"Daily asset directory is missing data/: {daily_asset_dir}")

    start_ts = pd.to_datetime(start_date, format="%Y%m%d", errors="coerce") if start_date else None
    end_ts = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce") if end_date else None

    parts: list[pd.DataFrame] = []
    for path in sorted(data_dir.glob("*.parquet")):
        try:
            frame = pd.read_parquet(path, columns=["trade_date", "symbol", "ts_code"])
        except Exception:
            frame = pd.read_parquet(path)
        frame = _normalize_frame_columns(frame)
        if frame.empty:
            continue
        if "trade_date" not in frame.columns:
            continue
        if "symbol" not in frame.columns and "ts_code" not in frame.columns:
            frame["symbol"] = path.stem
        frame = ensure_symbol_columns(frame, context=f"Daily asset file {path.name}")
        trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        valid = trade_dates.notna()
        if not valid.any():
            continue
        work = frame.loc[valid, ["symbol"]].copy()
        work["trade_date"] = trade_dates.loc[valid].dt.normalize()
        work["symbol"] = work["symbol"].astype(str).str.strip().map(_normalize_hk_symbol)
        work = work[work["symbol"] != ""].copy()
        if start_ts is not None:
            work = work[work["trade_date"] >= start_ts.normalize()].copy()
        if end_ts is not None:
            work = work[work["trade_date"] <= end_ts.normalize()].copy()
        if work.empty:
            continue
        work = work.drop_duplicates(subset=["trade_date"]).reset_index(drop=True)
        parts.append(work[["trade_date", "symbol"]])

    if not parts:
        raise SystemExit(f"No trade_date grid rows resolved from daily assets under {daily_asset_dir}")
    grid = pd.concat(parts, ignore_index=True)
    return grid.drop_duplicates().sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def _sample_trade_date_grid(grid: pd.DataFrame, *, frequency: str) -> tuple[pd.DataFrame, dict[str, object]]:
    if grid.empty:
        return grid.copy(), {"sampling_mode": "empty", "frequency": frequency, "rows_in": 0, "rows_out": 0}
    if frequency == "D":
        sampled = grid.copy()
        sampling_mode = "all_trade_dates"
    else:
        work = grid.copy()
        work["__period"] = work["trade_date"].dt.to_period(frequency)
        sampled = (
            work.sort_values(["symbol", "trade_date"])
            .groupby(["symbol", "__period"], group_keys=False)
            .tail(1)[["trade_date", "symbol"]]
            .reset_index(drop=True)
        )
        sampling_mode = "period_last_trade_date"
    sampled = sampled.drop_duplicates().sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    return sampled, {
        "sampling_mode": sampling_mode,
        "frequency": frequency,
        "rows_in": int(len(grid)),
        "rows_out": int(len(sampled)),
        "symbols": int(sampled["symbol"].nunique()) if not sampled.empty else 0,
        "trade_dates": int(sampled["trade_date"].nunique()) if not sampled.empty else 0,
    }


def _resolve_hk_industry_label_grid(args) -> tuple[pd.DataFrame, dict[str, object]]:
    frequency = _resolve_hk_label_frequency(args)
    source_universe = getattr(args, "source_universe_by_date", None)
    daily_asset_dir_arg = getattr(args, "daily_asset_dir", None)
    if bool(source_universe) == bool(daily_asset_dir_arg):
        raise SystemExit("Provide exactly one of --source-universe-by-date or --daily-asset-dir.")

    start_date = _resolve_optional_absolute_date(getattr(args, "start_date", None), label="--start-date")
    end_date = _resolve_optional_absolute_date(getattr(args, "end_date", None), label="--end-date")
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    if source_universe:
        source_path = _resolve_path(source_universe)
        if not source_path.exists():
            raise SystemExit(f"Universe-by-date file not found: {source_path}")
        grid = _load_universe_by_date_frame(source_path)
        if start_date:
            start_ts = pd.to_datetime(start_date, format="%Y%m%d", errors="coerce").normalize()
            grid = grid[grid["trade_date"] >= start_ts].copy()
        if end_date:
            end_ts = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce").normalize()
            grid = grid[grid["trade_date"] <= end_ts].copy()
        sampled, sample_meta = _sample_trade_date_grid(grid, frequency=frequency)
        meta = {
            "mode": "source_universe_by_date",
            "source_universe_by_date": str(source_path),
            "requested_frequency": frequency,
            "start_date": start_date,
            "end_date": end_date,
        }
        meta.update(sample_meta)
        return sampled, meta

    daily_asset_dir = _resolve_path(daily_asset_dir_arg)
    if (daily_asset_dir / "data").exists():
        resolved_daily_asset_dir = daily_asset_dir
    elif daily_asset_dir.name == "data" and daily_asset_dir.is_dir():
        resolved_daily_asset_dir = daily_asset_dir.parent
    else:
        raise SystemExit(f"Daily asset directory is missing data/: {daily_asset_dir}")
    grid = _load_trade_date_grid_from_daily_asset_dir(
        resolved_daily_asset_dir,
        start_date=start_date,
        end_date=end_date,
    )
    sampled, sample_meta = _sample_trade_date_grid(grid, frequency=frequency)
    meta = {
        "mode": "daily_asset_dir",
        "daily_asset_dir": str(resolved_daily_asset_dir),
        "requested_frequency": frequency,
        "start_date": start_date,
        "end_date": end_date,
    }
    meta.update(sample_meta)
    return sampled, meta


def _load_industry_changes_frame(data_files: Sequence[Path]) -> tuple[pd.DataFrame, int]:
    frames: list[pd.DataFrame] = []
    input_rows = 0
    for data_file in data_files:
        frame = _normalize_frame_columns(pd.read_parquet(data_file))
        input_rows += int(len(frame))
        if frame.empty:
            continue
        frame = ensure_symbol_columns(frame, context=f"Industry changes asset file {data_file.name}")
        required = {"symbol", "start_date", "cancel_date"}
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise SystemExit(
                f"Industry changes asset file is missing required columns {missing}: {data_file}"
            )
        work = frame.copy()
        work["symbol"] = work["symbol"].astype(str).str.strip().map(_normalize_hk_symbol)
        work = work[work["symbol"] != ""].copy()
        if work.empty:
            continue
        work["start_date"] = pd.to_datetime(work["start_date"], errors="coerce").dt.normalize()
        work["cancel_date"] = pd.to_datetime(work["cancel_date"], errors="coerce").dt.normalize()
        work = work[work["start_date"].notna()].copy()
        if work.empty:
            continue
        sort_columns = [column for column in ("symbol", "start_date", "cancel_date", "industry_code") if column in work.columns]
        if sort_columns:
            work = work.sort_values(sort_columns).reset_index(drop=True)
        frames.append(work)
    if not frames:
        return pd.DataFrame(), input_rows
    combined = pd.concat(frames, ignore_index=True)
    dedupe_subset = [column for column in ("symbol", "start_date", "cancel_date", "industry_code") if column in combined.columns]
    if dedupe_subset:
        combined = combined.drop_duplicates(subset=dedupe_subset, keep="last")
    combined = combined.sort_values([column for column in ("symbol", "start_date", "cancel_date", "industry_code") if column in combined.columns]).reset_index(drop=True)
    return combined, input_rows


def _derive_hk_industry_labels(
    *,
    grid: pd.DataFrame,
    intervals: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    if grid.empty:
        return pd.DataFrame(columns=["trade_date", "symbol"]), 0
    if intervals.empty:
        output = grid.copy()
        output["trade_date"] = output["trade_date"].dt.strftime("%Y%m%d")
        return output, int(len(output))

    parts: list[pd.DataFrame] = []
    invalid_rows = 0
    interval_groups = {symbol: frame.copy() for symbol, frame in intervals.groupby("symbol", sort=False)}

    for symbol, symbol_grid in grid.groupby("symbol", sort=False):
        left = symbol_grid.copy().sort_values("trade_date").reset_index(drop=True)
        right = interval_groups.get(symbol)
        if right is None or right.empty:
            parts.append(left)
            continue
        right = right.sort_values("start_date").reset_index(drop=True)
        merged = pd.merge_asof(
            left,
            right,
            left_on="trade_date",
            right_on="start_date",
            direction="backward",
            allow_exact_matches=True,
        )
        if "symbol_x" in merged.columns:
            merged = merged.rename(columns={"symbol_x": "symbol"})
        if "symbol_y" in merged.columns:
            merged = merged.drop(columns=["symbol_y"])
        cancel_date = pd.to_datetime(merged.get("cancel_date"), errors="coerce")
        valid_mask = cancel_date.isna() | (merged["trade_date"] < cancel_date)
        invalid_rows += int((~valid_mask).sum())
        label_columns = [column for column in merged.columns if column not in {"trade_date", "symbol"}]
        merged.loc[~valid_mask, label_columns] = pd.NA
        parts.append(merged)

    combined = pd.concat(parts, ignore_index=True) if parts else grid.copy()
    combined["trade_date"] = pd.to_datetime(combined["trade_date"], errors="coerce").dt.strftime("%Y%m%d")
    return combined, invalid_rows


def _resolve_build_fields(
    *,
    args,
    manifest: Mapping[str, object] | None,
    available_columns: Sequence[str],
) -> tuple[list[str], dict]:
    if getattr(args, "field_profile", None) or getattr(args, "field", None) or getattr(args, "fields_file", None):
        fields, metadata = _resolve_fields(args)
        fields = _normalize_field_list(fields)
        metadata["source"] = "explicit"
    else:
        manifest_fields: list[str] = []
        if manifest:
            query = manifest.get("query")
            if isinstance(query, Mapping):
                raw_fields = query.get("fields")
                if isinstance(raw_fields, Sequence) and not isinstance(raw_fields, str):
                    manifest_fields = [str(item) for item in raw_fields]
        fields = _normalize_field_list(manifest_fields)
        source = "asset_manifest"
        if not fields:
            excluded = {"symbol", "ts_code", "order_book_id", *PIT_METADATA_COLUMNS}
            fields = [
                field
                for field in _normalize_field_list(available_columns)
                if field not in excluded
            ]
            source = "inferred"
        metadata = {"count": len(fields), "fields_file": [], "source": source}
    if not fields:
        raise SystemExit("No PIT value fields resolved for building fundamentals.")
    available = set(_normalize_field_list(available_columns))
    missing = [field for field in fields if field not in available]
    if missing:
        raise SystemExit(
            "Requested PIT fields are not available in the asset: " + ", ".join(missing)
        )
    return fields, metadata


def _load_universe_by_date_frame(path_text: str | Path) -> pd.DataFrame:
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
    trade_date_text = (
        df["trade_date"].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    )
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
    df["trade_date"] = parsed
    df = df[df["trade_date"].notna()].copy()
    df["trade_date"] = df["trade_date"].dt.normalize()
    df["symbol"] = df["symbol"].astype(str).str.strip().map(_normalize_hk_symbol)
    df = df[df["symbol"] != ""].copy()
    return df[["trade_date", "symbol"]].drop_duplicates().sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _parse_trade_date_series(values: pd.Series) -> pd.Series:
    text = values.astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    digits_mask = text.str.fullmatch(r"\d{8}")
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    if digits_mask.any():
        parsed.loc[digits_mask] = pd.to_datetime(
            text.loc[digits_mask],
            format="%Y%m%d",
            errors="coerce",
        )
    if (~digits_mask).any():
        parsed.loc[~digits_mask] = pd.to_datetime(
            text.loc[~digits_mask],
            errors="coerce",
        )
    return parsed.dt.normalize()


def _is_supported_pit_feature(feature: str, available_columns: set[str]) -> bool:
    if feature in available_columns:
        return True
    if feature == "days_since_report":
        return True
    if feature.startswith("delta_") or feature.startswith("growth_"):
        return _is_supported_pit_feature(feature.split("_", 1)[1], available_columns)
    return feature in DERIVED_PIT_FEATURES


def _compute_pit_feature_series(
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
        return pd.Series(pd.NA, index=index, dtype="Float64")

    def _numeric(name: str) -> pd.Series:
        if name not in frame.columns:
            return _nan_series()
        return pd.to_numeric(frame[name], errors="coerce")

    def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
        valid_denominator = denominator.where(denominator.notna() & (denominator != 0))
        return (numerator / valid_denominator).replace([float("inf"), float("-inf")], pd.NA)

    def _get(name: str) -> pd.Series:
        return _compute_pit_feature_series(frame, name, cache=cache)

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
        series = _safe_ratio(
            _get("net_profit") - _get("cash_flow_from_operating_activities"),
            _get("total_assets"),
        )
    elif feature == "receivables_to_revenue":
        series = _safe_ratio(_get("accounts_receivable"), _get("revenue"))
    elif feature == "inventory_to_revenue":
        series = _safe_ratio(_get("inventory"), _get("revenue"))
    elif feature == "working_capital_to_assets":
        working_capital = _get("accounts_receivable") + _get("inventory") - _get("accounts_payable")
        series = _safe_ratio(working_capital, _get("total_assets"))
    elif feature == "net_debt_to_assets":
        series = _safe_ratio(_get("debt") - _get("cash_and_equivalents"), _get("total_assets"))
    elif feature == "days_since_report":
        series = pd.Series(0.0, index=index, dtype=float)
    elif feature == "sales_cagr_3y":
        series = compute_calendar_cagr(frame, _get("sales"), years=3).astype("Float64")
    elif feature == "eps_cagr_3y":
        series = compute_calendar_cagr(frame, _get("basic_earnings_per_share"), years=3).astype(
            "Float64"
        )
    elif feature == "cfo_margin_avg_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cfo_margin"),
            years=3,
            stat="mean",
            min_periods=3,
        ).astype("Float64")
    elif feature == "profit_margin_std_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("profit_margin"),
            years=3,
            stat="std",
            min_periods=3,
        ).astype("Float64")
    elif feature == "cfo_to_profit_median_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cfo_to_profit"),
            years=3,
            stat="median",
            min_periods=3,
        ).astype("Float64")
    elif feature == "positive_cfo_ratio_3y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cash_flow_from_operating_activities"),
            years=3,
            stat="positive_ratio",
            min_periods=3,
        ).astype("Float64")
    elif feature == "positive_cfo_ratio_2y":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cash_flow_from_operating_activities"),
            years=2,
            stat="positive_ratio",
            min_periods=2,
        ).astype("Float64")
    elif feature == "positive_cfo_ratio_3y_min2":
        series = compute_trailing_calendar_window_stat(
            frame,
            _get("cash_flow_from_operating_activities"),
            years=3,
            stat="positive_ratio",
            min_periods=2,
        ).astype("Float64")
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
        series = ((current - previous) / scale).replace([float("inf"), float("-inf")], pd.NA)
    else:
        series = _nan_series()

    cache[feature] = series
    return series


def _resolve_feature_age_filter_config(
    config_path: Path,
    *,
    available_columns: Sequence[str],
) -> dict[str, object]:
    resolved = resolve_pipeline_config(str(config_path)).data
    features_cfg = resolved.get("features")
    features_cfg = features_cfg if isinstance(features_cfg, Mapping) else {}
    fundamentals_cfg = resolved.get("fundamentals")
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

    available_set = set(_normalize_field_list(available_columns))
    supported_features = [
        feature for feature in model_features if _is_supported_pit_feature(feature, available_set)
    ]
    ignored_features = [feature for feature in model_features if feature not in supported_features]
    if not supported_features:
        raise SystemExit(
            "No PIT-backed features resolved from --feature-age-config. "
            "Check features.list or fundamentals.features."
        )
    return {
        "config_path": str(config_path),
        "source": source,
        "requested_features": model_features,
        "selected_features": supported_features,
        "ignored_features": ignored_features,
    }


def _apply_selected_feature_age_filter(
    *,
    filtered: pd.DataFrame,
    date_col: str,
    fundamentals: pd.DataFrame,
    selected_features: Sequence[str],
    max_selected_feature_age_days: int,
) -> tuple[pd.DataFrame, dict[str, object]]:
    if max_selected_feature_age_days < 0:
        raise SystemExit("--max-selected-feature-age-days must be >= 0.")

    rows_before_filter = int(len(filtered))
    if filtered.empty:
        return filtered, {
            "max_selected_feature_age_days": int(max_selected_feature_age_days),
            "rows_before_feature_age_filter": rows_before_filter,
            "rows_after_feature_age_filter": 0,
            "rows_dropped_missing_selected_feature_asof_trade_date": 0,
            "rows_dropped_stale_selected_feature": 0,
            "feature_drop_summary": [],
            "sample_dropped_missing_selected_feature_symbols": [],
            "sample_dropped_stale_selected_feature_symbols": [],
        }

    left = filtered.loc[:, ["symbol", "_trade_date_ts"]].copy()
    left["_source_index"] = left.index
    left = left.sort_values(["_trade_date_ts", "symbol"]).reset_index(drop=True)

    work = fundamentals.copy()
    work["symbol"] = work["symbol"].astype(str).str.strip().map(_normalize_hk_symbol)
    work["_pit_trade_date_ts"] = _parse_trade_date_series(work["trade_date"])
    work = work.loc[
        work["symbol"].ne("") & work["_pit_trade_date_ts"].notna()
    ].copy()

    cache: dict[str, pd.Series] = {}
    missing_any = pd.Series(False, index=left.index)
    stale_any = pd.Series(False, index=left.index)
    feature_drop_summary: list[dict[str, object]] = []

    for feature in selected_features:
        values = _compute_pit_feature_series(work, feature, cache=cache)
        available = work.loc[values.notna(), ["symbol", "_pit_trade_date_ts"]].drop_duplicates()
        if available.empty:
            latest = pd.Series(pd.NaT, index=left.index, dtype="datetime64[ns]")
        else:
            available = available.sort_values(["_pit_trade_date_ts", "symbol"]).reset_index(drop=True)
            merged = pd.merge_asof(
                left,
                available,
                left_on="_trade_date_ts",
                right_on="_pit_trade_date_ts",
                by="symbol",
                direction="backward",
            )
            latest = merged["_pit_trade_date_ts"]

        age_days = (left["_trade_date_ts"] - latest).dt.days
        missing_mask = latest.isna()
        stale_mask = (~missing_mask) & (age_days > max_selected_feature_age_days)
        missing_any |= missing_mask
        stale_any |= stale_mask
        feature_drop_summary.append(
            {
                "feature": feature,
                "rows_missing_asof_trade_date": int(missing_mask.sum()),
                "rows_stale_gt_max_age": int(stale_mask.sum()),
                "sample_missing_symbols": (
                    left.loc[missing_mask, "symbol"].astype(str).drop_duplicates().head(5).tolist()
                ),
                "sample_stale_symbols": (
                    left.loc[stale_mask, "symbol"].astype(str).drop_duplicates().head(5).tolist()
                ),
            }
        )

    drop_missing_mask = missing_any
    drop_stale_mask = (~missing_any) & stale_any
    keep_indices = left.loc[~(drop_missing_mask | drop_stale_mask), "_source_index"].tolist()
    result = filtered.loc[keep_indices].copy()
    return result, {
        "max_selected_feature_age_days": int(max_selected_feature_age_days),
        "rows_before_feature_age_filter": rows_before_filter,
        "rows_after_feature_age_filter": int(len(result)),
        "rows_dropped_missing_selected_feature_asof_trade_date": int(drop_missing_mask.sum()),
        "rows_dropped_stale_selected_feature": int(drop_stale_mask.sum()),
        "feature_drop_summary": feature_drop_summary,
        "sample_dropped_missing_selected_feature_symbols": (
            left.loc[drop_missing_mask, "symbol"].astype(str).drop_duplicates().head(5).tolist()
        ),
        "sample_dropped_stale_selected_feature_symbols": (
            left.loc[drop_stale_mask, "symbol"].astype(str).drop_duplicates().head(5).tolist()
        ),
    }


def _build_filtered_universe_by_date(
    *,
    source_path: Path,
    out_path: Path,
    symbols: Sequence[str],
    fundamentals: pd.DataFrame | None = None,
    max_latest_report_age_days: int | None = None,
    max_selected_feature_age_days: int | None = None,
    feature_age_config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    universe = pd.read_csv(source_path)
    date_col, symbol_col = _resolve_universe_by_date_columns(universe)
    normalized_symbols = universe[symbol_col].astype(str).map(_normalize_hk_symbol)
    selected_symbols = set(symbols)
    filtered = universe.loc[normalized_symbols.isin(selected_symbols)].copy()
    filtered["symbol"] = normalized_symbols.loc[filtered.index]

    age_filter_summary = None
    feature_age_filter_summary = None
    needs_fundamentals = (
        max_latest_report_age_days is not None
        or max_selected_feature_age_days is not None
    )
    if needs_fundamentals:
        if fundamentals is None:
            raise SystemExit(
                "fundamentals frame is required when PIT universe age filters are used."
            )
        filtered_dates = _parse_trade_date_series(filtered[date_col])
        valid_date_mask = filtered_dates.notna()
        filtered = filtered.loc[valid_date_mask].copy()
        filtered["_trade_date_ts"] = filtered_dates.loc[valid_date_mask]

    if max_latest_report_age_days is not None:
        if max_latest_report_age_days < 0:
            raise SystemExit("--max-latest-report-age-days must be >= 0.")
        pit_dates = fundamentals.loc[:, ["symbol", "trade_date"]].copy()
        pit_dates["symbol"] = (
            pit_dates["symbol"].astype(str).str.strip().map(_normalize_hk_symbol)
        )
        pit_dates["_pit_trade_date_ts"] = _parse_trade_date_series(pit_dates["trade_date"])
        pit_dates = pit_dates.loc[
            pit_dates["symbol"].ne("") & pit_dates["_pit_trade_date_ts"].notna(),
            ["symbol", "_pit_trade_date_ts"],
        ].drop_duplicates()

        rows_before_age_filter = int(len(filtered))
        if filtered.empty or pit_dates.empty:
            rows_dropped_missing_asof_pit = rows_before_age_filter
            rows_dropped_stale_latest_report = 0
            sample_dropped_stale_symbols: list[str] = []
            sample_dropped_missing_asof_pit_symbols: list[str] = []
            filtered = filtered.iloc[0:0].copy()
        else:
            left = filtered.loc[:, ["symbol", "_trade_date_ts"]].copy()
            left["_source_index"] = left.index
            left = left.sort_values(["_trade_date_ts", "symbol"]).reset_index(drop=True)
            pit_dates = pit_dates.sort_values(["_pit_trade_date_ts", "symbol"]).reset_index(drop=True)
            merged = pd.merge_asof(
                left,
                pit_dates,
                left_on="_trade_date_ts",
                right_on="_pit_trade_date_ts",
                by="symbol",
                direction="backward",
            )
            report_age_days = (
                (merged["_trade_date_ts"] - merged["_pit_trade_date_ts"]).dt.days
            )
            missing_asof_mask = merged["_pit_trade_date_ts"].isna()
            stale_mask = (~missing_asof_mask) & (report_age_days > max_latest_report_age_days)
            keep_indices = merged.loc[
                ~(missing_asof_mask | stale_mask), "_source_index"
            ].tolist()
            rows_dropped_missing_asof_pit = int(missing_asof_mask.sum())
            rows_dropped_stale_latest_report = int(stale_mask.sum())
            sample_dropped_stale_symbols = (
                merged.loc[stale_mask, "symbol"]
                .astype(str)
                .drop_duplicates()
                .head(5)
                .tolist()
            )
            sample_dropped_missing_asof_pit_symbols = (
                merged.loc[missing_asof_mask, "symbol"]
                .astype(str)
                .drop_duplicates()
                .head(5)
                .tolist()
            )
            filtered = filtered.loc[keep_indices].copy()

        age_filter_summary = {
            "max_latest_report_age_days": int(max_latest_report_age_days),
            "rows_before_age_filter": rows_before_age_filter,
            "rows_after_age_filter": int(len(filtered)),
            "rows_dropped_no_pit_asof_trade_date": rows_dropped_missing_asof_pit,
            "rows_dropped_stale_latest_report": rows_dropped_stale_latest_report,
            "sample_dropped_no_pit_asof_trade_date_symbols": sample_dropped_missing_asof_pit_symbols,
            "sample_dropped_stale_latest_report_symbols": sample_dropped_stale_symbols,
        }

    if max_selected_feature_age_days is not None:
        if feature_age_config is None:
            raise SystemExit(
                "--max-selected-feature-age-days requires --feature-age-config."
            )
        filtered, feature_age_summary = _apply_selected_feature_age_filter(
            filtered=filtered,
            date_col=date_col,
            fundamentals=fundamentals,
            selected_features=list(feature_age_config["selected_features"]),
            max_selected_feature_age_days=max_selected_feature_age_days,
        )
        feature_age_filter_summary = {
            **feature_age_summary,
            "config_path": feature_age_config["config_path"],
            "feature_source": feature_age_config["source"],
            "selected_features": list(feature_age_config["selected_features"]),
            "ignored_features": list(feature_age_config["ignored_features"]),
        }

    filtered = drop_legacy_symbol_columns(filtered)
    if symbol_col == "order_book_id":
        filtered = filtered.drop(columns=["order_book_id"], errors="ignore")
    filtered = filtered.drop(columns=["_trade_date_ts"], errors="ignore")

    preferred = []
    seen: set[str] = set()
    for column in [date_col, "symbol", *filtered.columns]:
        if column in filtered.columns and column not in seen:
            preferred.append(column)
            seen.add(column)
    filtered = filtered.loc[:, preferred]
    filtered = filtered.drop_duplicates().reset_index(drop=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    filtered.to_csv(out_path, index=False)
    return {
        "source_path": str(source_path),
        "output_path": str(out_path),
        "rows": int(len(filtered)),
        "symbols": int(filtered["symbol"].nunique()) if not filtered.empty else 0,
        "date_column": date_col,
        "symbol_column": "symbol",
        "latest_report_age_filter": age_filter_summary,
        "selected_feature_age_filter": feature_age_filter_summary,
    }


def build_hk_pit_fundamentals_file(args) -> int:
    asset_dir, source_manifest = _resolve_pit_asset_dir(args.asset_dir)
    data_dir = asset_dir / "data"
    data_files = sorted(data_dir.glob("*.parquet"))
    if not data_files:
        raise SystemExit(f"No parquet files found under {data_dir}")

    first_frame = _normalize_frame_columns(pd.read_parquet(data_files[0]))
    available_columns = list(source_manifest.get("columns") or []) if source_manifest else []
    if not available_columns:
        available_columns = first_frame.columns.tolist()
    fields, field_metadata = _resolve_build_fields(
        args=args,
        manifest=source_manifest,
        available_columns=available_columns,
    )

    out_path = _resolve_pipeline_fundamentals_out_path(args, asset_dir)
    force = bool(getattr(args, "force", False))
    if out_path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing output: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    symbols_out_path = _resolve_path(args.symbols_out) if getattr(args, "symbols_out", None) else None
    if symbols_out_path and symbols_out_path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing output: {symbols_out_path}")
    source_universe_path = (
        _resolve_path(args.source_universe_by_date)
        if getattr(args, "source_universe_by_date", None)
        else None
    )
    universe_out_path = (
        _resolve_path(args.universe_by_date_out)
        if getattr(args, "universe_by_date_out", None)
        else None
    )
    max_latest_report_age_days = getattr(args, "max_latest_report_age_days", None)
    max_selected_feature_age_days = getattr(args, "max_selected_feature_age_days", None)
    feature_age_config_path = (
        _resolve_path(args.feature_age_config)
        if getattr(args, "feature_age_config", None)
        else None
    )
    if universe_out_path and source_universe_path is None:
        raise SystemExit("--source-universe-by-date is required when --universe-by-date-out is set.")
    if source_universe_path and not source_universe_path.exists():
        raise SystemExit(f"Universe-by-date file not found: {source_universe_path}")
    if feature_age_config_path and not feature_age_config_path.exists():
        raise SystemExit(f"Feature-age config file not found: {feature_age_config_path}")
    if universe_out_path and universe_out_path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing output: {universe_out_path}")
    if max_latest_report_age_days is not None and universe_out_path is None:
        raise SystemExit(
            "--max-latest-report-age-days requires --source-universe-by-date and --universe-by-date-out."
        )
    if max_selected_feature_age_days is not None and universe_out_path is None:
        raise SystemExit(
            "--max-selected-feature-age-days requires --source-universe-by-date and --universe-by-date-out."
        )
    if max_selected_feature_age_days is not None and feature_age_config_path is None:
        raise SystemExit("--max-selected-feature-age-days requires --feature-age-config.")
    if feature_age_config_path is not None and max_selected_feature_age_days is None:
        raise SystemExit("--feature-age-config requires --max-selected-feature-age-days.")

    combined_frames: list[pd.DataFrame] = []
    input_rows = 0
    dropped_missing_info_date = 0
    dropped_all_missing_fields = 0
    precombine_duplicate_rows_seen = 0
    precombine_duplicate_rows_dropped = 0
    cached_frames: dict[Path, pd.DataFrame] = {data_files[0]: first_frame}
    keep_meta = bool(getattr(args, "keep_meta", False))

    for data_file in data_files:
        frame = cached_frames.get(data_file)
        if frame is None:
            frame = _normalize_frame_columns(pd.read_parquet(data_file))
        input_rows += int(len(frame))
        if frame.empty:
            continue
        frame = ensure_symbol_columns(frame, context=f"PIT asset file {data_file.name}")
        if "symbol" not in frame.columns or "info_date" not in frame.columns:
            raise SystemExit(
                "PIT asset file must include canonical symbol and info_date columns "
                f"(legacy ts_code inputs remain compatible): {data_file}"
            )
        missing_fields = [field for field in fields if field not in frame.columns]
        if missing_fields:
            raise SystemExit(
                f"PIT asset file is missing requested fields {missing_fields}: {data_file}"
            )

        work = frame.copy()
        work["symbol"] = work["symbol"].astype(str).str.strip()
        info_dates = pd.to_datetime(work["info_date"], errors="coerce")
        valid_info_date = info_dates.notna()
        dropped_missing_info_date += int((~valid_info_date).sum())
        work = work.loc[valid_info_date].copy()
        if work.empty:
            continue
        work["info_date"] = info_dates.loc[valid_info_date].dt.normalize()
        work["trade_date"] = work["info_date"].dt.strftime("%Y%m%d")
        empty_value_rows = work[fields].isna().all(axis=1)
        dropped_all_missing_fields += int(empty_value_rows.sum())
        work = work.loc[~empty_value_rows].copy()
        if work.empty:
            continue

        # Early dedup per file: keep only the latest row for each symbol + trade_date pair.
        # Different info_date values must survive into the combined frame.
        if "trade_date" in work.columns and "symbol" in work.columns:
            local_duplicate_rows_seen = int(
                work.duplicated(subset=["trade_date", "symbol"], keep=False).sum()
            )
            work = work.sort_values(["trade_date", "rice_create_tm"] if "rice_create_tm" in work.columns else ["trade_date"])
            deduped_work = work.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
            precombine_duplicate_rows_seen += local_duplicate_rows_seen
            precombine_duplicate_rows_dropped += int(len(work) - len(deduped_work))
            work = deduped_work

        combined_frames.append(work)

    meta_columns = [col for col in PIT_METADATA_COLUMNS if keep_meta and col in available_columns]
    output_columns = ["trade_date", "symbol", *fields, *meta_columns]
    duplicate_rows_seen = 0
    duplicate_rows_dropped = 0
    if combined_frames:
        combined = pd.concat(combined_frames, ignore_index=True)
        sort_columns = [
            col
            for col in [
                "symbol",
                "trade_date",
                "quarter",
                "fiscal_year",
                "info_date",
                "rice_create_tm",
                "if_adjusted",
                "standard",
            ]
            if col in combined.columns
        ]
        if sort_columns:
            combined = combined.sort_values(sort_columns).reset_index(drop=True)
        duplicate_rows_seen = int(
            combined.duplicated(subset=["trade_date", "symbol"], keep=False).sum()
        )
        duplicate_rows_seen += precombine_duplicate_rows_seen
        if duplicate_rows_seen and getattr(args, "duplicate_policy", "keep-last") == "error":
            raise SystemExit(
                "Duplicate trade_date + symbol rows found in PIT asset. "
                "Retry with --duplicate-policy keep-last if you want automatic deduplication."
            )
        deduped = combined.drop_duplicates(subset=["trade_date", "symbol"], keep="last")
        duplicate_rows_dropped = precombine_duplicate_rows_dropped + int(len(combined) - len(deduped))
        output_df = deduped.loc[:, [col for col in output_columns if col in deduped.columns]].copy()
        output_df = output_df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    else:
        output_df = pd.DataFrame(columns=output_columns)
    research_symbols = sorted(output_df["symbol"].astype(str).str.strip().unique().tolist()) if not output_df.empty else []

    if out_path.suffix.lower() == ".csv":
        output_df.to_csv(out_path, index=False)
        output_format = "csv"
    else:
        output_df.to_parquet(out_path, index=False)
        output_format = "parquet"

    outputs = {"pipeline_fundamentals": str(out_path)}
    if symbols_out_path:
        _write_symbol_list(symbols_out_path, research_symbols)
        outputs["symbols_file"] = str(symbols_out_path)
    filtered_universe = None
    if source_universe_path and universe_out_path:
        feature_age_config = None
        if feature_age_config_path is not None:
            feature_age_config = _resolve_feature_age_filter_config(
                feature_age_config_path,
                available_columns=output_df.columns.tolist(),
            )
        filtered_universe = _build_filtered_universe_by_date(
            source_path=source_universe_path,
            out_path=universe_out_path,
            symbols=research_symbols,
            fundamentals=output_df,
            max_latest_report_age_days=max_latest_report_age_days,
            max_selected_feature_age_days=max_selected_feature_age_days,
            feature_age_config=feature_age_config,
        )
        outputs["universe_by_date_file"] = str(universe_out_path)

    output_manifest = {
        "name": out_path.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "completed",
        "dataset": "pit_fundamentals_file",
        "market": "hk",
        "source_asset_dir": str(asset_dir),
        "source_manifest": str(asset_dir / "manifest.yml") if (asset_dir / "manifest.yml").exists() else None,
        "source_query": source_manifest.get("query") if isinstance(source_manifest, Mapping) else None,
        "symbol_source": source_manifest.get("symbol_source") if isinstance(source_manifest, Mapping) else None,
        "output_file": str(out_path),
        "output_format": output_format,
        "query": {
            "fields_count": len(fields),
            "fields": list(fields),
            "field_profile": list(field_metadata.get("field_profile") or []),
            "fields_file": list(field_metadata.get("fields_file") or []),
            "field_source": field_metadata.get("source"),
            "keep_meta": keep_meta,
            "duplicate_policy": getattr(args, "duplicate_policy", "keep-last"),
            "max_latest_report_age_days": max_latest_report_age_days,
            "feature_age_config": str(feature_age_config_path) if feature_age_config_path is not None else None,
            "max_selected_feature_age_days": max_selected_feature_age_days,
        },
        "columns": output_df.columns.tolist(),
        "totals": {
            "input_files": len(data_files),
            "input_rows": input_rows,
            "output_rows": int(len(output_df)),
            "symbols": int(output_df["symbol"].nunique()) if not output_df.empty else 0,
            "dropped_missing_info_date": dropped_missing_info_date,
            "dropped_all_missing_fields": dropped_all_missing_fields,
            "duplicate_rows_seen": duplicate_rows_seen,
            "duplicate_rows_dropped": duplicate_rows_dropped,
        },
        "outputs": outputs,
        "filtered_universe": filtered_universe,
        "git": _git_metadata(Path.cwd().resolve()),
    }
    _write_manifest(_pipeline_fundamentals_manifest_path(out_path), output_manifest)

    print(
        f"Wrote HK PIT fundamentals file to {out_path} "
        f"({len(output_df)} rows, {len(fields)} value columns, {output_format})"
    )
    return 0


def build_hk_industry_labels_file(args) -> int:
    asset_dir, source_manifest = _resolve_industry_changes_asset_dir(args.asset_dir)
    data_dir = asset_dir / "data"
    data_files = sorted(data_dir.glob("*.parquet"))
    if not data_files:
        raise SystemExit(f"No parquet files found under {data_dir}")

    frequency = _resolve_hk_label_frequency(args)
    out_path = _resolve_hk_industry_labels_out_path(args, asset_dir)
    force = bool(getattr(args, "force", False))
    if out_path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing output: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    symbols_out_path = _resolve_path(args.symbols_out) if getattr(args, "symbols_out", None) else None
    if symbols_out_path and symbols_out_path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing output: {symbols_out_path}")

    grid, grid_metadata = _resolve_hk_industry_label_grid(args)
    intervals, input_rows = _load_industry_changes_frame(data_files)
    output_df, interval_miss_rows = _derive_hk_industry_labels(grid=grid, intervals=intervals)
    output_df = output_df.sort_values(["trade_date", "symbol"]).reset_index(drop=True)

    if out_path.suffix.lower() == ".csv":
        output_df.to_csv(out_path, index=False)
        output_format = "csv"
    else:
        output_df.to_parquet(out_path, index=False)
        output_format = "parquet"

    resolved_symbols = (
        sorted(output_df["symbol"].astype(str).str.strip().unique().tolist())
        if "symbol" in output_df.columns and not output_df.empty
        else []
    )
    outputs = {"industry_labels_file": str(out_path)}
    if symbols_out_path:
        _write_symbol_list(symbols_out_path, resolved_symbols)
        outputs["symbols_file"] = str(symbols_out_path)

    label_value_columns = [
        column
        for column in (
            "industry_code",
            "industry_name",
            "industry_level",
            "industry_source",
            *HK_INDUSTRY_HIERARCHY_COLUMNS,
        )
        if column in output_df.columns
    ]
    resolved_rows = 0
    if label_value_columns:
        resolved_rows = int(output_df[label_value_columns].notna().any(axis=1).sum())

    output_manifest = {
        "name": out_path.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "completed",
        "dataset": "industry_labels_file",
        "market": "hk",
        "source_asset_dir": str(asset_dir),
        "source_manifest": str(asset_dir / "manifest.yml") if (asset_dir / "manifest.yml").exists() else None,
        "source_query": source_manifest.get("query") if isinstance(source_manifest, Mapping) else None,
        "symbol_source": source_manifest.get("symbol_source") if isinstance(source_manifest, Mapping) else None,
        "output_file": str(out_path),
        "output_format": output_format,
        "query": {
            "frequency": frequency,
            "grid_mode": grid_metadata.get("mode"),
            "source_universe_by_date": grid_metadata.get("source_universe_by_date"),
            "daily_asset_dir": grid_metadata.get("daily_asset_dir"),
            "start_date": grid_metadata.get("start_date"),
            "end_date": grid_metadata.get("end_date"),
        },
        "grid": grid_metadata,
        "columns": output_df.columns.tolist(),
        "totals": {
            "input_files": len(data_files),
            "input_rows": input_rows,
            "grid_rows": int(len(grid)),
            "output_rows": int(len(output_df)),
            "resolved_rows": resolved_rows,
            "unresolved_rows": int(len(output_df) - resolved_rows),
            "interval_miss_rows": interval_miss_rows,
            "symbols": int(output_df["symbol"].nunique()) if "symbol" in output_df.columns and not output_df.empty else 0,
            "trade_dates": int(output_df["trade_date"].nunique()) if "trade_date" in output_df.columns and not output_df.empty else 0,
        },
        "outputs": outputs,
        "git": _git_metadata(Path.cwd().resolve()),
    }
    _write_manifest(_industry_labels_manifest_path(out_path), output_manifest)

    print(
        f"Wrote HK industry labels file to {out_path} "
        f"({len(output_df)} rows, frequency={frequency}, {output_format})"
    )
    return 0
