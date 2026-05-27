# -*- coding: utf-8 -*-
"""Build an HK full-market universe from local daily asset mirrors."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yaml

from market_data_platform.artifacts import ASSETS_DIR, UNIVERSE_DIR
from market_data_platform.config_utils import repo_config_search_paths, resolve_config
from market_data_platform.hk_assets.build_hk_connect_universe import (
    default_meta_path,
    format_output_path,
    get_rebalance_dates,
    validate_yyyymmdd,
)
from market_data_platform.symbols import canonicalize_symbol_columns

DEFAULT_DAILY_ASSET_GLOBS = (
    "hk_all_*_daily_final_latest",
    "hk_all_*_daily_full_latest",
)
DEFAULTS = {
    "daily_asset_dir": None,
    "start_date": "20000104",
    "end_date": "20251231",
    "rebalance_frequency": "M",
    "lookback_days": 60,
    "min_window_days": 30,
    "top_quantile": 0.0,
    "min_turnover": 0.0,
    "out": (UNIVERSE_DIR / "hk_all_full_by_date.csv").as_posix(),
    "latest_out": (UNIVERSE_DIR / "hk_all_full_symbols.txt").as_posix(),
    "write_meta": True,
    "meta_out": (UNIVERSE_DIR / "hk_all_full_by_date.meta.yml").as_posix(),
}


def _resolve_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def load_yaml_config(path: str | Path | None) -> dict:
    resolved = resolve_config(
        path,
        package=None,
        default_name="universe/hk_all_assets.yml",
        search_paths=repo_config_search_paths(),
    )
    return resolved.data


def extract_config(cfg: dict) -> dict:
    if not cfg:
        return {}
    section = cfg.get("hk_daily_asset_universe")
    cfg = section if isinstance(section, dict) else cfg
    normalized = {}
    for key, value in cfg.items():
        normalized[str(key).strip().replace("-", "_")] = value
    return normalized


def merge_settings(defaults: dict, cfg: dict, cli: dict) -> dict:
    merged = defaults.copy()
    for key, value in cfg.items():
        if key in defaults and value is not None:
            merged[key] = value
    for key, value in cli.items():
        if key in defaults and value is not None:
            merged[key] = value
    return merged


def discover_daily_asset_dir(path_text: str | Path | None = None) -> Path:
    if path_text:
        asset_dir = _resolve_path(path_text)
        if (asset_dir / "data").exists():
            return asset_dir
        if asset_dir.name == "data" and asset_dir.is_dir():
            return asset_dir.parent
        raise SystemExit(f"Daily asset directory is missing data/: {asset_dir}")

    daily_root = (ASSETS_DIR / "rqdata" / "hk" / "daily").resolve()
    for pattern in DEFAULT_DAILY_ASSET_GLOBS:
        candidates = sorted(path for path in daily_root.glob(pattern) if (path / "data").exists())
        if candidates:
            return candidates[-1]

    patterns = ", ".join(DEFAULT_DAILY_ASSET_GLOBS)
    raise SystemExit(
        f"No HK daily asset snapshot found under {daily_root} matching any of: {patterns}."
    )


def _load_symbol_turnover_frame(path: Path) -> tuple[str, pd.Series] | None:
    try:
        frame = pd.read_parquet(path, columns=["trade_date", "symbol", "total_turnover"])
    except Exception:
        try:
            frame = pd.read_parquet(path, columns=["trade_date", "ts_code", "total_turnover"])
        except Exception:
            frame = pd.read_parquet(path)
    if frame is None or frame.empty:
        return None
    if "trade_date" not in frame.columns:
        return None
    if "symbol" not in frame.columns and "ts_code" not in frame.columns:
        frame["symbol"] = path.stem
    frame = canonicalize_symbol_columns(frame, context=f"Daily asset file {path.name}")
    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    valid = trade_dates.notna()
    if not valid.any():
        return None
    work = frame.loc[valid].copy()
    work["trade_date"] = trade_dates.loc[valid].dt.normalize()
    work["symbol"] = work["symbol"].astype(str).str.strip()
    work["total_turnover"] = pd.to_numeric(work["total_turnover"], errors="coerce")
    work = work.dropna(subset=["total_turnover"])
    if work.empty:
        return None
    symbol = str(work["symbol"].iloc[0] or path.stem).strip() or path.stem
    series = (
        work.sort_values("trade_date")
        .drop_duplicates(subset=["trade_date"], keep="last")
        .set_index("trade_date")["total_turnover"]
    )
    return symbol, series


def _collect_trade_dates(data_dir: Path) -> list[pd.Timestamp]:
    trade_dates: set[pd.Timestamp] = set()
    for path in sorted(data_dir.glob("*.parquet")):
        frame = pd.read_parquet(path, columns=["trade_date"])
        if frame is None or frame.empty:
            continue
        parsed = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
        trade_dates.update(ts.normalize() for ts in parsed.tolist())
    return sorted(trade_dates)


def select_liquid_symbols(liq: pd.Series, top_quantile: float) -> pd.Series:
    if liq.empty:
        return liq
    if top_quantile <= 0:
        return liq.sort_values(ascending=False)
    threshold = liq.quantile(top_quantile)
    return liq[liq >= threshold].sort_values(ascending=False)


def build_universe_frame(
    asset_dir: Path,
    *,
    start_date: str,
    end_date: str,
    rebalance_frequency: str,
    lookback_days: int,
    min_window_days: int,
    top_quantile: float,
    min_turnover: float,
) -> tuple[pd.DataFrame, dict]:
    data_dir = asset_dir / "data"
    if not data_dir.exists():
        raise SystemExit(f"Daily asset directory is missing data/: {asset_dir}")

    start_ts = pd.to_datetime(validate_yyyymmdd(start_date, "start-date"), format="%Y%m%d")
    end_ts = pd.to_datetime(validate_yyyymmdd(end_date, "end-date"), format="%Y%m%d")
    if start_ts > end_ts:
        raise SystemExit("start-date must be <= end-date.")
    if lookback_days <= 0:
        raise SystemExit("lookback-days must be positive.")
    if min_window_days <= 0:
        raise SystemExit("min-window-days must be positive.")
    if not 0 <= top_quantile < 1:
        raise SystemExit("top-quantile must be between 0 and 1, or exactly 0 for full coverage.")

    trade_dates = _collect_trade_dates(data_dir)
    if not trade_dates:
        raise SystemExit(f"No trade dates found under {data_dir}.")

    trade_dates_in_range = [date for date in trade_dates if start_ts <= date <= end_ts]
    if not trade_dates_in_range:
        raise SystemExit("No trading dates in the requested range.")

    rebalance_dates = get_rebalance_dates(trade_dates_in_range, rebalance_frequency)
    if not rebalance_dates:
        raise SystemExit("No rebalance dates computed for the requested range.")

    trade_index = pd.DatetimeIndex(trade_dates)
    rebalance_index = pd.DatetimeIndex(rebalance_dates)
    rows: list[pd.DataFrame] = []
    symbols_seen = 0
    symbols_selected = 0

    for path in sorted(data_dir.glob("*.parquet")):
        loaded = _load_symbol_turnover_frame(path)
        if loaded is None:
            continue
        symbol, turnover = loaded
        symbols_seen += 1
        aligned = turnover.reindex(trade_index)
        liq = aligned.shift(1).rolling(window=lookback_days, min_periods=min_window_days).median()
        liq = liq.reindex(rebalance_index).dropna()
        if min_turnover > 0:
            liq = liq[liq >= min_turnover]
        if liq.empty:
            continue
        symbols_selected += 1
        rows.append(
            pd.DataFrame(
                {
                    "trade_date": liq.index.strftime("%Y%m%d"),
                    "symbol": symbol,
                    "liq_metric": liq.astype(float).to_numpy(),
                    "selected": 1,
                }
            )
        )

    if not rows:
        raise SystemExit("No eligible symbols found after applying lookback and liquidity filters.")

    universe = pd.concat(rows, ignore_index=True)
    if top_quantile > 0:
        filtered = []
        for _, group in universe.groupby("trade_date", sort=True):
            liq = pd.Series(group["liq_metric"].to_numpy(), index=group.index, dtype=float)
            keep = select_liquid_symbols(liq, top_quantile)
            if keep.empty:
                continue
            filtered.append(group.loc[keep.index].copy())
        universe = pd.concat(filtered, ignore_index=True) if filtered else universe.iloc[0:0].copy()

    universe = universe.sort_values(["trade_date", "liq_metric", "symbol"], ascending=[True, False, True])
    universe = universe.reset_index(drop=True)

    summary = {
        "asset_dir": str(asset_dir),
        "symbols_seen": int(symbols_seen),
        "symbols_selected": int(symbols_selected),
        "trade_dates": int(len(trade_dates)),
        "rebalance_dates": int(len(rebalance_dates)),
        "rows": int(len(universe)),
        "first_trade_date": trade_dates[0].strftime("%Y%m%d"),
        "last_trade_date": trade_dates[-1].strftime("%Y%m%d"),
        "first_rebalance_date": rebalance_dates[0].strftime("%Y%m%d"),
        "last_rebalance_date": rebalance_dates[-1].strftime("%Y%m%d"),
    }
    return universe, summary


def write_outputs(
    universe: pd.DataFrame,
    *,
    out_path: Path,
    latest_out_path: Path | None,
    meta_out_path: Path | None,
    settings: dict,
    build_stats: dict,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(out_path, index=False)

    if latest_out_path is not None:
        latest_out_path.parent.mkdir(parents=True, exist_ok=True)
        latest_trade_date = str(universe["trade_date"].iloc[-1])
        latest = universe[universe["trade_date"] == latest_trade_date].copy()
        latest = latest.sort_values(["liq_metric", "symbol"], ascending=[False, True])
        latest_out_path.write_text("\n".join(latest["symbol"].tolist()) + "\n", encoding="utf-8")

    if meta_out_path is not None:
        meta_out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tool": "market_data_platform.hk_assets.build_hk_daily_asset_universe",
            "settings": settings,
            "build": build_stats,
            "outputs": {
                "by_date_file": str(out_path),
                "latest_symbols_file": str(latest_out_path) if latest_out_path else None,
            },
        }
        meta_out_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build HK full-market universe from local daily assets.")
    parser.add_argument(
        "--config",
        help="YAML config path (optional). If omitted, uses the repository preset.",
    )
    parser.add_argument(
        "--daily-asset-dir",
        help=(
            "Local daily asset snapshot directory "
            "(defaults to latest hk_all_*_daily_final_latest, then hk_all_*_daily_full_latest)."
        ),
    )
    parser.add_argument("--start-date", help="Start date in YYYYMMDD.")
    parser.add_argument("--end-date", help="End date in YYYYMMDD.")
    parser.add_argument("--rebalance-frequency", help="Rebalance frequency (default: M).")
    parser.add_argument("--lookback-days", type=int, help="Lookback trading days (default: 60).")
    parser.add_argument("--min-window-days", type=int, help="Minimum valid days in window (default: 30).")
    parser.add_argument(
        "--top-quantile",
        type=float,
        help="Liquidity quantile threshold; 0 keeps all symbols with enough history.",
    )
    parser.add_argument("--min-turnover", type=float, help="Absolute turnover threshold before quantile filter.")
    parser.add_argument("--out", help="Output CSV path.")
    parser.add_argument("--latest-out", help="Optional latest symbols txt path.")
    parser.add_argument("--meta-out", help="Optional meta YAML path.")
    parser.add_argument(
        "--write-meta",
        dest="write_meta",
        action="store_true",
        default=None,
        help="Write meta YAML alongside the output CSV.",
    )
    parser.add_argument(
        "--no-write-meta",
        dest="write_meta",
        action="store_false",
        help="Disable meta YAML output.",
    )
    args = parser.parse_args(argv)

    cfg = extract_config(load_yaml_config(args.config))
    settings = merge_settings(DEFAULTS, cfg, vars(args))
    asset_dir = discover_daily_asset_dir(settings.get("daily_asset_dir"))
    out_path = format_output_path(settings.get("out"), settings.get("end_date"), append_date=False)
    latest_out_path = (
        format_output_path(settings.get("latest_out"), settings.get("end_date"), append_date=False)
        if settings.get("latest_out")
        else None
    )
    write_meta = bool(settings.get("write_meta"))
    meta_out_path = None
    if write_meta:
        if settings.get("meta_out"):
            meta_out_path = format_output_path(settings.get("meta_out"), settings.get("end_date"), append_date=False)
        else:
            meta_out_path = default_meta_path(out_path)

    effective = {
        "daily_asset_dir": str(asset_dir),
        "start_date": str(settings.get("start_date")),
        "end_date": str(settings.get("end_date")),
        "rebalance_frequency": str(settings.get("rebalance_frequency")),
        "lookback_days": int(settings.get("lookback_days")),
        "min_window_days": int(settings.get("min_window_days")),
        "top_quantile": float(settings.get("top_quantile")),
        "min_turnover": float(settings.get("min_turnover") or 0.0),
        "out": str(out_path),
        "latest_out": str(latest_out_path) if latest_out_path else None,
        "meta_out": str(meta_out_path) if meta_out_path else None,
        "write_meta": write_meta,
    }

    print("Effective settings:")
    print(yaml.safe_dump(effective, sort_keys=False).strip())

    universe, build_stats = build_universe_frame(
        asset_dir,
        start_date=effective["start_date"],
        end_date=effective["end_date"],
        rebalance_frequency=effective["rebalance_frequency"],
        lookback_days=effective["lookback_days"],
        min_window_days=effective["min_window_days"],
        top_quantile=effective["top_quantile"],
        min_turnover=effective["min_turnover"],
    )
    write_outputs(
        universe,
        out_path=out_path,
        latest_out_path=latest_out_path,
        meta_out_path=meta_out_path,
        settings=effective,
        build_stats=build_stats,
    )
    print(f"Wrote {len(universe)} rows to {out_path}")


if __name__ == "__main__":  # pragma: no cover
    main()
