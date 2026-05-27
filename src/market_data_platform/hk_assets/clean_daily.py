from __future__ import annotations

import json
import os
import shutil
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from .asset_io import (
    _daily_audit_record,
    _field_coverage_template,
    _load_existing_daily_entry,
    _update_field_coverage,
    _write_daily_audit_csv,
    _write_daily_symbol_frame,
)
from .manifest_ops import _build_daily_manifest
from .shared import (
    DATE_TEXT_OUTPUT_COLUMNS,
    _load_manifest,
    _load_text_list,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _path_mtime_iso,
    _resolve_path,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)

_KEY_COLUMNS = {"trade_date", "symbol", "order_book_id", "ts_code"}
_PRICE_FIELDS = ("open", "high", "low", "close")
_NULL_OUT_ZERO_RUN_FIELDS = ("open", "high", "low", "close", "volume", "total_turnover")
_NEGATIVE_TO_NULL_FIELDS = ("volume", "total_turnover")
_AUDIT_SYMBOL_COLUMNS = ("symbol", "ts_code", "order_book_id")
_DEFAULT_HK_ETF_INSTRUMENTS_PATH = Path("artifacts/assets/rqdata/hk/instruments/hk_etf_instruments_latest.parquet")
_ETF_LEVERAGED_OR_INVERSE_KEYWORDS = (
    "leveraged",
    "leverage",
    "inverse",
    "bear",
    "short",
    "ultra",
    "2x",
    "3x",
    "兩倍",
    "两倍",
    "槓桿",
    "杠杆",
    "反向",
)
_ETF_CRYPTO_OR_COMMODITY_KEYWORDS = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "比特幣",
    "比特币",
    "以太幣",
    "以太币",
    "commodity",
    "gold",
    "silver",
    "oil",
    "gas",
    "crude",
    "metal",
    "metals",
)


def _ensure_clean_dir(path: Path, *, overwrite: bool) -> None:
    if path.exists() or path.is_symlink():
        if not overwrite:
            raise SystemExit(f"Output path already exists: {path}")
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _resolve_link_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).absolute()


def _looks_like_etf_asset(asset_dir: Path) -> bool:
    return any("etf" in str(part).lower() for part in asset_dir.parts)


def _resolve_etf_instruments_path(asset_dir: Path, explicit_path: str | None) -> Path | None:
    if explicit_path:
        path = _resolve_path(explicit_path)
        if not path.exists():
            raise SystemExit(f"ETF instruments file not found: {path}")
        return path
    if not _looks_like_etf_asset(asset_dir):
        return None
    default_path = _resolve_path(_DEFAULT_HK_ETF_INSTRUMENTS_PATH)
    if default_path.exists():
        return default_path
    return None


def _classify_etf_product_profile(metadata: Mapping[str, object]) -> str:
    combined = " ".join(
        str(metadata.get(column) or "").strip().lower()
        for column in ("name", "eng_symbol", "type")
    )
    if any(keyword in combined for keyword in _ETF_LEVERAGED_OR_INVERSE_KEYWORDS):
        return "leveraged_or_inverse"
    if any(keyword in combined for keyword in _ETF_CRYPTO_OR_COMMODITY_KEYWORDS):
        return "crypto_or_commodity"
    return "vanilla"


