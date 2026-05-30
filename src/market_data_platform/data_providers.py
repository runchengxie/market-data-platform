"""RQData-backed data access helpers for the HK research workflow."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from .artifacts import resolve_data_input_path
from .data_provider_contracts import (
    SUPPORTED_MARKETS as SUPPORTED_MARKETS,
)
from .data_provider_contracts import (
    fundamentals_provider_supported as fundamentals_provider_supported,
)
from .data_provider_contracts import (
    normalize_market,
    resolve_provider,
)
from .data_provider_contracts import (
    require_supported_market as _require_supported_market,
)
from .data_provider_contracts import (
    to_rqdata_symbol as _to_rqdata_symbol,
)
from .provider_cache import (
    basic_cache_file,
    cache_tag,
    drop_legacy_symbol_aliases,
    fundamentals_cache_file,
    sanitize_cache_tag,
    write_parquet_cache,
)
from .rqdata_runtime import (
    init_rqdatac as _init_rqdatac_runtime,
)
from .rqdata_runtime import (
    resolve_rqdatac_init_kwargs as _resolve_rqdatac_init_kwargs_runtime,
)
from .symbols import (
    PROVIDER_SYMBOL_PRIORITY,
    ensure_symbol_columns,
    normalize_symbol_for_market,
    normalize_symbol_standard_name,
)

logger = logging.getLogger("market_data_platform.data_providers")

_basic_cache_file = basic_cache_file
_cache_tag = cache_tag
_fundamentals_cache_file = fundamentals_cache_file

FUNDAMENTAL_COLUMN_CANDIDATES = {
    "trade_date": ["trade_date", "date", "trade_dt", "trade_day"],
    "symbol": ["symbol", "ts_code", "ticker", "code", "sec_code", "tscode", "order_book_id"],
}

FUNDAMENTAL_REQUIRED_COLUMNS = ("trade_date", "symbol")

DEFAULT_RQDATA_HK_FUNDAMENTAL_FIELDS = {
    "market_cap": "hk_total_market_val",
    "pe_ttm": "pe_ratio_ttm",
    "pb": "pb_ratio_ttm",
}

DEFAULT_COLUMN_MAPS = {
    "a_share": {
        "trade_date": "trade_date",
        "symbol": "symbol",
        "close": "close",
        "vol": "volume",
        "amount": "total_turnover",
    },
    "hk": {
        "trade_date": "trade_date",
        "symbol": "symbol",
        "close": "close",
        "vol": "vol",
        "amount": "amount",
    },
}

COLUMN_CANDIDATES = {
    "trade_date": ["trade_date", "date", "trade_dt", "trade_day"],
    "symbol": ["symbol", "ts_code", "ticker", "code", "sec_code", "tscode"],
    "close": ["close", "close_price", "adj_close", "close_adj", "cls"],
    "vol": ["vol", "volume", "trade_vol", "volume_traded"],
    "amount": ["amount", "turnover", "total_turnover", "trade_value", "value"],
}

REQUIRED_DAILY_COLUMNS = ("trade_date", "symbol", "close", "vol")
_RQDATA_LISTED_DATE_CACHE: dict[tuple[str, str], pd.Timestamp | None] = {}


def _prepare_rqdata_daily_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.index, pd.MultiIndex):
        date_index = df.index.get_level_values(-1)
    else:
        date_index = df.index
    df = df.reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(date_index).strftime("%Y%m%d")
    df["symbol"] = symbol
    return ensure_symbol_columns(
        df, context="RQData daily frame", priority=PROVIDER_SYMBOL_PRIORITY
    )


def _prepare_rqdata_fundamentals_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if isinstance(df, pd.Series):
        df = df.to_frame(name=str(df.name or "value"))
    df = df.copy()
    if isinstance(df.index, pd.MultiIndex):
        date_index = pd.to_datetime(df.index.get_level_values(-1), errors="coerce")
    else:
        date_index = pd.to_datetime(df.index, errors="coerce")
    df = df.reset_index(drop=True)
    df["trade_date"] = date_index.strftime("%Y%m%d")
    df["symbol"] = symbol
    df = df[df["trade_date"].notna()].copy()
    return ensure_symbol_columns(
        df,
        context="RQData fundamentals frame",
        priority=PROVIDER_SYMBOL_PRIORITY,
    )


def _resolve_rqdatac_init_kwargs(data_cfg: Mapping | None) -> dict[str, object]:
    return _resolve_rqdatac_init_kwargs_runtime(data_cfg)


def _ensure_rqdatac_ready(data_cfg: Mapping | None):
    return _init_rqdatac_runtime(
        data_cfg=data_cfg,
        logger=logger,
        error_cls=RuntimeError,
        import_error_message="rqdatac is required for provider='rqdata'.",
    )


def _rqdata_default_fields(rq_cfg: Mapping | None) -> list[str] | None:
    if isinstance(rq_cfg, Mapping) and "fields" in rq_cfg:
        fields = rq_cfg.get("fields")
        if fields is None or fields == "all" or fields == "*":
            return None
        return list(fields) if isinstance(fields, (list, tuple)) else [str(fields)]
    return ["close", "volume", "total_turnover"]


def _rqdata_skip_suspended(market: str, rq_cfg: Mapping | None) -> bool | None:
    if isinstance(rq_cfg, Mapping) and "skip_suspended" in rq_cfg:
        return bool(rq_cfg.get("skip_suspended"))
    return True if normalize_market(market) == "hk" else None


def _rqdata_listed_date(client, market: str, rq_symbol: str) -> pd.Timestamp | None:
    key = (normalize_market(market), str(rq_symbol))
    if key in _RQDATA_LISTED_DATE_CACHE:
        return _RQDATA_LISTED_DATE_CACHE[key]

    instrument = None
    try:
        instrument = client.instruments(rq_symbol, market=market)
    except TypeError:
        instrument = client.instruments(rq_symbol)
    except Exception:
        instrument = None
    if isinstance(instrument, list):
        instrument = instrument[0] if instrument else None

    listed_date = getattr(instrument, "listed_date", None) or getattr(instrument, "listed_at", None)
    parsed = pd.to_datetime(listed_date, errors="coerce")
    normalized = None if pd.isna(parsed) else parsed.normalize()
    _RQDATA_LISTED_DATE_CACHE[key] = normalized
    return normalized


def _rqdata_fundamental_fields(fundamentals_cfg: Mapping) -> list[str]:
    explicit_fields = fundamentals_cfg.get("fields")
    if explicit_fields is not None:
        if isinstance(explicit_fields, str):
            text = explicit_fields.strip()
            if text and text not in {"all", "*"}:
                return [text]
        else:
            normalized = [str(field).strip() for field in explicit_fields if str(field).strip()]
            if normalized:
                return normalized

    column_map = fundamentals_cfg.get("column_map")
    if isinstance(column_map, Mapping):
        inferred_fields: list[str] = []
        for standard in DEFAULT_RQDATA_HK_FUNDAMENTAL_FIELDS:
            source = column_map.get(standard)
            if source:
                inferred_fields.append(str(source))
        if inferred_fields:
            return inferred_fields

    return list(DEFAULT_RQDATA_HK_FUNDAMENTAL_FIELDS.values())


def _fetch_daily_rqdata(
    market: str,
    symbol: str,
    start_date: str,
    end_date: str,
    client,
    data_cfg: Mapping,
) -> pd.DataFrame:
    if client is None:
        import rqdatac as client
    rq_cfg = data_cfg.get("rqdata") if isinstance(data_cfg, Mapping) else None
    if isinstance(rq_cfg, Mapping) and rq_cfg.get("market"):
        rq_market = normalize_market(rq_cfg.get("market"))
    else:
        rq_market = normalize_market(market)
    frequency = "1d"
    if isinstance(rq_cfg, Mapping) and rq_cfg.get("frequency"):
        frequency = str(rq_cfg.get("frequency"))
    fields = _rqdata_default_fields(rq_cfg)
    skip_suspended = _rqdata_skip_suspended(rq_market, rq_cfg)

    kwargs = {}
    if fields is not None:
        kwargs["fields"] = fields
    if isinstance(rq_cfg, Mapping) and "adjust_type" in rq_cfg:
        kwargs["adjust_type"] = rq_cfg.get("adjust_type")
    if skip_suspended is not None:
        kwargs["skip_suspended"] = skip_suspended
    kwargs["market"] = rq_market

    rq_symbol = _to_rqdata_symbol(rq_market, symbol)
    effective_start = str(start_date).strip()
    effective_end = str(end_date).strip()
    if skip_suspended:
        listed_date = _rqdata_listed_date(client, rq_market, rq_symbol)
        start_ts = pd.to_datetime(effective_start, errors="coerce")
        end_ts = pd.to_datetime(effective_end, errors="coerce")
        if listed_date is not None and not pd.isna(start_ts) and not pd.isna(end_ts):
            if listed_date > end_ts.normalize():
                return pd.DataFrame()
            if listed_date > start_ts.normalize():
                effective_start = listed_date.strftime("%Y%m%d")

    df = client.get_price(rq_symbol, effective_start, effective_end, frequency, **kwargs)
    if df is None or df.empty:
        return df
    return _prepare_rqdata_daily_frame(df, symbol)


def _normalize_trade_date_series(series: pd.Series) -> pd.Series:
    if series.empty:
        return series.astype(str)
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.strftime("%Y%m%d")
    parsed = pd.to_datetime(series.astype(str), errors="coerce")
    return parsed.dt.strftime("%Y%m%d")


def _ensure_trade_date_str(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "trade_date" not in df.columns:
        return df
    df = df.copy()
    df["trade_date"] = _normalize_trade_date_series(df["trade_date"])
    return df[df["trade_date"].notna()].copy()


def _is_small_leading_calendar_gap(
    start_date: str,
    cached_min: str,
    *,
    max_gap_days: int = 7,
) -> bool:
    """Treat tiny left-edge gaps as likely non-trading days."""
    try:
        start_ts = pd.to_datetime(str(start_date), format="%Y%m%d", errors="raise")
        cached_min_ts = pd.to_datetime(str(cached_min), format="%Y%m%d", errors="raise")
    except Exception:
        return False
    gap_days = int((cached_min_ts.normalize() - start_ts.normalize()).days)
    return 0 < gap_days <= int(max_gap_days)


def _load_basic_rqdata(
    market: str,
    symbols: Iterable[str] | None,
    client,
    data_cfg: Mapping,
) -> pd.DataFrame:
    local_basic = _load_basic_from_local_asset(market, symbols, data_cfg)
    if local_basic is not None:
        return local_basic
    if client is None:
        import rqdatac as client
    rq_cfg = data_cfg.get("rqdata") if isinstance(data_cfg, Mapping) else None
    if isinstance(rq_cfg, Mapping) and rq_cfg.get("market"):
        rq_market = normalize_market(rq_cfg.get("market"))
    else:
        rq_market = normalize_market(market)

    symbol_map = {}
    order_book_ids = None
    if symbols:
        order_book_ids = []
        for sym in symbols:
            rq_sym = _to_rqdata_symbol(rq_market, sym)
            order_book_ids.append(rq_sym)
            symbol_map[rq_sym] = sym

    if order_book_ids:
        instruments = client.instruments(order_book_ids, market=rq_market)
        if not isinstance(instruments, list):
            instruments = [instruments]
        rows = []
        for ins in instruments:
            if ins is None:
                continue
            order_book_id = getattr(ins, "order_book_id", None)
            rows.append(
                {
                    "symbol": symbol_map.get(order_book_id, order_book_id),
                    "name": getattr(ins, "symbol", None),
                    "list_date": getattr(ins, "listed_date", None),
                }
            )
        df_basic = pd.DataFrame(rows)
        if "list_date" in df_basic.columns:
            df_basic["list_date"] = pd.to_datetime(
                df_basic["list_date"], errors="coerce"
            ).dt.strftime("%Y%m%d")
        return drop_legacy_symbol_aliases(
            ensure_symbol_columns(
                df_basic,
                context="RQData basic data",
                priority=PROVIDER_SYMBOL_PRIORITY,
            )
        )

    df_basic = client.all_instruments("CS", market=rq_market)
    if df_basic is None or df_basic.empty:
        return df_basic
    df_basic = df_basic.copy()
    if "order_book_id" in df_basic.columns:
        df_basic["symbol"] = df_basic["order_book_id"]
    if "listed_date" in df_basic.columns:
        df_basic["list_date"] = df_basic["listed_date"]
    if "symbol" in df_basic.columns and "name" not in df_basic.columns:
        df_basic["name"] = df_basic["symbol"]
    df_basic = ensure_symbol_columns(
        df_basic,
        context="RQData all instruments",
        priority=PROVIDER_SYMBOL_PRIORITY,
    )
    df_basic = drop_legacy_symbol_aliases(df_basic)
    df_basic = df_basic[["symbol", "name", "list_date"]].copy()
    df_basic["symbol"] = df_basic["symbol"].map(
        lambda value: normalize_symbol_for_market(value, market=market)
    )
    if "list_date" in df_basic.columns:
        df_basic["list_date"] = pd.to_datetime(df_basic["list_date"], errors="coerce").dt.strftime(
            "%Y%m%d"
        )
    return df_basic


def _merge_column_map(market: str, data_cfg: Mapping) -> dict[str, str]:
    merged = dict(DEFAULT_COLUMN_MAPS.get(market, {}))
    cfg_map = data_cfg.get("column_map") if isinstance(data_cfg, Mapping) else None
    if isinstance(cfg_map, Mapping):
        for key, value in cfg_map.items():
            if value:
                merged[normalize_symbol_standard_name(key)] = str(value)
    return merged


def _apply_column_map(df: pd.DataFrame, column_map: Mapping[str, str]) -> pd.DataFrame:
    rename_map = {}
    for standard, source in column_map.items():
        normalized_standard = normalize_symbol_standard_name(standard)
        if (
            source in df.columns
            and normalized_standard != source
            and normalized_standard not in df.columns
        ):
            rename_map[source] = normalized_standard
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def _infer_missing_columns(df: pd.DataFrame) -> pd.DataFrame:
    for standard, candidates in COLUMN_CANDIDATES.items():
        if standard in df.columns:
            continue
        for candidate in candidates:
            if candidate in df.columns:
                df = df.rename(columns={candidate: standard})
                break
    return df


def _infer_fundamental_columns(df: pd.DataFrame) -> pd.DataFrame:
    for standard, candidates in FUNDAMENTAL_COLUMN_CANDIDATES.items():
        if standard in df.columns:
            continue
        for candidate in candidates:
            if candidate in df.columns:
                df = df.rename(columns={candidate: standard})
                break
    return df


def _force_symbol_value(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    out["symbol"] = str(symbol).strip()
    return out


def _standardize_fundamentals_frame(
    df: pd.DataFrame,
    column_map: Mapping[str, str],
    symbol: str,
) -> pd.DataFrame:
    df = _apply_column_map(df, column_map)
    df = _infer_fundamental_columns(df)
    if "symbol" not in df.columns:
        df = df.copy()
        df["symbol"] = symbol
    df = ensure_symbol_columns(
        df,
        context="Fundamentals data",
        priority=PROVIDER_SYMBOL_PRIORITY,
    )
    df = _force_symbol_value(df, symbol)
    df = drop_legacy_symbol_aliases(df)
    missing = [col for col in FUNDAMENTAL_REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Fundamentals data missing required columns: {missing}")
    return df


def _standardize_daily_frame(
    df: pd.DataFrame,
    market: str,
    data_cfg: Mapping,
    symbol: str,
) -> pd.DataFrame:
    df = _apply_column_map(df, _merge_column_map(market, data_cfg))
    df = _infer_missing_columns(df)
    if "symbol" not in df.columns:
        df = df.copy()
        df["symbol"] = symbol
    df = ensure_symbol_columns(
        df,
        context="Daily data",
        priority=PROVIDER_SYMBOL_PRIORITY,
    )
    df = _force_symbol_value(df, symbol)
    missing = [col for col in REQUIRED_DAILY_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Daily data missing required columns: {missing}")
    return drop_legacy_symbol_aliases(df)


def _resolve_local_path(path_text: object, *, label: str) -> Path | None:
    if path_text in {None, ""}:
        return None
    path = resolve_data_input_path(str(path_text))
    if not path.exists():
        raise SystemExit(f"{label} not found: {path}")
    return path


def _provider_local_cfg(data_cfg: Mapping | None, provider: str | None = None) -> Mapping | None:
    if not isinstance(data_cfg, Mapping):
        return None
    selected = str(provider or resolve_provider(data_cfg) or "").strip().lower()
    provider_cfg = data_cfg.get(selected) if selected else None
    return provider_cfg if isinstance(provider_cfg, Mapping) else None


def _resolve_local_daily_asset_dir(data_cfg: Mapping | None) -> Path | None:
    if not isinstance(data_cfg, Mapping):
        return None
    provider = resolve_provider(data_cfg)
    provider_cfg = _provider_local_cfg(data_cfg, provider)
    candidates = []
    if isinstance(provider_cfg, Mapping):
        candidates.extend([provider_cfg.get("daily_asset_dir"), provider_cfg.get("asset_dir")])
    # Backward-compatible RQData fallback for older configs/tests.
    rq_cfg = data_cfg.get("rqdata")
    if provider == "rqdata" and isinstance(rq_cfg, Mapping) and rq_cfg is not provider_cfg:
        candidates.extend([rq_cfg.get("daily_asset_dir"), rq_cfg.get("asset_dir")])
    candidates.extend([data_cfg.get("daily_asset_dir"), data_cfg.get("asset_dir")])
    label = f"Local {str(provider or 'provider').upper()} daily asset path"
    for candidate in candidates:
        root = (
            _resolve_local_path(candidate, label=label)
            if candidate
            else None
        )
        if root is None:
            continue
        if (root / "data").exists():
            return root
        if root.name == "data" and root.is_dir():
            return root.parent
        raise SystemExit(f"{label} is missing data/: {root}")
    return None


def _resolve_local_instruments_file(data_cfg: Mapping | None) -> Path | None:
    if not isinstance(data_cfg, Mapping):
        return None
    provider = resolve_provider(data_cfg)
    provider_cfg = _provider_local_cfg(data_cfg, provider)
    candidates = []
    if isinstance(provider_cfg, Mapping):
        candidates.extend([provider_cfg.get("instruments_file"), provider_cfg.get("basic_file")])
    # Backward-compatible RQData fallback for older configs/tests.
    rq_cfg = data_cfg.get("rqdata")
    if provider == "rqdata" and isinstance(rq_cfg, Mapping) and rq_cfg is not provider_cfg:
        candidates.extend([rq_cfg.get("instruments_file"), rq_cfg.get("basic_file")])
    candidates.extend([data_cfg.get("instruments_file"), data_cfg.get("basic_file")])
    label = f"Local {str(provider or 'provider').upper()} instruments file"
    for candidate in candidates:
        resolved = (
            _resolve_local_path(candidate, label=label)
            if candidate
            else None
        )
        if resolved is not None:
            return resolved
    return None


def _resolve_local_reference_asset_dir(
    data_cfg: Mapping | None,
    dataset_name: str,
) -> Path | None:
    if not isinstance(data_cfg, Mapping):
        return None
    rq_cfg = data_cfg.get("rqdata")
    key = f"{str(dataset_name).strip()}_dir"
    candidates = []
    if isinstance(rq_cfg, Mapping):
        candidates.append(rq_cfg.get(key))
    candidates.append(data_cfg.get(key))
    for candidate in candidates:
        root = (
            _resolve_local_path(
                candidate,
                label=f"Local RQData {dataset_name} asset path",
            )
            if candidate
            else None
        )
        if root is None:
            continue
        if (root / "data").exists():
            return root
        if root.name == "data" and root.is_dir():
            return root.parent
        raise SystemExit(f"Local RQData {dataset_name} asset directory is missing data/: {root}")
    return None


def has_local_rqdata_assets(data_cfg: Mapping | None) -> bool:
    return (
        _resolve_local_daily_asset_dir(data_cfg) is not None
        and _resolve_local_instruments_file(data_cfg) is not None
    )


def _read_local_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise SystemExit(f"Unsupported local asset file type: {path}")


def _normalize_reference_date_frame(
    frame: pd.DataFrame,
    *,
    date_col: str,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=[date_col])
    work = frame.copy()
    if date_col not in work.columns:
        if isinstance(work.index, pd.MultiIndex) and date_col in work.index.names:
            work = work.reset_index()
        elif work.index.name == date_col:
            work = work.reset_index()
    if date_col not in work.columns:
        return pd.DataFrame(columns=[date_col])
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    return work[work[date_col].notna()].copy()


def _load_local_ex_factors_frame(symbol: str, data_cfg: Mapping | None) -> pd.DataFrame | None:
    asset_dir = _resolve_local_reference_asset_dir(data_cfg, "ex_factors")
    if asset_dir is None:
        return None
    asset_path = asset_dir / "data" / f"{symbol}.parquet"
    if not asset_path.exists():
        return pd.DataFrame(columns=["ex_date", "ex_cum_factor"])
    frame = _normalize_reference_date_frame(pd.read_parquet(asset_path), date_col="ex_date")
    if frame.empty:
        return pd.DataFrame(columns=["ex_date", "ex_cum_factor"])
    if "ex_cum_factor" not in frame.columns:
        if "ex_factor" not in frame.columns:
            return pd.DataFrame(columns=["ex_date", "ex_cum_factor"])
        frame["ex_cum_factor"] = pd.to_numeric(frame["ex_factor"], errors="coerce").cumprod()
    frame["ex_cum_factor"] = pd.to_numeric(frame["ex_cum_factor"], errors="coerce")
    frame = frame[
        frame["ex_cum_factor"].notna()
        & np.isfinite(frame["ex_cum_factor"])
        & (frame["ex_cum_factor"] > 0)
    ][["ex_date", "ex_cum_factor"]].copy()
    if frame.empty:
        return pd.DataFrame(columns=["ex_date", "ex_cum_factor"])
    frame = frame.sort_values("ex_date").drop_duplicates(subset=["ex_date"], keep="last")
    return frame.reset_index(drop=True)


def _rqdata_adjust_type(data_cfg: Mapping | None) -> str | None:
    if not isinstance(data_cfg, Mapping):
        return None
    rq_cfg = data_cfg.get("rqdata")
    if not isinstance(rq_cfg, Mapping) or "adjust_type" not in rq_cfg:
        return None
    value = str(rq_cfg.get("adjust_type") or "").strip().lower()
    return value or None


def _input_tr_close_series(
    frame: pd.DataFrame,
    *,
    trade_dates: pd.Series,
) -> pd.Series | None:
    if "tr_close" not in frame.columns:
        return None
    tr_close = pd.to_numeric(frame["tr_close"], errors="coerce")
    tr_close = tr_close.where(trade_dates.notna())
    tr_close.name = "tr_close"
    return tr_close


def _build_tr_close_payload(
    frame: pd.DataFrame,
    *,
    symbol: str,
    data_cfg: Mapping | None,
) -> tuple[pd.Series | None, dict[str, object] | None]:
    if (
        frame is None
        or frame.empty
        or "close" not in frame.columns
        or "trade_date" not in frame.columns
    ):
        return None, None
    close = pd.to_numeric(frame["close"], errors="coerce")
    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    input_tr_close = _input_tr_close_series(frame, trade_dates=trade_dates)

    ex_factors = _load_local_ex_factors_frame(symbol, data_cfg)
    if ex_factors is not None:
        if ex_factors.empty:
            if input_tr_close is not None:
                return input_tr_close, {
                    "source": "input_frame_missing_ex_factors",
                    "configured_local_ex_factors": True,
                    "local_ex_factors_available": False,
                    "adjust_type": _rqdata_adjust_type(data_cfg),
                }
            return close.rename("tr_close"), {
                "source": "close_fallback_missing_ex_factors",
                "configured_local_ex_factors": True,
                "local_ex_factors_available": False,
                "adjust_type": _rqdata_adjust_type(data_cfg),
            }
        ex_dates = ex_factors["ex_date"].to_numpy(dtype="datetime64[ns]")
        trade_values = trade_dates.to_numpy(dtype="datetime64[ns]")
        ex_cum_values = ex_factors["ex_cum_factor"].to_numpy(dtype=float)
        idx = np.searchsorted(ex_dates, trade_values, side="right") - 1
        period_cum = np.ones(len(frame), dtype=float)
        valid_idx = idx >= 0
        period_cum[valid_idx] = ex_cum_values[idx[valid_idx]]
        tr_close = close * pd.Series(period_cum, index=frame.index, dtype=float)
        tr_close = tr_close.where(trade_dates.notna())
        tr_close.name = "tr_close"
        return tr_close, {
            "source": "local_ex_factors",
            "configured_local_ex_factors": True,
            "local_ex_factors_available": True,
            "adjust_type": _rqdata_adjust_type(data_cfg),
        }

    adjust_type = _rqdata_adjust_type(data_cfg)
    if adjust_type in {"pre", "post", "pre_volume", "post_volume"}:
        return close.rename("tr_close"), {
            "source": "provider_adjusted_price",
            "configured_local_ex_factors": False,
            "local_ex_factors_available": None,
            "adjust_type": adjust_type,
        }
    if input_tr_close is not None:
        return input_tr_close, {
            "source": "input_frame",
            "configured_local_ex_factors": False,
            "local_ex_factors_available": None,
            "adjust_type": adjust_type,
        }
    return None, {
        "source": "unavailable",
        "configured_local_ex_factors": False,
        "local_ex_factors_available": None,
        "adjust_type": adjust_type,
    }


def _augment_daily_frame(
    df: pd.DataFrame,
    *,
    market: str,
    symbol: str,
    data_cfg: Mapping | None,
) -> tuple[pd.DataFrame, bool]:
    if df is None or df.empty:
        return df, False
    out = df.copy()
    changed = False

    tr_close, tr_close_meta = _build_tr_close_payload(out, symbol=symbol, data_cfg=data_cfg)
    if tr_close is not None:
        existing = out["tr_close"].copy() if "tr_close" in out.columns else None
        if existing is None or not existing.equals(tr_close):
            out["tr_close"] = tr_close
            changed = True
    if tr_close_meta is not None:
        out.attrs["tr_close_meta"] = {"symbol": symbol, **tr_close_meta}

    return out, changed


def _load_daily_from_local_asset(
    market: str,
    symbol: str,
    start_date: str,
    end_date: str,
    data_cfg: Mapping,
) -> pd.DataFrame | None:
    asset_dir = _resolve_local_daily_asset_dir(data_cfg)
    if asset_dir is None:
        return None
    asset_path = asset_dir / "data" / f"{symbol}.parquet"
    if not asset_path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(asset_path)
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = _standardize_daily_frame(frame, market, data_cfg, symbol)
    frame = _ensure_trade_date_str(frame)
    if frame is None or frame.empty:
        return pd.DataFrame()
    mask = (frame["trade_date"] >= str(start_date)) & (frame["trade_date"] <= str(end_date))
    return frame.loc[mask].copy()


def _load_basic_from_local_asset(
    market: str,
    symbols: Iterable[str] | None,
    data_cfg: Mapping,
) -> pd.DataFrame | None:
    instruments_file = _resolve_local_instruments_file(data_cfg)
    if instruments_file is None:
        return None
    work = _read_local_table(instruments_file)
    if work is None or work.empty:
        return pd.DataFrame()
    work = work.copy()
    if "name" not in work.columns:
        for candidate in ("symbol", "eng_symbol", "abbrev_symbol", "order_book_id", "ts_code"):
            if candidate in work.columns:
                work["name"] = work[candidate]
                break
    if (
        "order_book_id" in work.columns
        and "symbol" not in work.columns
        and "ts_code" not in work.columns
    ):
        work["symbol"] = work["order_book_id"]
    if "listed_date" in work.columns and "list_date" not in work.columns:
        work["list_date"] = work["listed_date"]
    work = ensure_symbol_columns(
        work,
        context="Local RQData instruments file",
        priority=PROVIDER_SYMBOL_PRIORITY,
    )
    work = drop_legacy_symbol_aliases(work)
    required = ["symbol", "name", "list_date"]
    missing = [column for column in required if column not in work.columns]
    if missing:
        raise SystemExit(
            "Local RQData instruments file is missing required columns "
            f"{missing}: {instruments_file}"
        )
    work = work[["symbol", "name", "list_date"]].copy()
    work["symbol"] = work["symbol"].map(
        lambda value: normalize_symbol_for_market(value, market=market)
    )
    work["list_date"] = pd.to_datetime(work["list_date"], errors="coerce").dt.strftime("%Y%m%d")
    if symbols:
        work = work[work["symbol"].isin(list(symbols))].copy()
    return work.reset_index(drop=True)


def _fetch_daily_from_provider(
    provider: str,
    market: str,
    symbol: str,
    start_date: str,
    end_date: str,
    client,
    data_cfg: Mapping,
) -> pd.DataFrame:
    local_frame = _load_daily_from_local_asset(market, symbol, start_date, end_date, data_cfg)
    if local_frame is not None:
        return local_frame
    if provider != "rqdata":
        raise ValueError(
            f"Unsupported online data provider '{provider}'. "
            "Configure provider-local platform assets (for example data.tushare.daily_asset_dir) "
            "or use provider='rqdata' for online reads."
        )
    df = _fetch_daily_rqdata(market, symbol, start_date, end_date, client, data_cfg)
    if df is None or df.empty:
        return df
    return _standardize_daily_frame(df, market, data_cfg, symbol)


def fetch_daily(
    market: str,
    symbol: str,
    start_date: str,
    end_date: str,
    cache_dir: Path,
    client,
    data_cfg: Mapping | None = None,
) -> pd.DataFrame:
    market = _require_supported_market(market)
    data_cfg = data_cfg or {}
    provider = resolve_provider(data_cfg)
    start_date = str(start_date).strip()
    end_date = str(end_date).strip()
    tag = cache_tag(data_cfg)
    prefix = f"{market}_{provider}"
    if tag:
        prefix = f"{prefix}_{tag}"
    cache_mode = (
        str(data_cfg.get("daily_cache_mode", data_cfg.get("cache_mode", "symbol"))).strip().lower()
    )
    if cache_mode in {"range", "window"}:
        cache_file = cache_dir / f"{prefix}_daily_{symbol}_{start_date}_{end_date}.parquet"
        if cache_file.exists():
            cached = pd.read_parquet(cache_file)
            if cached is None or cached.empty:
                return drop_legacy_symbol_aliases(cached)
            cached = ensure_symbol_columns(
                cached,
                context="Cached daily data",
                priority=PROVIDER_SYMBOL_PRIORITY,
            )
            cached = _force_symbol_value(cached, symbol)
            cached, cache_changed = _augment_daily_frame(
                cached,
                market=market,
                symbol=symbol,
                data_cfg=data_cfg,
            )
            if cache_changed:
                cached = cached.copy(deep=True)
                write_parquet_cache(cached, cache_file)
            return drop_legacy_symbol_aliases(cached)
        df = _fetch_daily_from_provider(
            provider, market, symbol, start_date, end_date, client, data_cfg
        )
        if df is None or df.empty:
            return df
        df, _ = _augment_daily_frame(
            df,
            market=market,
            symbol=symbol,
            data_cfg=data_cfg,
        )
        # Ensure buffers are writable before parquet serialization.
        df = df.copy(deep=True)
        write_parquet_cache(df, cache_file)
        return df

    cache_file = cache_dir / f"{prefix}_daily_{symbol}.parquet"
    cached = None
    trade_dates = []
    if cache_file.exists():
        cached = pd.read_parquet(cache_file)
        cached = _ensure_trade_date_str(cached)
        if cached is not None and not cached.empty:
            cached = ensure_symbol_columns(
                cached,
                context="Cached daily data",
                priority=PROVIDER_SYMBOL_PRIORITY,
            )
            cached = _force_symbol_value(cached, symbol)
        if cached is not None and not cached.empty and "trade_date" in cached.columns:
            trade_dates = sorted(cached["trade_date"].unique().tolist())
            if not trade_dates:
                cached = None

    refresh_days = int(data_cfg.get("cache_refresh_days", 0) or 0)
    refresh_days = max(0, refresh_days)
    refresh_on_hit = bool(data_cfg.get("cache_refresh_on_hit", False))

    fetch_ranges: list[tuple[str, str]] = []
    if cached is None or cached.empty or not trade_dates:
        fetch_ranges.append((start_date, end_date))
    else:
        cached_min, cached_max = trade_dates[0], trade_dates[-1]
        if start_date < cached_min and not _is_small_leading_calendar_gap(start_date, cached_min):
            left_end = min(end_date, cached_min)
            if start_date <= left_end:
                fetch_ranges.append((start_date, left_end))
        if end_date > cached_max:
            refresh_start = cached_max
            if refresh_days > 0:
                idx = max(0, len(trade_dates) - refresh_days)
                refresh_start = trade_dates[idx]
            if refresh_start < start_date:
                refresh_start = start_date
            fetch_ranges.append((refresh_start, end_date))
        elif refresh_on_hit and refresh_days > 0 and end_date >= cached_min:
            idx = max(0, len(trade_dates) - refresh_days)
            refresh_start = trade_dates[idx]
            if refresh_start < start_date:
                refresh_start = start_date
            if refresh_start <= end_date:
                fetch_ranges.append((refresh_start, end_date))

    new_frames: list[pd.DataFrame] = []
    for fetch_start, fetch_end in fetch_ranges:
        if fetch_start > fetch_end:
            continue
        df_new = _fetch_daily_from_provider(
            provider, market, symbol, fetch_start, fetch_end, client, data_cfg
        )
        if df_new is None or df_new.empty:
            continue
        df_new = _ensure_trade_date_str(df_new)
        if df_new is not None and not df_new.empty:
            new_frames.append(df_new)

    if cached is None or cached.empty:
        if not new_frames:
            return pd.DataFrame()
        merged = pd.concat(new_frames, ignore_index=True) if len(new_frames) > 1 else new_frames[0]
        updated = True
    else:
        if new_frames:
            merged = pd.concat([cached] + new_frames, ignore_index=True)
            updated = True
        else:
            merged = cached
            updated = False

    merged = _ensure_trade_date_str(merged)
    if merged is None or merged.empty:
        return pd.DataFrame()
    merged = ensure_symbol_columns(
        merged,
        context="Daily data",
        priority=PROVIDER_SYMBOL_PRIORITY,
    )
    merged = _force_symbol_value(merged, symbol)
    merged, augment_changed = _augment_daily_frame(
        merged,
        market=market,
        symbol=symbol,
        data_cfg=data_cfg,
    )

    if updated or augment_changed:
        merged = merged.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
        merged.sort_values(["symbol", "trade_date"], inplace=True)
        # Ensure buffers are writable before parquet serialization.
        merged = merged.copy(deep=True)
        write_parquet_cache(merged, cache_file)

    mask = (merged["trade_date"] >= start_date) & (merged["trade_date"] <= end_date)
    return drop_legacy_symbol_aliases(merged.loc[mask].copy())


def load_basic(
    market: str,
    cache_dir: Path,
    client,
    data_cfg: Mapping | None = None,
    symbols: Iterable[str] | None = None,
) -> pd.DataFrame:
    market = _require_supported_market(market)
    data_cfg = data_cfg or {}
    provider = resolve_provider(data_cfg)
    tag = cache_tag(data_cfg)
    cache_file = basic_cache_file(cache_dir, market, provider, symbols, tag=tag)
    if cache_file.exists():
        cached = pd.read_parquet(cache_file)
        if cached is None or cached.empty:
            return cached
        return drop_legacy_symbol_aliases(
            ensure_symbol_columns(
                cached,
                context="Cached basic data",
                priority=PROVIDER_SYMBOL_PRIORITY,
            )
        )

    local_basic = _load_basic_from_local_asset(market, symbols, data_cfg)
    if local_basic is not None:
        if local_basic is None or local_basic.empty:
            return local_basic
        # Ensure buffers are writable before parquet serialization.
        local_basic = local_basic.copy(deep=True)
        write_parquet_cache(local_basic, cache_file)
        return local_basic

    if provider != "rqdata":
        raise ValueError(
            f"Unsupported online data provider '{provider}'. "
            "Configure provider-local platform assets (for example data.tushare.instruments_file) "
            "or use provider='rqdata' for online reads."
        )
    df_basic = _load_basic_rqdata(market, symbols, client, data_cfg)

    if df_basic is None or df_basic.empty:
        return df_basic
    df_basic = drop_legacy_symbol_aliases(
        ensure_symbol_columns(
            df_basic,
            context="Basic data",
            priority=PROVIDER_SYMBOL_PRIORITY,
        )
    )

    if symbols:
        if "symbol" in df_basic.columns:
            df_basic = df_basic[df_basic["symbol"].isin(list(symbols))].copy()

    # Ensure buffers are writable before parquet serialization.
    df_basic = df_basic.copy(deep=True)
    write_parquet_cache(df_basic, cache_file)
    return df_basic


def fetch_fundamentals(
    market: str,
    symbol: str,
    start_date: str,
    end_date: str,
    cache_dir: Path,
    client,
    data_cfg: Mapping | None = None,
    fundamentals_cfg: Mapping | None = None,
) -> pd.DataFrame:
    market = _require_supported_market(market)
    data_cfg = data_cfg or {}
    fundamentals_cfg = fundamentals_cfg or {}
    provider = (
        resolve_provider({"provider": fundamentals_cfg.get("provider")})
        if fundamentals_cfg.get("provider")
        else resolve_provider(data_cfg)
    )
    tag = sanitize_cache_tag(
        fundamentals_cfg.get("cache_tag")
        or fundamentals_cfg.get("cache_version")
        or cache_tag(data_cfg)
    )
    cache_file = fundamentals_cache_file(
        cache_dir,
        market,
        provider,
        symbol,
        start_date,
        end_date,
        tag,
        fundamentals_cfg,
    )
    if cache_file.exists():
        cached = pd.read_parquet(cache_file)
        if cached is None or cached.empty:
            return cached
        cached = ensure_symbol_columns(
            cached,
            context="Cached fundamentals data",
            priority=PROVIDER_SYMBOL_PRIORITY,
        )
        cached = _force_symbol_value(cached, symbol)
        return drop_legacy_symbol_aliases(cached)

    if provider != "rqdata":
        raise ValueError(
            "Fundamentals provider not supported. "
            "Use fundamentals.source=file or provider='rqdata'."
        )
    if market != "hk":
        raise ValueError("RQData fundamentals provider currently supports only market='hk'.")
    if client is None:
        try:
            import rqdatac as client
        except ImportError as exc:
            raise RuntimeError(f"rqdatac is required for provider='rqdata' ({exc}).") from exc

    endpoint_name = str(fundamentals_cfg.get("endpoint") or "get_factor").strip()
    if endpoint_name != "get_factor":
        raise ValueError(
            "RQData fundamentals provider currently supports only endpoint='get_factor'."
        )
    rq_cfg = data_cfg.get("rqdata") if isinstance(data_cfg, Mapping) else None
    rq_market = (
        normalize_market(rq_cfg.get("market"))
        if isinstance(rq_cfg, Mapping) and rq_cfg.get("market")
        else market
    )
    factor_fields = _rqdata_fundamental_fields(fundamentals_cfg)
    params = dict(fundamentals_cfg.get("params") or {})
    params["market"] = rq_market
    rq_symbol = _to_rqdata_symbol(rq_market, symbol)
    try:
        df = client.get_factor(rq_symbol, factor_fields, start_date, end_date, **params)
    except Exception as exc:
        if "not initialized" not in str(exc).lower():
            raise
        client = _ensure_rqdatac_ready(data_cfg)
        df = client.get_factor(rq_symbol, factor_fields, start_date, end_date, **params)
    if df is None or df.empty:
        return df

    column_map = fundamentals_cfg.get("column_map") or {}
    df = _prepare_rqdata_fundamentals_frame(df, symbol)
    df = _standardize_fundamentals_frame(df, column_map, symbol)
    df = df.copy(deep=True)
    write_parquet_cache(df, cache_file)
    return df
