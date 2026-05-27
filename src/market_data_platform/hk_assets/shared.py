from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from market_data_platform.data_providers import (
    DEFAULT_RQDATA_HK_FUNDAMENTAL_FIELDS,
)
from ..backup_data import _git_metadata

DEFAULT_PIPELINE_FUNDAMENTALS_NAME = "pipeline_fundamentals.parquet"
DEFAULT_HK_INDUSTRY_LABELS_FILENAME_PREFIX = "industry_labels"
DEFAULT_HK_DAILY_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_turnover",
)
DEFAULT_HK_VALUATION_FIELDS = tuple(DEFAULT_RQDATA_HK_FUNDAMENTAL_FIELDS.values())
DEFAULT_HK_SHARES_FIELDS = (
    "total",
    "circulation_a",
    "management_circulation",
    "non_circulation_a",
    "total_a",
    "total_hk",
    "total_hk1",
)
DEFAULT_HK_EXCHANGE_RATE_FIELDS = (
    "currency_pair",
    "middle_referrence_rate",
)
DATE_TEXT_OUTPUT_COLUMNS = {
    "trade_date",
    "date",
    "ex_date",
    "declaration_announcement_date",
}
DEFAULT_HK_INDUSTRY_SOURCE = "citics_2019"
DEFAULT_HK_INSTRUMENT_INDUSTRY_LEVEL = 0
DEFAULT_HK_INDUSTRY_CHANGE_LEVEL = 1
HK_INSTRUMENT_INDUSTRY_FIELDS = {
    0: (
        "first_industry_code",
        "first_industry_name",
        "second_industry_code",
        "second_industry_name",
        "third_industry_code",
        "third_industry_name",
    ),
    1: ("first_industry_code", "first_industry_name"),
    2: (
        "first_industry_code",
        "first_industry_name",
        "second_industry_code",
        "second_industry_name",
    ),
    3: (
        "first_industry_code",
        "first_industry_name",
        "second_industry_code",
        "second_industry_name",
        "third_industry_code",
        "third_industry_name",
    ),
}
HK_INDUSTRY_HIERARCHY_COLUMNS = (
    "first_industry_code",
    "first_industry_name",
    "second_industry_code",
    "second_industry_name",
    "third_industry_code",
    "third_industry_name",
)
PIT_METADATA_COLUMNS = (
    "quarter",
    "info_date",
    "fiscal_year",
    "standard",
    "if_adjusted",
    "rice_create_tm",
    "order_book_id",
)
STARTER_HK_FINANCIAL_FIELDS = (
    "revenue",
    "operating_revenue",
    "operating_profit",
    "net_profit",
    "basic_earnings_per_share",
    "dividend_per_share",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "cash_and_equivalents",
    "cash_flow_from_operating_activities",
    "inventory",
    "accounts_receivable",
    "accounts_payable",
    "short_term_debt",
    "long_term_loans",
    "goodwill",
)
DERIVED_PIT_FEATURES = {
    "sales",
    "debt",
    "profit_margin",
    "operating_margin",
    "cfo_margin",
    "cfo_to_profit",
    "asset_turnover",
    "roa",
    "leverage",
    "cfo_to_assets",
    "debt_to_assets",
    "debt_to_equity",
    "cash_to_assets",
    "goodwill_to_assets",
    "accrual_ratio",
    "receivables_to_revenue",
    "inventory_to_revenue",
    "working_capital_to_assets",
    "net_debt_to_assets",
    "days_since_report",
    "sales_cagr_3y",
    "eps_cagr_3y",
    "cfo_margin_avg_3y",
    "profit_margin_std_3y",
    "cfo_to_profit_median_3y",
    "positive_cfo_ratio_3y",
    "positive_cfo_ratio_2y",
    "positive_cfo_ratio_3y_min2",
}


def _resolve_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def _normalize_hk_symbol(symbol: object) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    if text.endswith(".XHKG"):
        text = text[:-5]
    if text.endswith(".HK"):
        text = text[:-3]
    if text.isdigit():
        text = text.zfill(5)
    return f"{text}.HK"


def _normalize_field_name(value: object) -> str:
    return str(value or "").strip()


def _dedupe_preserve_order(values: Iterable[str], *, strip: bool = True) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "")
        normalized = text.strip() if strip else text
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized if strip else text)
        seen.add(normalized)
    return deduped


def _normalize_field_list(values: Iterable[object]) -> list[str]:
    return _dedupe_preserve_order(_normalize_field_name(value) for value in values)


