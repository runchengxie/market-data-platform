from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd

from market_data_platform.artifacts import RQDATA_ASSETS_DIR as DEFAULT_RQDATA_ASSETS_DIR
from market_data_platform.config_utils import get_research_universe_config, resolve_pipeline_config
from market_data_platform.data_providers import _to_rqdata_symbol
from market_data_platform.symbols import ensure_symbol_columns
from .models import DatedRequestGroup
from .shared import (
    _dedupe_preserve_order,
    _load_symbols_from_by_date,
    _load_text_list,
    _normalize_frame_columns,
    _normalize_hk_symbol,
    _resolve_path,
)


DEFAULT_HK_INSTRUMENTS_FILENAME_PREFIX = "hk_instruments"
DEFAULT_HK_INSTRUMENTS_DIR = DEFAULT_RQDATA_ASSETS_DIR / "hk" / "instruments"


_HK_INSTRUMENTS_FRAME_CACHE: dict[Path, pd.DataFrame] = {}


def _resolve_symbols_from_config(config_ref: str) -> tuple[list[str], dict]:
    resolved = resolve_pipeline_config(config_ref)
    cfg = resolved.data
    universe_cfg = get_research_universe_config(cfg)
    if not isinstance(universe_cfg, Mapping) or not universe_cfg:
        raise SystemExit("Config is missing research_universe settings.")

    symbols: list[str] = []
    raw_symbols = universe_cfg.get("symbols")
    if isinstance(raw_symbols, str):
        symbols.append(raw_symbols)
    elif isinstance(raw_symbols, Sequence):
        symbols.extend(str(item) for item in raw_symbols)

    symbols_file = universe_cfg.get("symbols_file")
    if symbols_file:
        symbols.extend(_load_text_list(symbols_file, label="Symbols file"))

    by_date_file = universe_cfg.get("by_date_file")
    if by_date_file:
        symbols.extend(_load_symbols_from_by_date(by_date_file))

    metadata = {
        "mode": "config_universe",
        "config_ref": str(config_ref),
        "config_source": resolved.source,
        "symbols_file": str(_resolve_path(symbols_file)) if symbols_file else None,
        "by_date_file": str(_resolve_path(by_date_file)) if by_date_file else None,
    }
    return symbols, metadata


def _resolve_symbols(args) -> tuple[list[str], dict]:
    explicit_values = list(getattr(args, "symbol", None) or [])
    symbols_file = getattr(args, "symbols_file", None)
    by_date_file = getattr(args, "by_date_file", None)
    if explicit_values or symbols_file or by_date_file:
        symbols = list(explicit_values)
        if symbols_file:
            symbols.extend(_load_text_list(symbols_file, label="Symbols file"))
        if by_date_file:
            symbols.extend(_load_symbols_from_by_date(by_date_file))
        metadata = {
            "mode": "explicit",
            "symbols_file": str(_resolve_path(symbols_file)) if symbols_file else None,
            "by_date_file": str(_resolve_path(by_date_file)) if by_date_file else None,
        }
    elif getattr(args, "config", None):
        symbols, metadata = _resolve_symbols_from_config(args.config)
    else:
        raise SystemExit(
            "Provide --symbol/--symbols-file/--by-date-file, or pass --config with "
            "research_universe settings."
        )

    normalized = _dedupe_preserve_order(_normalize_hk_symbol(symbol) for symbol in symbols)
    limit = getattr(args, "limit", None)
    if limit is not None:
        if limit <= 0:
            raise SystemExit("--limit must be > 0.")
        normalized = normalized[:limit]
    if not normalized:
        raise SystemExit("No HK symbols resolved for mirroring.")
    metadata["count"] = len(normalized)
    metadata["limit"] = limit
    return normalized, metadata


