from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd

from .tushare_a_share import _normalize_ts_code, _write_manifest

PRICE_COLUMNS = ("open", "high", "low", "close", "pre_close")
VALUATION_COLUMNS = (
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
)
LIMIT_COLUMNS = ("up_limit", "down_limit")


def _read_parquet_parts(asset_dir: str | Path, *, label: str) -> pd.DataFrame:
    root = Path(asset_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"{label} asset directory not found: {root}")
    data_root = root / "data" if (root / "data").exists() else root
    files = sorted(data_root.glob("**/*.parquet"))
    if not files:
        return pd.DataFrame()
    frames = [pd.read_parquet(path) for path in files]
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _normalize_trade_date(value: object) -> str:
    text = str(value or "").strip().replace("-", "")
    if text.endswith(".0"):
        text = text[:-2]
    return text[:8]


def _prepare_index_frame(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    if "ts_code" in out.columns:
        out["symbol"] = out["ts_code"].map(_normalize_ts_code)
    elif "symbol" in out.columns:
        out["symbol"] = out["symbol"].map(_normalize_ts_code)
    else:
        raise ValueError(f"{label} is missing ts_code/symbol.")
    if "trade_date" not in out.columns:
        raise ValueError(f"{label} is missing trade_date.")
    out["trade_date"] = out["trade_date"].map(_normalize_trade_date)
    mask = (out["symbol"] != "") & out["trade_date"].str.fullmatch(r"\d{8}", na=False)
    return cast(pd.DataFrame, out.loc[mask].copy())


def _load_instruments(instruments_file: str | Path | None) -> pd.DataFrame:
    if instruments_file is None:
        return pd.DataFrame()
    path = Path(instruments_file).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"A 股 instruments file not found: {path}")
    frame = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
    if frame.empty:
        return frame
    out = frame.copy()
    if "ts_code" in out.columns:
        out["symbol"] = out["ts_code"].map(_normalize_ts_code)
    elif "symbol" in out.columns:
        out["symbol"] = out["symbol"].map(_normalize_ts_code)
    if "name" not in out.columns:
        if "symbol" in out.columns:
            out["name"] = out["symbol"]
        else:
            out["name"] = ""
    if "list_date" in out.columns:
        out["list_date"] = out["list_date"].map(_normalize_trade_date)
    if "symbol" in out.columns:
        return out.drop_duplicates(subset=["symbol"], keep="last")
    return out


def _normalize_suspension_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    for name in ("is_open", "is_suspended", "suspend", "suspended"):
        if name in out.columns:
            out[name] = out[name].astype(str).str.strip().str.lower()
    return out


def _derive_is_suspended(daily: pd.DataFrame, suspend: pd.DataFrame | None) -> pd.Series:
    result = pd.Series(False, index=daily.index, dtype=bool)
    if daily.empty:
        return result
    if suspend is not None and not suspend.empty:
        keys = list(zip(suspend["symbol"], suspend["trade_date"], strict=False))
        result |= daily[["symbol", "trade_date"]].apply(tuple, axis=1).isin(keys)
    if "vol" in daily.columns:
        vol = pd.Series(pd.to_numeric(daily["vol"], errors="coerce"), index=daily.index)
        result |= vol.fillna(0) <= 0
    if "amount" in daily.columns:
        amount = pd.Series(pd.to_numeric(daily["amount"], errors="coerce"), index=daily.index)
        result |= amount.fillna(0) <= 0
    return result


def _derive_st_flag(daily: pd.DataFrame, instruments: pd.DataFrame) -> pd.Series:
    result = pd.Series(False, index=daily.index, dtype=bool)
    if daily.empty:
        return result
    name_cols = [col for col in ("name", "fullname") if col in instruments.columns]
    if not name_cols or "symbol" not in instruments.columns:
        return result
    lookup = instruments.set_index("symbol")
    st_symbols: set[str] = set()
    for col in name_cols:
        names = cast(pd.Series, lookup[col]).astype(str)
        st_mask = names.str.contains(r"\*?ST", case=False, regex=True, na=False)
        st_symbols.update(str(symbol) for symbol in names.loc[st_mask].index.tolist())
    return cast(pd.Series, daily["symbol"].isin(list(st_symbols)))


def _board_from_symbol(symbol: str) -> str:
    text = str(symbol).upper()
    if text.endswith(".BJ"):
        return "BSE"
    if text.startswith("688") and text.endswith(".SH"):
        return "STAR"
    if text.startswith("300") and text.endswith(".SZ"):
        return "CHINEXT"
    return "MAIN"