def _normalize_frame_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty and len(frame.columns) == 0:
        return frame.copy()
    normalized_names = [_normalize_field_name(column) or str(column) for column in frame.columns]
    if normalized_names == [str(column) for column in frame.columns] and len(set(normalized_names)) == len(
        normalized_names
    ):
        return frame.copy()
    if len(set(normalized_names)) == len(normalized_names):
        normalized = frame.copy()
        normalized.columns = normalized_names
        return normalized

    groups: dict[str, list[pd.Series]] = {}
    order: list[str] = []
    for idx, column_name in enumerate(normalized_names):
        series = frame.iloc[:, idx].copy()
        series.name = column_name
        if column_name not in groups:
            groups[column_name] = [series]
            order.append(column_name)
        else:
            groups[column_name].append(series)

    merged: list[pd.Series] = []
    for column_name in order:
        combined = groups[column_name][0]
        for series in groups[column_name][1:]:
            combined = combined.combine_first(series)
        combined.name = column_name
        merged.append(combined)
    return pd.concat(merged, axis=1) if merged else pd.DataFrame(index=frame.index)


def _drop_conflicting_index_levels(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty and len(frame.columns) == 0:
        return frame.copy()

    column_names = {str(column) for column in frame.columns}
    work = frame.copy()

    if isinstance(work.index, pd.MultiIndex):
        drop_levels = [
            idx
            for idx, name in enumerate(work.index.names)
            if isinstance(name, str) and name and name in column_names
        ]
        if drop_levels:
            keep_levels = [idx for idx in range(work.index.nlevels) if idx not in drop_levels]
            if keep_levels:
                work.index = work.index.droplevel(drop_levels)
            else:
                work.index = pd.RangeIndex(len(work))
        return work

    index_name = work.index.name
    if isinstance(index_name, str) and index_name and index_name in column_names:
        work.index = pd.RangeIndex(len(work))
    return work


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "t"}
    return False


def _load_text_list(path_text: str | Path, *, label: str) -> list[str]:
    path = _resolve_path(path_text)
    if not path.exists():
        raise SystemExit(f"{label} not found: {path}")
    values: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            values.append(text)
    return values


def _resolve_universe_by_date_columns(df: pd.DataFrame) -> tuple[str, str]:
    columns = {str(col).lower(): str(col) for col in df.columns}
    date_col = columns.get("trade_date") or columns.get("date") or columns.get("rebalance_date")
    symbol_col = (
        columns.get("symbol")
        or columns.get("ts_code")
        or columns.get("stock_ticker")
        or columns.get("order_book_id")
    )
    if not date_col or not symbol_col:
        raise SystemExit("Universe-by-date file must include date + symbol columns.")
    return date_col, symbol_col


def _load_symbols_from_by_date(path_text: str | Path) -> list[str]:
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
    df["trade_date"] = parsed
    df = df[df["trade_date"].notna()].copy()
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["symbol"] = df["symbol"].map(_normalize_hk_symbol)
    df = df[df["symbol"] != ""].copy()
    return df["symbol"].drop_duplicates().tolist()


def _load_field_profile(profile_name: str) -> list[str]:
    profile = str(profile_name or "").strip().lower()
    if profile == "starter":
        return list(STARTER_HK_FINANCIAL_FIELDS)
    if profile == "full":
        return _load_hk_financial_fields()
    raise SystemExit(f"Unsupported --field-profile: {profile_name}")


def _resolve_fields(args) -> tuple[list[str], dict]:
    fields: list[str] = []
    field_profiles = [
        str(item).strip().lower()
        for item in (getattr(args, "field_profile", None) or [])
        if str(item).strip()
    ]
    for profile_name in field_profiles:
        fields.extend(_load_field_profile(profile_name))
    if getattr(args, "field", None):
        fields.extend(str(item).strip() for item in args.field if str(item).strip())
    for path_text in getattr(args, "fields_file", None) or []:
        fields.extend(_load_text_list(path_text, label="Fields file"))
    fields = _dedupe_preserve_order(fields, strip=False)
    if not fields:
        raise SystemExit("Provide at least one --field or --fields-file.")
    metadata = {
        "count": len(fields),
        "field_profile": field_profiles,
        "fields_file": [str(_resolve_path(path_text)) for path_text in (args.fields_file or [])],
    }
    return fields, metadata


def _resolve_fields_with_overrides(
    args,
    *,
    load_hk_financial_fields_override=None,
) -> tuple[list[str], dict]:
    if load_hk_financial_fields_override is None:
        return _resolve_fields(args)
    original = _load_hk_financial_fields
    globals()["_load_hk_financial_fields"] = load_hk_financial_fields_override
    try:
        return _resolve_fields(args)
    finally:
        globals()["_load_hk_financial_fields"] = original