def _resolve_instrument_symbol_filter(args) -> tuple[list[str] | None, dict]:
    explicit_values = list(getattr(args, "symbol", None) or [])
    symbols_file = getattr(args, "symbols_file", None)
    by_date_file = getattr(args, "by_date_file", None)
    use_config_universe = bool(getattr(args, "use_config_universe", False))
    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        raise SystemExit("--limit must be > 0.")

    if explicit_values or symbols_file or by_date_file:
        symbols = list(explicit_values)
        if symbols_file:
            symbols.extend(_load_text_list(symbols_file, label="Symbols file"))
        if by_date_file:
            symbols.extend(_load_symbols_from_by_date(by_date_file))
        metadata = {
            "mode": "explicit",
            "symbols_file": str(_resolve_path(symbols_file)) if symbols_file else None,
            "by_date_file": str(_resolve_path(by_date_file)) if by_date_file else None,
        }
    elif use_config_universe:
        config_ref = getattr(args, "config", None)
        if not config_ref:
            raise SystemExit("--use-config-universe requires --config.")
        symbols, metadata = _resolve_symbols_from_config(config_ref)
    else:
        return None, {
            "mode": "all_instruments",
            "config_ref": str(getattr(args, "config", None) or "") or None,
            "limit": limit,
        }

    normalized = _dedupe_preserve_order(_normalize_hk_symbol(symbol) for symbol in symbols)
    if limit is not None:
        normalized = normalized[:limit]
    if not normalized:
        raise SystemExit("No HK symbols resolved for instrument export.")
    metadata["count"] = len(normalized)
    metadata["limit"] = limit
    return normalized, metadata


def _default_hk_instruments_out_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        DEFAULT_HK_INSTRUMENTS_DIR / f"{DEFAULT_HK_INSTRUMENTS_FILENAME_PREFIX}_{timestamp}.parquet"
    )


def _candidate_hk_instruments_snapshot_paths(out_root: str | Path) -> list[Path]:
    instruments_dir = _resolve_path(out_root) / "hk" / "instruments"
    if not instruments_dir.exists():
        return []
    paths = [path for path in instruments_dir.glob("*.parquet") if path.is_file()]
    return sorted(
        paths,
        key=lambda path: (
            0 if "all_instruments" in path.name.lower() else 1,
            -path.stat().st_mtime,
            path.name,
        ),
    )


def _load_cached_hk_instruments_frame(path: Path) -> pd.DataFrame:
    cached = _HK_INSTRUMENTS_FRAME_CACHE.get(path)
    if cached is not None:
        return cached.copy()

    frame = pd.read_parquet(path)
    instruments = _normalize_frame_columns(frame.copy())
    instruments = ensure_symbol_columns(instruments, context=f"HK instruments snapshot {path.name}")
    if "symbol" not in instruments.columns or "order_book_id" not in instruments.columns:
        raise ValueError(f"HK instruments snapshot is missing symbol/order_book_id: {path}")

    instruments["symbol"] = instruments["symbol"].map(_normalize_hk_symbol)
    instruments["order_book_id"] = instruments["order_book_id"].astype(str).str.strip()
    if "unique_id" in instruments.columns:
        instruments["unique_id"] = instruments["unique_id"].astype(str).str.strip()
        instruments.loc[instruments["unique_id"] == "", "unique_id"] = pd.NA
    else:
        instruments["unique_id"] = pd.NA
    _HK_INSTRUMENTS_FRAME_CACHE[path] = instruments.copy()
    return instruments


def _build_default_dated_request_groups(
    symbols: Sequence[str],
) -> tuple[list[DatedRequestGroup], dict[str, dict[str, str | None]], dict[str, object]]:
    groups: list[DatedRequestGroup] = []
    metadata: dict[str, dict[str, str | None]] = {}
    for symbol in symbols:
        order_book_id = _to_rqdata_symbol("hk", symbol)
        groups.append(
            DatedRequestGroup(
                symbol=symbol,
                request_ids=(order_book_id,),
                order_book_ids=(order_book_id,),
            )
        )
        metadata[order_book_id] = {
            "symbol": symbol,
            "order_book_id": order_book_id,
            "unique_id": None,
        }
    return groups, metadata, {"mode": "default_order_book_id", "file": None}