def _safe_numeric(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")


def build_a_share_daily_clean(
    *,
    daily_dir: str | Path,
    adj_factor_dir: str | Path | None = None,
    daily_basic_dir: str | Path | None = None,
    limit_status_dir: str | Path | None = None,
    suspend_dir: str | Path | None = None,
    instruments_file: str | Path | None = None,
    out_dir: str | Path,
    min_rows: int = 1,
    min_symbols: int = 1,
) -> dict[str, Any]:
    daily = _prepare_index_frame(_read_parquet_parts(daily_dir, label="daily"), label="daily")
    if daily.empty:
        raise ValueError("TuShare A 股 daily raw asset is empty; cannot build daily_clean.")
    daily = daily.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    _safe_numeric(daily, (*PRICE_COLUMNS, "vol", "amount", "pct_chg", "change"))

    out = daily.copy()
    adj = pd.DataFrame()
    if adj_factor_dir is not None:
        adj = _prepare_index_frame(
            _read_parquet_parts(adj_factor_dir, label="adj_factor"), label="adj_factor"
        )
        if not adj.empty and "adj_factor" in adj.columns:
            adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
            adj = adj.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
            out = out.merge(
                adj[["symbol", "trade_date", "adj_factor"]],
                on=["symbol", "trade_date"],
                how="left",
            )
    if "adj_factor" not in out.columns:
        out["adj_factor"] = pd.NA
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce")
    latest_factor = out.groupby("symbol")["adj_factor"].transform("last")
    factor_ratio = out["adj_factor"] / latest_factor
    for column in PRICE_COLUMNS:
        if column in out.columns:
            out[f"adj_{column}"] = pd.to_numeric(out[column], errors="coerce") * factor_ratio
    out["tr_close"] = out["adj_close"] if "adj_close" in out.columns else out.get("close")

    daily_basic = pd.DataFrame()
    if daily_basic_dir is not None:
        daily_basic = _prepare_index_frame(
            _read_parquet_parts(daily_basic_dir, label="daily_basic"), label="daily_basic"
        )
        if not daily_basic.empty:
            daily_basic = daily_basic.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
            keep = [
                "symbol",
                "trade_date",
                *(col for col in VALUATION_COLUMNS if col in daily_basic.columns),
            ]
            _safe_numeric(
                daily_basic,
                tuple(col for col in keep if col not in {"symbol", "trade_date"}),
            )
            out = out.merge(daily_basic[keep], on=["symbol", "trade_date"], how="left")

    limit_status = pd.DataFrame()
    if limit_status_dir is not None:
        limit_status = _prepare_index_frame(
            _read_parquet_parts(limit_status_dir, label="limit_status"), label="limit_status"
        )
        if not limit_status.empty:
            limit_status = limit_status.drop_duplicates(
                subset=["symbol", "trade_date"], keep="last"
            )
            keep = [
                "symbol",
                "trade_date",
                *(col for col in LIMIT_COLUMNS if col in limit_status.columns),
            ]
            _safe_numeric(
                limit_status,
                tuple(col for col in keep if col not in {"symbol", "trade_date"}),
            )
            out = out.merge(limit_status[keep], on=["symbol", "trade_date"], how="left")
    for column in LIMIT_COLUMNS:
        if column not in out.columns:
            out[column] = pd.NA
    out["is_limit_up"] = pd.Series(
        pd.to_numeric(out.get("close"), errors="coerce"), index=out.index
    ) >= pd.Series(pd.to_numeric(out["up_limit"], errors="coerce"), index=out.index)
    out["is_limit_down"] = pd.Series(
        pd.to_numeric(out.get("close"), errors="coerce"), index=out.index
    ) <= pd.Series(pd.to_numeric(out["down_limit"], errors="coerce"), index=out.index)

    suspend = None
    if suspend_dir is not None:
        suspend = _prepare_index_frame(
            _read_parquet_parts(suspend_dir, label="suspend"), label="suspend"
        )
        suspend = _normalize_suspension_columns(suspend)
    out["is_suspended"] = _derive_is_suspended(out, suspend)

    instruments = _load_instruments(instruments_file)
    out["is_st"] = _derive_st_flag(out, instruments)
    if not instruments.empty and "list_date" in instruments.columns:
        listed_frame = cast(pd.DataFrame, instruments[["symbol", "list_date"]])
        listed = listed_frame.sort_values("symbol").groupby("symbol", as_index=False).tail(1)
        out = out.merge(listed, on="symbol", how="left")
        trade_ts = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
        list_ts = pd.to_datetime(out["list_date"], format="%Y%m%d", errors="coerce")
        out["listed_days"] = (trade_ts - list_ts).dt.days
    else:
        out["list_date"] = pd.NA
        out["listed_days"] = pd.NA
    out["board"] = out["symbol"].map(_board_from_symbol)
    out["platform_market"] = "a_share"

    out = out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    output_dir = Path(out_dir).expanduser().resolve()
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for symbol, frame in out.groupby("symbol", sort=True):
        frame.to_parquet(data_dir / f"{symbol}.parquet", index=False)

    rows = int(len(out))
    symbols = int(out["symbol"].nunique())
    if rows < min_rows or symbols < min_symbols:
        raise ValueError(
            f"daily_clean quality gate failed: rows={rows} symbols={symbols} "
            f"min_rows={min_rows} min_symbols={min_symbols}"
        )
    duplicate_rows = int(out.duplicated(subset=["symbol", "trade_date"]).sum())
    missing_tr_close = int(out["tr_close"].isna().sum()) if "tr_close" in out.columns else rows
    manifest = {
        "schema_version": "tushare.a_share.daily_clean.v1",
        "dataset": "daily_clean",
        "market": "a_share",
        "provider": "tushare",
        "status": "completed",
        "output_dir": str(output_dir),
        "inputs": {
            "daily_dir": str(Path(daily_dir).expanduser().resolve()),
            "adj_factor_dir": (
                str(Path(adj_factor_dir).expanduser().resolve())
                if adj_factor_dir
                else None
            ),
            "daily_basic_dir": (
                str(Path(daily_basic_dir).expanduser().resolve())
                if daily_basic_dir
                else None
            ),
            "limit_status_dir": (
                str(Path(limit_status_dir).expanduser().resolve())
                if limit_status_dir
                else None
            ),
            "suspend_dir": str(Path(suspend_dir).expanduser().resolve()) if suspend_dir else None,
            "instruments_file": (
                str(Path(instruments_file).expanduser().resolve())
                if instruments_file
                else None
            ),
        },
        "totals": {"rows": rows, "symbols": symbols, "files": symbols},
        "quality": {
            "duplicate_rows": duplicate_rows,
            "missing_tr_close": missing_tr_close,
            "st_rows": int(out["is_st"].sum()),
            "suspended_rows": int(out["is_suspended"].sum()),
            "limit_up_rows": int(out["is_limit_up"].sum()),
            "limit_down_rows": int(out["is_limit_down"].sum()),
        },
        "columns": sorted(out.columns.tolist()),
    }
    _write_manifest(output_dir / "manifest.yml", manifest)
    return manifest


def validate_a_share_daily_clean(
    *,
    daily_clean_dir: str | Path,
    min_rows: int = 1,
    min_symbols: int = 1,
    require_valuation: bool = False,
    require_limit_status: bool = False,
) -> dict[str, Any]:
    frame = _prepare_index_frame(
        _read_parquet_parts(daily_clean_dir, label="daily_clean"), label="daily_clean"
    )
    required = {
        "symbol",
        "trade_date",
        "close",
        "tr_close",
        "is_st",
        "is_suspended",
        "is_limit_up",
        "is_limit_down",
    }
    missing = sorted(required.difference(frame.columns))
    if require_valuation:
        missing.extend(
            sorted({"pe_ttm", "pb", "total_mv", "turnover_rate"}.difference(frame.columns))
        )
    if require_limit_status:
        missing.extend(sorted({"up_limit", "down_limit"}.difference(frame.columns)))
    duplicate_rows = (
        int(frame.duplicated(subset=["symbol", "trade_date"]).sum())
        if not frame.empty
        else 0
    )
    rows = int(len(frame))
    symbols = int(frame["symbol"].nunique()) if "symbol" in frame.columns else 0
    errors = []
    if missing:
        errors.append(f"missing columns: {sorted(set(missing))}")
    if duplicate_rows:
        errors.append(f"duplicate symbol/trade_date rows: {duplicate_rows}")
    if rows < min_rows or symbols < min_symbols:
        errors.append(f"below minimum size: rows={rows} symbols={symbols}")
    status = "passed" if not errors else "failed"
    return {
        "dataset": "daily_clean",
        "market": "a_share",
        "provider": "tushare",
        "status": status,
        "totals": {"rows": rows, "symbols": symbols},
        "quality": {"duplicate_rows": duplicate_rows},
        "errors": errors,
    }