def _normalize_absolute_date(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SystemExit(f"{label} is required.")
    normalized = text.replace("/", "-").replace(".", "-")
    parsed = pd.to_datetime(normalized, errors="coerce")
    if pd.isna(parsed):
        raise SystemExit(f"{label} must be a valid absolute date such as 20260310 or 2026-03-10.")
    return parsed.strftime("%Y%m%d")


def _resolve_daily_fields(args) -> tuple[list[str], dict]:
    explicit_fields: list[str] = []
    if getattr(args, "field", None):
        explicit_fields.extend(str(item).strip() for item in args.field if str(item).strip())
    for path_text in getattr(args, "fields_file", None) or []:
        explicit_fields.extend(_load_text_list(path_text, label="Fields file"))
    fields = _dedupe_preserve_order([*DEFAULT_HK_DAILY_FIELDS, *explicit_fields], strip=False)
    metadata = {
        "count": len(fields),
        "base_fields": list(DEFAULT_HK_DAILY_FIELDS),
        "fields_file": [str(_resolve_path(path_text)) for path_text in (args.fields_file or [])],
        "source": "default_plus_explicit" if explicit_fields else "default",
    }
    return fields, metadata


def _resolve_default_plus_explicit_fields(
    args,
    *,
    default_fields: Sequence[str],
    source_label: str,
) -> tuple[list[str], dict]:
    explicit_fields: list[str] = []
    if getattr(args, "field", None):
        explicit_fields.extend(str(item).strip() for item in args.field if str(item).strip())
    for path_text in getattr(args, "fields_file", None) or []:
        explicit_fields.extend(_load_text_list(path_text, label="Fields file"))
    fields = _dedupe_preserve_order([*default_fields, *explicit_fields], strip=False)
    metadata = {
        "count": len(fields),
        "base_fields": list(default_fields),
        "fields_file": [str(_resolve_path(path_text)) for path_text in (args.fields_file or [])],
        "source": source_label if explicit_fields else "default",
    }
    return fields, metadata


def _resolve_optional_explicit_fields(
    args,
    *,
    empty_source_label: str = "api_default",
    explicit_source_label: str = "explicit",
) -> tuple[list[str], dict]:
    fields: list[str] = []
    if getattr(args, "field", None):
        fields.extend(str(item).strip() for item in args.field if str(item).strip())
    for path_text in getattr(args, "fields_file", None) or []:
        fields.extend(_load_text_list(path_text, label="Fields file"))
    fields = _dedupe_preserve_order(fields, strip=False)
    metadata = {
        "count": len(fields),
        "base_fields": [],
        "fields_file": [str(_resolve_path(path_text)) for path_text in (args.fields_file or [])],
        "source": explicit_source_label if fields else empty_source_label,
    }
    return fields, metadata


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _load_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    return None


def _load_hk_financial_fields() -> list[str]:
    try:
        from rqdatac.services.financial import HK_FIELDS_LIST_EX
    except ImportError as exc:
        raise SystemExit(
            "rqdatac with HK financial field metadata is not installed. Install with: pip install '.[rqdata]'"
        ) from exc
    return list(HK_FIELDS_LIST_EX)


def _default_snapshot_name(
    dataset_name: str,
    start_quarter: str,
    end_quarter: str,
    statements: str,
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{dataset_name}_{start_quarter}_{end_quarter}_{statements}_{timestamp}"


def _default_daily_snapshot_name(
    dataset_name: str,
    start_date: str,
    end_date: str,
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{dataset_name}_{start_date}_{end_date}_{timestamp}"


def _timestamp_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(
            timespec="seconds"
        )
    except OSError:
        return None


def _prepare_output_dir(
    *,
    out_root: str,
    dataset_name: str,
    start_quarter: str,
    end_quarter: str,
    statements: str,
    name: str | None,
    resume: bool,
) -> Path:
    root = _resolve_path(out_root)
    snapshot_name = name or _default_snapshot_name(
        dataset_name,
        start_quarter,
        end_quarter,
        statements,
    )
    output_dir = root / "hk" / dataset_name / snapshot_name
    if output_dir.exists():
        if not resume:
            raise SystemExit(f"Refusing to overwrite existing output: {output_dir}")
        if not output_dir.is_dir():
            raise SystemExit(f"Resume target is not a directory: {output_dir}")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def _prepare_daily_output_dir(
    *,
    out_root: str,
    dataset_name: str,
    start_date: str,
    end_date: str,
    name: str | None,
    resume: bool,
) -> Path:
    root = _resolve_path(out_root)
    snapshot_name = name or _default_daily_snapshot_name(
        dataset_name,
        start_date,
        end_date,
    )
    output_dir = root / "hk" / dataset_name / snapshot_name
    if output_dir.exists():
        if not resume:
            raise SystemExit(f"Refusing to overwrite existing output: {output_dir}")
        if not output_dir.is_dir():
            raise SystemExit(f"Resume target is not a directory: {output_dir}")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def _split_daily_range_by_year(
    start_date: str,
    end_date: str,
) -> list[tuple[str, str]]:
    start_ts = pd.to_datetime(start_date, format="%Y%m%d", errors="raise")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d", errors="raise")
    chunks: list[tuple[str, str]] = []
    current = start_ts
    while current <= end_ts:
        year_end = pd.Timestamp(year=current.year, month=12, day=31)
        chunk_end = min(year_end, end_ts)
        chunks.append((current.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        current = chunk_end + pd.Timedelta(days=1)
    return chunks


def _write_text_list(path: Path, values: Sequence[str]) -> None:
    text = "\n".join(values)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _load_existing_text_list(path: Path, *, strip: bool = True) -> list[str]:
    if not path.exists():
        return []
    values: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip() if strip else line.rstrip("\r\n")
            if text:
                values.append(text)
    return values