def _resolve_hk_dated_request_groups(
    symbols: Sequence[str],
    *,
    start_date: str,
    end_date: str,
    out_root: str,
) -> tuple[list[DatedRequestGroup], dict[str, dict[str, str | None]], dict[str, object]]:
    default_groups, default_metadata, default_info = _build_default_dated_request_groups(symbols)
    snapshot_paths = _candidate_hk_instruments_snapshot_paths(out_root)
    if not snapshot_paths:
        return default_groups, default_metadata, default_info

    start_ts = pd.to_datetime(start_date, errors="coerce")
    end_ts = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        return default_groups, default_metadata, default_info

    for path in snapshot_paths:
        try:
            instruments = _load_cached_hk_instruments_frame(path)
        except Exception:
            continue

        subset = instruments[instruments["symbol"].isin(symbols)].copy()
        if subset.empty:
            continue

        if "listed_date" in subset.columns:
            subset["listed_date_parsed"] = pd.to_datetime(subset["listed_date"], errors="coerce")
        else:
            subset["listed_date_parsed"] = pd.NaT
        if "de_listed_date" in subset.columns:
            delisted_text = subset["de_listed_date"].astype(str).str.strip()
            delisted_text = delisted_text.mask(delisted_text == "0000-00-00")
            subset["de_listed_date_parsed"] = pd.to_datetime(delisted_text, errors="coerce")
        else:
            subset["de_listed_date_parsed"] = pd.NaT

        overlap_mask = (
            subset["listed_date_parsed"].isna() | (subset["listed_date_parsed"] <= end_ts)
        ) & (
            subset["de_listed_date_parsed"].isna() | (subset["de_listed_date_parsed"] >= start_ts)
        )
        overlapping = subset[overlap_mask].copy()
        effective = overlapping if not overlapping.empty else subset

        groups: list[DatedRequestGroup] = []
        metadata: dict[str, dict[str, str | None]] = {}
        for symbol in symbols:
            symbol_rows = effective[effective["symbol"] == symbol].copy()
            if symbol_rows.empty:
                fallback_order_book_id = _to_rqdata_symbol("hk", symbol)
                groups.append(
                    DatedRequestGroup(
                        symbol=symbol,
                        request_ids=(fallback_order_book_id,),
                        order_book_ids=(fallback_order_book_id,),
                    )
                )
                metadata[fallback_order_book_id] = {
                    "symbol": symbol,
                    "order_book_id": fallback_order_book_id,
                    "unique_id": None,
                }
                continue

            symbol_rows = symbol_rows.sort_values(
                ["listed_date_parsed", "order_book_id", "unique_id"],
                kind="mergesort",
            )
            request_ids: list[str] = []
            order_book_ids: list[str] = []
            for row in symbol_rows.itertuples(index=False):
                order_book_id = str(getattr(row, "order_book_id") or "").strip()
                unique_id = str(getattr(row, "unique_id") or "").strip() or None
                request_id = unique_id or order_book_id
                if not request_id:
                    continue
                request_ids.append(request_id)
                order_book_ids.append(order_book_id or request_id)
                metadata[request_id] = {
                    "symbol": symbol,
                    "order_book_id": order_book_id or request_id,
                    "unique_id": unique_id,
                }
                metadata[order_book_id or request_id] = {
                    "symbol": symbol,
                    "order_book_id": order_book_id or request_id,
                    "unique_id": unique_id,
                }

            request_ids = _dedupe_preserve_order(request_ids)
            order_book_ids = _dedupe_preserve_order(order_book_ids)
            if not request_ids:
                fallback_order_book_id = _to_rqdata_symbol("hk", symbol)
                request_ids = [fallback_order_book_id]
                order_book_ids = [fallback_order_book_id]
                metadata[fallback_order_book_id] = {
                    "symbol": symbol,
                    "order_book_id": fallback_order_book_id,
                    "unique_id": None,
                }

            groups.append(
                DatedRequestGroup(
                    symbol=symbol,
                    request_ids=tuple(request_ids),
                    order_book_ids=tuple(order_book_ids),
                )
            )

        return (
            groups,
            metadata,
            {
                "mode": "local_hk_instruments_snapshot",
                "file": str(path),
                "symbols_resolved": len(groups),
            },
        )

    return default_groups, default_metadata, default_info