def _load_etf_metadata_by_symbol(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    frame = _normalize_frame_columns(pd.read_parquet(path))
    if frame.empty or "symbol" not in frame.columns:
        return {}

    rows: dict[str, dict[str, object]] = {}
    for _, row in frame.iterrows():
        symbol = _normalize_hk_symbol(row.get("symbol"))
        if not symbol:
            continue
        payload = {
            "symbol": symbol,
            "name": row.get("name"),
            "eng_symbol": row.get("eng_symbol"),
            "type": row.get("type"),
        }
        payload["product_profile"] = _classify_etf_product_profile(payload)
        rows[symbol] = payload
    return rows


def _create_relative_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    rel_target = os.path.relpath(target, start=link.parent)
    os.symlink(rel_target, link, target_is_directory=target.is_dir())


def _resolve_source_symbols(asset_dir: Path, explicit_symbols_file: str | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def _append(values: Sequence[str]) -> None:
        for value in values:
            symbol = _normalize_hk_symbol(value)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            ordered.append(symbol)

    if explicit_symbols_file:
        _append(_load_text_list(explicit_symbols_file, label="Symbols file"))
        return ordered

    default_symbols_path = asset_dir / "symbols.txt"
    if default_symbols_path.exists():
        _append(_load_text_list(default_symbols_path, label="Source symbols.txt"))
        return ordered

    _append([path.stem for path in (asset_dir / "data").glob("*.parquet")])
    return sorted(ordered)


def _load_source_audit_by_symbol(asset_dir: Path) -> dict[str, dict[str, object]]:
    audit_path = asset_dir / "audit.csv"
    if not audit_path.exists():
        return {}
    try:
        audit = pd.read_csv(audit_path)
    except pd.errors.EmptyDataError:
        return {}
    audit = _normalize_frame_columns(audit)
    if audit.empty:
        return {}

    symbol_col = next((column for column in _AUDIT_SYMBOL_COLUMNS if column in audit.columns), None)
    if symbol_col is None:
        return {}

    rows_by_symbol: dict[str, dict[str, object]] = {}
    for _, row in audit.iterrows():
        symbol = _normalize_hk_symbol(row.get(symbol_col))
        if not symbol:
            continue
        rows_by_symbol[symbol] = {
            str(column): (None if pd.isna(value) else value)
            for column, value in row.to_dict().items()
        }
    return rows_by_symbol


def _resolve_source_fields(
    asset_dir: Path,
    *,
    manifest: Mapping[str, object] | None,
    sample_columns: Sequence[str],
) -> tuple[list[str], dict[str, object]]:
    fields_path = asset_dir / "fields.txt"
    if fields_path.exists():
        fields = [item for item in _load_text_list(fields_path, label="Source fields.txt") if str(item).strip()]
        return fields, {
            "fields_file": [str(fields_path)],
            "source": "source_fields_txt",
            "base_fields": list(fields),
        }

    if isinstance(manifest, Mapping):
        query = manifest.get("query")
        if isinstance(query, Mapping):
            manifest_fields = query.get("fields")
            if isinstance(manifest_fields, Sequence) and not isinstance(manifest_fields, (str, bytes)):
                fields = [str(item).strip() for item in manifest_fields if str(item).strip()]
                if fields:
                    return fields, {
                        "fields_file": list(query.get("fields_file") or []),
                        "source": "source_manifest_query_fields",
                        "base_fields": list(query.get("base_fields") or fields),
                    }

    inferred = [column for column in sample_columns if column not in _KEY_COLUMNS]
    return inferred, {
        "fields_file": [],
        "source": "inferred_non_key_columns",
        "base_fields": list(inferred),
    }


def _format_trade_date(value: object) -> str | None:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.normalize().strftime("%Y-%m-%d")


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _find_true_segments(mask: np.ndarray, *, min_run: int) -> list[tuple[int, int]]:
    if min_run <= 0:
        raise SystemExit("--zero-price-min-run must be > 0.")
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for idx, flag in enumerate(mask.tolist()):
        if flag and start is None:
            start = idx
            continue
        if flag:
            continue
        if start is not None and idx - start >= min_run:
            segments.append((start, idx - 1))
        start = None
    if start is not None and len(mask) - start >= min_run:
        segments.append((start, len(mask) - 1))
    return segments


def _count_daily_anomalies(frame: pd.DataFrame) -> dict[str, int]:
    open_arr = _numeric_series(frame, "open").to_numpy(dtype="float64")
    high_arr = _numeric_series(frame, "high").to_numpy(dtype="float64")
    low_arr = _numeric_series(frame, "low").to_numpy(dtype="float64")
    close_arr = _numeric_series(frame, "close").to_numpy(dtype="float64")
    finite_mask = (
        np.isfinite(open_arr)
        & np.isfinite(high_arr)
        & np.isfinite(low_arr)
        & np.isfinite(close_arr)
    )
    upper_bound = np.maximum.reduce([open_arr, low_arr, close_arr])
    lower_bound = np.minimum.reduce([open_arr, high_arr, close_arr])
    bounds_mask = finite_mask & ((high_arr < upper_bound) | (low_arr > lower_bound))
    nonpositive_mask = finite_mask & (np.minimum.reduce([open_arr, high_arr, low_arr, close_arr]) <= 0.0)

    summary = {
        "remaining_price_bounds_rows": int(np.count_nonzero(bounds_mask)),
        "remaining_nonpositive_price_rows": int(np.count_nonzero(nonpositive_mask)),
        "remaining_negative_volume_rows": 0,
        "remaining_negative_total_turnover_rows": 0,
    }
    for field_name, summary_key in (
        ("volume", "remaining_negative_volume_rows"),
        ("total_turnover", "remaining_negative_total_turnover_rows"),
    ):
        numeric = _numeric_series(frame, field_name).to_numpy(dtype="float64")
        summary[summary_key] = int(np.count_nonzero(np.isfinite(numeric) & (numeric < 0.0)))
    return summary


def _append_action(
    actions: list[dict[str, object]],
    *,
    symbol: str,
    action: str,
    rows_affected: int,
    trade_dates: pd.Series,
    extra: Mapping[str, object] | None = None,
) -> None:
    if rows_affected <= 0:
        return
    payload = {
        "symbol": symbol,
        "action": action,
        "rows_affected": int(rows_affected),
        "first_trade_date": _format_trade_date(trade_dates.min()),
        "last_trade_date": _format_trade_date(trade_dates.max()),
    }
    if extra:
        payload.update(extra)
    actions.append(payload)


def _clean_daily_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    zero_price_min_run: int,
    etf_metadata: Mapping[str, object] | None = None,
    etf_short_zero_max_run: int = 0,
) -> tuple[pd.DataFrame, list[dict[str, object]], dict[str, int], dict[str, object]]:
    work = _normalize_frame_columns(frame)
    if "trade_date" not in work.columns:
        raise SystemExit(f"Daily asset file is missing trade_date: {symbol}")

    parsed_dates = pd.to_datetime(work["trade_date"], errors="coerce")
    if parsed_dates.notna().any():
        order = np.argsort(parsed_dates.fillna(pd.Timestamp.max).to_numpy(dtype="datetime64[ns]"))
        work = work.iloc[order].reset_index(drop=True)
        parsed_dates = parsed_dates.iloc[order].reset_index(drop=True)
    else:
        parsed_dates = parsed_dates.reset_index(drop=True)

    actions: list[dict[str, object]] = []
    diagnostics = {
        "etf_product_profile": str((etf_metadata or {}).get("product_profile") or "") or None,
        "flagged_segments": [],
        "short_zero_segments_cleaned": 0,
        "short_zero_rows_cleaned": 0,
        "short_zero_segments_flagged_special": 0,
        "short_zero_rows_flagged_special": 0,
        "short_zero_segments_flagged_vanilla_over_limit": 0,
        "short_zero_rows_flagged_vanilla_over_limit": 0,
    }

    open_values = _numeric_series(work, "open").to_numpy(dtype="float64")
    high_values = _numeric_series(work, "high").to_numpy(dtype="float64")
    low_values = _numeric_series(work, "low").to_numpy(dtype="float64")
    close_values = _numeric_series(work, "close").to_numpy(dtype="float64")
    all_zero_mask = (
        np.isfinite(open_values)
        & np.isfinite(high_values)
        & np.isfinite(low_values)
        & np.isfinite(close_values)
        & (open_values == 0.0)
        & (high_values == 0.0)
        & (low_values == 0.0)
        & (close_values == 0.0)
    )
    all_zero_segments = _find_true_segments(all_zero_mask, min_run=1)
    zero_segments = [
        segment
        for segment in all_zero_segments
        if (segment[1] - segment[0] + 1) >= zero_price_min_run
    ]
    zero_run_mask = np.zeros(len(work), dtype=bool)
    for start_idx, end_idx in zero_segments:
        zero_run_mask[start_idx : end_idx + 1] = True
    if zero_run_mask.any():
        for column in _NULL_OUT_ZERO_RUN_FIELDS:
            if column in work.columns:
                work.loc[zero_run_mask, column] = np.nan
        _append_action(
            actions,
            symbol=symbol,
            action="zero_price_run_to_null",
            rows_affected=int(np.count_nonzero(zero_run_mask)),
            trade_dates=parsed_dates.loc[zero_run_mask],
            extra={"segments_affected": len(zero_segments)},
        )

    short_zero_segments = [
        segment
        for segment in all_zero_segments
        if (segment[1] - segment[0] + 1) < zero_price_min_run
    ]
    if short_zero_segments and diagnostics["etf_product_profile"] and int(etf_short_zero_max_run) > 0:
        cleanable_short_segments: list[tuple[int, int]] = []
        flagged_short_segments: list[tuple[tuple[int, int], str]] = []
        if diagnostics["etf_product_profile"] == "vanilla":
            for segment in short_zero_segments:
                run_length = int(segment[1] - segment[0] + 1)
                if run_length <= int(etf_short_zero_max_run):
                    cleanable_short_segments.append(segment)
                else:
                    flagged_short_segments.append((segment, "vanilla_run_over_limit"))
        else:
            flagged_short_segments = [
                (segment, f"special_product:{diagnostics['etf_product_profile']}")
                for segment in short_zero_segments
            ]

        etf_short_zero_mask = np.zeros(len(work), dtype=bool)
        for start_idx, end_idx in cleanable_short_segments:
            etf_short_zero_mask[start_idx : end_idx + 1] = True
        if etf_short_zero_mask.any():
            for column in _NULL_OUT_ZERO_RUN_FIELDS:
                if column in work.columns:
                    work.loc[etf_short_zero_mask, column] = np.nan
            diagnostics["short_zero_segments_cleaned"] = len(cleanable_short_segments)
            diagnostics["short_zero_rows_cleaned"] = int(np.count_nonzero(etf_short_zero_mask))
            _append_action(
                actions,
                symbol=symbol,
                action="etf_short_zero_price_run_to_null",
                rows_affected=int(np.count_nonzero(etf_short_zero_mask)),
                trade_dates=parsed_dates.loc[etf_short_zero_mask],
                extra={
                    "segments_affected": len(cleanable_short_segments),
                    "product_profile": diagnostics["etf_product_profile"],
                },
            )

        for segment, reason in flagged_short_segments:
            start_idx, end_idx = segment
            run_length = int(end_idx - start_idx + 1)
            payload = {
                "symbol": symbol,
                "product_profile": diagnostics["etf_product_profile"],
                "reason": reason,
                "run_length": run_length,
                "start_trade_date": _format_trade_date(parsed_dates.iloc[start_idx]),
                "end_trade_date": _format_trade_date(parsed_dates.iloc[end_idx]),
            }
            diagnostics["flagged_segments"].append(payload)
            if diagnostics["etf_product_profile"] == "vanilla":
                diagnostics["short_zero_segments_flagged_vanilla_over_limit"] = int(
                    diagnostics["short_zero_segments_flagged_vanilla_over_limit"]
                ) + 1
                diagnostics["short_zero_rows_flagged_vanilla_over_limit"] = int(
                    diagnostics["short_zero_rows_flagged_vanilla_over_limit"]
                ) + run_length
            else:
                diagnostics["short_zero_segments_flagged_special"] = int(
                    diagnostics["short_zero_segments_flagged_special"]
                ) + 1
                diagnostics["short_zero_rows_flagged_special"] = int(
                    diagnostics["short_zero_rows_flagged_special"]
                ) + run_length

    open_values = _numeric_series(work, "open").to_numpy(dtype="float64")
    high_values = _numeric_series(work, "high").to_numpy(dtype="float64")
    low_values = _numeric_series(work, "low").to_numpy(dtype="float64")
    close_values = _numeric_series(work, "close").to_numpy(dtype="float64")
    finite_price_mask = (
        np.isfinite(open_values)
        & np.isfinite(high_values)
        & np.isfinite(low_values)
        & np.isfinite(close_values)
    )
    positive_price_counts = (
        (open_values > 0.0).astype(int)
        + (high_values > 0.0).astype(int)
        + (low_values > 0.0).astype(int)
        + (close_values > 0.0).astype(int)
    )
    partial_nonpositive_price_mask = (
        finite_price_mask
        & (positive_price_counts > 0)
        & (positive_price_counts < len(_PRICE_FIELDS))
    )
    if partial_nonpositive_price_mask.any():
        fields_affected = 0
        for field_name in _PRICE_FIELDS:
            numeric = _numeric_series(work, field_name).to_numpy(dtype="float64")
            field_mask = partial_nonpositive_price_mask & (numeric <= 0.0)
            if not field_mask.any():
                continue
            work.loc[field_mask, field_name] = np.nan
            fields_affected += int(np.count_nonzero(field_mask))
        _append_action(
            actions,
            symbol=symbol,
            action="partial_nonpositive_price_to_null",
            rows_affected=int(np.count_nonzero(partial_nonpositive_price_mask)),
            trade_dates=parsed_dates.loc[partial_nonpositive_price_mask],
            extra={"fields_affected": fields_affected},
        )

    open_values = _numeric_series(work, "open").to_numpy(dtype="float64")
    high_values = _numeric_series(work, "high").to_numpy(dtype="float64")
    low_values = _numeric_series(work, "low").to_numpy(dtype="float64")
    close_values = _numeric_series(work, "close").to_numpy(dtype="float64")
    finite_positive_mask = (
        np.isfinite(open_values)
        & np.isfinite(high_values)
        & np.isfinite(low_values)
        & np.isfinite(close_values)
        & (np.minimum.reduce([open_values, high_values, low_values, close_values]) > 0.0)
    )
    upper_bound = np.maximum.reduce([open_values, low_values, close_values])
    lower_bound = np.minimum.reduce([open_values, high_values, close_values])
    price_bounds_mask = finite_positive_mask & ((high_values < upper_bound) | (low_values > lower_bound))
    if price_bounds_mask.any():
        fixed_high = np.maximum.reduce([open_values, high_values, low_values, close_values])
        fixed_low = np.minimum.reduce([open_values, high_values, low_values, close_values])
        work.loc[price_bounds_mask, "high"] = fixed_high[price_bounds_mask]
        work.loc[price_bounds_mask, "low"] = fixed_low[price_bounds_mask]
        _append_action(
            actions,
            symbol=symbol,
            action="price_bounds_fix",
            rows_affected=int(np.count_nonzero(price_bounds_mask)),
            trade_dates=parsed_dates.loc[price_bounds_mask],
        )

    for field_name in _NEGATIVE_TO_NULL_FIELDS:
        if field_name not in work.columns:
            continue
        numeric = _numeric_series(work, field_name)
        negative_mask = np.isfinite(numeric.to_numpy(dtype="float64")) & (numeric.to_numpy(dtype="float64") < 0.0)
        if not negative_mask.any():
            continue
        work.loc[negative_mask, field_name] = np.nan
        _append_action(
            actions,
            symbol=symbol,
            action=f"{field_name}_to_null",
            rows_affected=int(np.count_nonzero(negative_mask)),
            trade_dates=parsed_dates.loc[negative_mask],
        )

    if "trade_date" in work.columns and "trade_date" in DATE_TEXT_OUTPUT_COLUMNS:
        work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce").dt.strftime("%Y%m%d")

    anomaly_counts = _count_daily_anomalies(work)
    return work, actions, anomaly_counts, diagnostics


def build_hk_daily_clean_layer(args) -> int:
    asset_dir = _resolve_path(args.asset_dir)
    data_dir = asset_dir / "data"
    if not data_dir.exists():
        raise SystemExit(f"Asset directory is missing data/: {asset_dir}")

    out_dir = _resolve_path(args.out_dir)
    alias_path = _resolve_link_path(args.alias) if getattr(args, "alias", None) else None
    _ensure_clean_dir(out_dir, overwrite=bool(getattr(args, "overwrite", False)))
    out_data_dir = out_dir / "data"
    out_data_dir.mkdir(parents=True, exist_ok=True)

    source_files = {
        _normalize_hk_symbol(path.stem): path
        for path in sorted(data_dir.glob("*.parquet"))
        if _normalize_hk_symbol(path.stem)
    }
    if not source_files:
        raise SystemExit(f"No parquet files found under {data_dir}")
    source_audit_by_symbol = _load_source_audit_by_symbol(asset_dir)
    etf_instruments_path = _resolve_etf_instruments_path(asset_dir, getattr(args, "instruments_file", None))
    etf_metadata_by_symbol = _load_etf_metadata_by_symbol(etf_instruments_path)

    manifest_path = asset_dir / "manifest.yml"
    source_manifest = _load_manifest(manifest_path) if manifest_path.exists() else None
    dataset = str((source_manifest or {}).get("dataset") or "").strip() if isinstance(source_manifest, Mapping) else ""
    if dataset and dataset != "daily":
        raise SystemExit(f"Only daily assets are supported for local clean layers. Got dataset={dataset!r}.")

    sample_symbol = next(iter(source_files))
    sample_frame = _normalize_frame_columns(pd.read_parquet(source_files[sample_symbol]))
    fields, field_metadata = _resolve_source_fields(
        asset_dir,
        manifest=source_manifest,
        sample_columns=sample_frame.columns.tolist(),
    )
    schema_columns = sample_frame.columns.tolist()

    symbols_requested = _resolve_source_symbols(asset_dir, getattr(args, "symbols_file", None))
    if not symbols_requested:
        symbols_requested = sorted(source_files)

    entries_by_symbol = {}
    audit_records = []
    field_coverage = _field_coverage_template(fields)
    cleaning_actions: list[dict[str, object]] = []
    action_counts: Counter[str] = Counter()
    action_rows_by_type: Counter[str] = Counter()
    sample_symbols_by_action: dict[str, list[str]] = defaultdict(list)
    remaining_anomaly_totals: Counter[str] = Counter()
    etf_product_profile_counts: Counter[str] = Counter()
    etf_flag_reason_counts: Counter[str] = Counter()
    sample_etf_flagged_segments: list[dict[str, object]] = []
    etf_second_pass_totals: Counter[str] = Counter()
    started_at = _timestamp_now()

    for symbol in symbols_requested:
        source_path = source_files.get(symbol)
        finished_at = _timestamp_now()
        if source_path is None:
            source_audit = source_audit_by_symbol.get(symbol) or {}
            audit_records.append(
                _daily_audit_record(
                    symbol=symbol,
                    order_book_id=f"{symbol[:-3]}.XHKG",
                    status="missing_source_asset",
                    attempts=0,
                    started_at=None,
                    finished_at=finished_at,
                    file_mtime=None,
                    error=(
                        str(source_audit.get("error") or "").strip()
                        or "Source daily parquet is missing from the base asset snapshot."
                    ),
                    entry=None,
                )
            )
            continue

        try:
            frame = pd.read_parquet(source_path)
            etf_metadata = etf_metadata_by_symbol.get(symbol)
            cleaned_frame, symbol_actions, anomaly_counts, diagnostics = _clean_daily_frame(
                frame,
                symbol=symbol,
                zero_price_min_run=int(getattr(args, "zero_price_min_run", 5) or 5),
                etf_metadata=etf_metadata,
                etf_short_zero_max_run=int(getattr(args, "etf_short_zero_max_run", 4) or 4),
            )
        except Exception as exc:
            audit_records.append(
                _daily_audit_record(
                    symbol=symbol,
                    order_book_id=f"{symbol[:-3]}.XHKG",
                    status="failed",
                    attempts=1,
                    started_at=None,
                    finished_at=finished_at,
                    file_mtime=None,
                    error=str(exc),
                    entry=None,
                )
            )
            continue

        for key, value in anomaly_counts.items():
            remaining_anomaly_totals[key] += int(value)
        product_profile = str(diagnostics.get("etf_product_profile") or "").strip()
        if product_profile:
            etf_product_profile_counts[product_profile] += 1
        for key in (
            "short_zero_segments_cleaned",
            "short_zero_rows_cleaned",
            "short_zero_segments_flagged_special",
            "short_zero_rows_flagged_special",
            "short_zero_segments_flagged_vanilla_over_limit",
            "short_zero_rows_flagged_vanilla_over_limit",
        ):
            etf_second_pass_totals[key] += int(diagnostics.get(key) or 0)
        for segment in diagnostics.get("flagged_segments") or []:
            if not isinstance(segment, Mapping):
                continue
            reason = str(segment.get("reason") or "")
            if reason:
                etf_flag_reason_counts[reason] += 1
            if len(sample_etf_flagged_segments) < 5 and segment not in sample_etf_flagged_segments:
                sample_etf_flagged_segments.append(dict(segment))

        if symbol_actions:
            entry = _write_daily_symbol_frame(out_data_dir, cleaned_frame)
            status = "cleaned"
            for action in symbol_actions:
                cleaning_actions.append(action)
                action_name = str(action.get("action") or "")
                action_rows_by_type[action_name] += int(action.get("rows_affected") or 0)
                if action_name:
                    action_counts[action_name] += 1
                    sample_list = sample_symbols_by_action[action_name]
                    if symbol not in sample_list and len(sample_list) < 5:
                        sample_list.append(symbol)
        else:
            dest_path = out_data_dir / source_path.name
            _create_relative_symlink(source_path, dest_path)
            entry, symbol_frame = _load_existing_daily_entry(dest_path, fields=fields)
            _update_field_coverage(field_coverage, symbol_frame, fields=fields)
            entries_by_symbol[symbol] = entry
            audit_records.append(
                _daily_audit_record(
                    symbol=symbol,
                    order_book_id=entry.order_book_id,
                    status="linked_base",
                    attempts=0,
                    started_at=None,
                    finished_at=_path_mtime_iso(dest_path),
                    file_mtime=_path_mtime_iso(dest_path),
                    error=None,
                    entry=entry,
                )
            )
            continue

        entries_by_symbol[symbol] = entry
        _update_field_coverage(field_coverage, cleaned_frame, fields=fields)
        audit_records.append(
            _daily_audit_record(
                symbol=symbol,
                order_book_id=entry.order_book_id,
                status=status,
                attempts=1,
                started_at=None,
                finished_at=_path_mtime_iso(entry.path),
                file_mtime=_path_mtime_iso(entry.path),
                error=None,
                entry=entry,
            )
        )

    audit_path = out_dir / "audit.csv"
    actions_path = out_dir / "cleaning_actions.csv"
    report_path = out_dir / "cleaning_report.json"
    _write_daily_audit_csv(audit_path, audit_records)
    pd.DataFrame(cleaning_actions).to_csv(actions_path, index=False)

    _write_text_list(out_dir / "fields.txt", fields)
    _write_text_list(out_dir / "symbols.txt", symbols_requested)

    finished_at = _timestamp_now()
    symbol_metadata = {
        "mode": "source_asset_symbols" if (asset_dir / "symbols.txt").exists() and not getattr(args, "symbols_file", None) else "explicit",
        "symbols_file": str(_resolve_path(args.symbols_file)) if getattr(args, "symbols_file", None) else str(asset_dir / "symbols.txt") if (asset_dir / "symbols.txt").exists() else None,
        "count": len(symbols_requested),
        "source_asset_dir": str(asset_dir),
    }
    source_query = dict((source_manifest or {}).get("query") or {}) if isinstance(source_manifest, Mapping) else {}
    manifest = _build_daily_manifest(
        dataset_name="daily",
        api_name="local_daily_clean_layer",
        output_dir=out_dir,
        fields=fields,
        field_metadata=field_metadata,
        symbol_metadata=symbol_metadata,
        symbols_requested=symbols_requested,
        entries=[entries_by_symbol[symbol] for symbol in symbols_requested if symbol in entries_by_symbol],
        missing_symbols=[symbol for symbol in symbols_requested if symbol not in entries_by_symbol],
        start_date=str(source_query.get("start_date") or ""),
        end_date=str(source_query.get("end_date") or ""),
        frequency=str(source_query.get("frequency") or "1d"),
        adjust_type=source_query.get("adjust_type"),
        skip_suspended=bool(source_query.get("skip_suspended", True)),
        batches=[
            {
                "source_snapshot": asset_dir.name,
                "rows": int(((source_manifest or {}).get("totals") or {}).get("rows") or 0),
                "symbols_written": len(source_files),
                "status": "local_clean_source",
            }
        ],
        columns=schema_columns,
        audit_file=audit_path,
        audit_records=audit_records,
        field_coverage=list(field_coverage.values()),
        started_at=started_at,
        finished_at=finished_at,
        status="completed",
        error=None,
        config_ref=None,
    )

    status_counts = Counter(record.status for record in audit_records)
    report_summary = {
        "symbols_requested": len(symbols_requested),
        "symbols_written": len(entries_by_symbol),
        "symbols_cleaned": int(status_counts.get("cleaned", 0)),
        "symbols_linked_base": int(status_counts.get("linked_base", 0)),
        "symbols_missing_source_asset": int(status_counts.get("missing_source_asset", 0)),
        "symbols_failed": int(status_counts.get("failed", 0)),
        "rows_price_bounds_fixed": int(action_rows_by_type.get("price_bounds_fix", 0)),
        "rows_zero_price_nulled": int(action_rows_by_type.get("zero_price_run_to_null", 0)),
        "rows_partial_nonpositive_price_nulled": int(
            action_rows_by_type.get("partial_nonpositive_price_to_null", 0)
        ),
        "rows_negative_volume_nulled": int(action_rows_by_type.get("volume_to_null", 0)),
        "rows_negative_total_turnover_nulled": int(action_rows_by_type.get("total_turnover_to_null", 0)),
        "rows_etf_short_zero_nulled": int(action_rows_by_type.get("etf_short_zero_price_run_to_null", 0)),
        "zero_price_segments_nulled": int(
            sum(int(item.get("segments_affected") or 0) for item in cleaning_actions if item.get("action") == "zero_price_run_to_null")
        ),
        "etf_short_zero_segments_nulled": int(etf_second_pass_totals.get("short_zero_segments_cleaned", 0)),
        "etf_short_zero_segments_flagged_special": int(
            etf_second_pass_totals.get("short_zero_segments_flagged_special", 0)
        ),
        "etf_short_zero_rows_flagged_special": int(
            etf_second_pass_totals.get("short_zero_rows_flagged_special", 0)
        ),
        "etf_short_zero_segments_flagged_vanilla_over_limit": int(
            etf_second_pass_totals.get("short_zero_segments_flagged_vanilla_over_limit", 0)
        ),
        "etf_short_zero_rows_flagged_vanilla_over_limit": int(
            etf_second_pass_totals.get("short_zero_rows_flagged_vanilla_over_limit", 0)
        ),
        **{key: int(value) for key, value in sorted(remaining_anomaly_totals.items())},
    }
    manifest["totals"].update(report_summary)
    manifest["cleaning"] = {
        "source_asset_dir": str(asset_dir),
        "source_audit_file": str(asset_dir / "audit.csv") if (asset_dir / "audit.csv").exists() else None,
        "rules": {
            "price_bounds_fix": True,
            "negative_volume_to_null": True,
            "negative_total_turnover_to_null": True,
            "zero_price_run_to_null": True,
            "partial_nonpositive_price_to_null": True,
            "zero_price_min_run": int(getattr(args, "zero_price_min_run", 5) or 5),
            "etf_second_pass": bool(etf_metadata_by_symbol),
            "etf_short_zero_max_run": int(getattr(args, "etf_short_zero_max_run", 4) or 4),
            "etf_instruments_file": str(etf_instruments_path) if etf_instruments_path is not None else None,
        },
        "report_file": str(report_path),
        "actions_file": str(actions_path),
        "summary": report_summary,
        "sample_symbols_by_action": dict(sample_symbols_by_action),
    }
    _write_manifest(out_dir / "manifest.yml", manifest)

    report_payload = {
        "source": {
            "asset_dir": str(asset_dir),
            "manifest_file": str(manifest_path) if manifest_path.exists() else None,
            "audit_file": str(asset_dir / "audit.csv") if (asset_dir / "audit.csv").exists() else None,
        },
        "output": {
            "out_dir": str(out_dir),
            "audit_file": str(audit_path),
            "actions_file": str(actions_path),
            "manifest_file": str(out_dir / "manifest.yml"),
        },
        "rules": manifest["cleaning"]["rules"],
        "summary": report_summary,
        "sample_symbols_by_action": dict(sample_symbols_by_action),
        "etf_second_pass": {
            "enabled": bool(etf_metadata_by_symbol),
            "instruments_file": str(etf_instruments_path) if etf_instruments_path is not None else None,
            "product_profile_counts": dict(sorted(etf_product_profile_counts.items())),
            "flag_reason_counts": dict(sorted(etf_flag_reason_counts.items())),
            "sample_flagged_segments": sample_etf_flagged_segments,
        },
    }
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if alias_path is not None:
        _create_relative_symlink(out_dir, alias_path)
    return 0