def _uses_hk_unique_ids(request_ids: Sequence[str]) -> bool:
    for request_id in request_ids:
        prefix = str(request_id or "").strip().split(".", 1)[0]
        if "_" in prefix:
            return True
    return False


def _normalize_hk_dated_payload(
    payload,
    *,
    request_id_metadata: Mapping[str, Mapping[str, str | None]],
) -> pd.DataFrame | pd.Series | None:
    if payload is None:
        return None
    if isinstance(payload, (pd.DataFrame, pd.Series)):
        frame = payload.copy()
    else:
        frame = pd.DataFrame(payload)
    if isinstance(frame, pd.Series):
        return frame
    if frame.empty and len(frame.columns) == 0:
        return frame

    normalized = _normalize_frame_columns(frame)
    if "order_book_id" not in normalized.columns:
        return normalized

    raw_request_ids = normalized["order_book_id"].astype(str).str.strip()
    canonical_order_book_ids = raw_request_ids.map(
        lambda value: (request_id_metadata.get(value) or {}).get("order_book_id")
    )
    unique_ids = raw_request_ids.map(
        lambda value: (request_id_metadata.get(value) or {}).get("unique_id")
    )

    if "unique_id" not in normalized.columns:
        unique_series = unique_ids.where(
            unique_ids.notna(), raw_request_ids.where(raw_request_ids.str.contains("_"))
        )
        if unique_series.notna().any():
            normalized["unique_id"] = unique_series
    else:
        existing_unique_ids = normalized["unique_id"].astype(str).str.strip()
        existing_unique_ids = existing_unique_ids.mask(existing_unique_ids == "")
        normalized["unique_id"] = existing_unique_ids.where(existing_unique_ids.notna(), unique_ids)

    normalized["order_book_id"] = canonical_order_book_ids.where(
        canonical_order_book_ids.notna(),
        raw_request_ids,
    )
    return normalized


def _normalize_hk_valuation_payload(
    payload,
    *,
    request_id_metadata: Mapping[str, Mapping[str, str | None]],
) -> pd.DataFrame | pd.Series | None:
    if payload is None:
        return None
    if isinstance(payload, pd.Series):
        frame = payload.to_frame(name=str(payload.name or "value"))
    elif isinstance(payload, pd.DataFrame):
        frame = payload.copy()
    else:
        frame = pd.DataFrame(payload)
    if frame.empty and len(frame.columns) == 0:
        return frame

    if isinstance(frame.index, pd.MultiIndex):
        raw_request_ids = frame.index.get_level_values(0).map(lambda value: str(value or "").strip())
        order_book_ids = raw_request_ids.map(
            lambda value: (request_id_metadata.get(value) or {}).get("order_book_id") or value
        )
        trade_dates = pd.to_datetime(frame.index.get_level_values(-1), errors="coerce")
        valid_trade_date = pd.Series(trade_dates.notna(), index=frame.index)
        normalized = frame.loc[valid_trade_date.values].copy()
        normalized.index = pd.MultiIndex.from_arrays(
            [
                order_book_ids[valid_trade_date.values].tolist(),
                trade_dates[valid_trade_date.values].strftime("%Y%m%d").tolist(),
            ],
            names=["order_book_id", "trade_date"],
        )
        return normalized

    trade_dates = pd.to_datetime(frame.index, errors="coerce")
    valid_trade_date = pd.Series(trade_dates.notna(), index=frame.index)
    normalized = frame.loc[valid_trade_date.values].copy()
    normalized.index = pd.Index(
        trade_dates[valid_trade_date.values].strftime("%Y%m%d").tolist(),
        name="trade_date",
    )
    return normalized
