from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd
import yaml

from market_data_platform.paths import candidate_asset_paths, resolve_artifacts_root

from .tushare_a_share import _normalize_ts_code, _write_manifest

DEFAULT_REBALANCE_FREQUENCY = "M"
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_MIN_WINDOW_DAYS = 30


def _validate_date(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"{label} must be in YYYYMMDD format.")
    return text


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _manifest_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.manifest.yml")


def _rebalance_dates(dates: list[pd.Timestamp], frequency: str) -> list[pd.Timestamp]:
    normalized = str(frequency or DEFAULT_REBALANCE_FREQUENCY).strip().upper()
    if normalized == "D":
        return list(dates)
    frame = pd.DataFrame({"date": pd.to_datetime(dates)})
    frame["period"] = frame["date"].dt.to_period(normalized)
    values = cast(pd.Series, frame.groupby("period")["date"].max())
    return cast(list[pd.Timestamp], values.sort_values().tolist())


def _load_source_manifest(asset_dir: Path) -> dict[str, Any]:
    path = asset_dir / "manifest.yml"
    if not path.is_file():
        raise FileNotFoundError(f"daily_clean manifest not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"daily_clean manifest is not a mapping: {path}")
    if payload.get("status") != "completed":
        raise ValueError(f"daily_clean manifest status is not completed: {path}")
    return payload


def _load_symbol_amount_frame(path: Path) -> tuple[str, pd.Series] | None:
    try:
        frame = pd.read_parquet(path, columns=["trade_date", "symbol", "amount"])
    except Exception:
        frame = pd.read_parquet(path, columns=["trade_date", "ts_code", "amount"])
    if frame is None or frame.empty:
        return None
    if "trade_date" not in frame.columns or "amount" not in frame.columns:
        raise ValueError(f"daily_clean file is missing trade_date/amount: {path}")
    symbol_column = "symbol" if "symbol" in frame.columns else "ts_code"
    symbols = frame[symbol_column].map(_normalize_ts_code)
    if symbols.empty or not str(symbols.iloc[0]).strip():
        raise ValueError(f"daily_clean file is missing a usable symbol: {path}")
    symbol = str(symbols.iloc[0]).strip()
    trade_dates = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    amounts = pd.to_numeric(frame["amount"], errors="coerce")
    work = pd.DataFrame({"trade_date": trade_dates, "amount": amounts}).dropna()
    if work.empty:
        return None
    series = cast(
        pd.Series,
        work.sort_values("trade_date")
        .drop_duplicates(subset=["trade_date"], keep="last")
        .set_index("trade_date")["amount"],
    )
    return symbol, series


def _select_liquid_symbols(values: pd.Series, top_quantile: float) -> pd.Series:
    if values.empty:
        return values
    if top_quantile <= 0:
        return values.sort_values(ascending=False)
    threshold = values.quantile(top_quantile)
    selected = cast(pd.Series, values[values >= threshold])
    return selected.sort_values(ascending=False)


def build_a_share_universe_frame(
    daily_clean_dir: str | Path,
    *,
    start_date: str,
    end_date: str,
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_window_days: int = DEFAULT_MIN_WINDOW_DAYS,
    top_quantile: float = 0.0,
    min_turnover: float = 0.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    asset_dir = _resolved_path(daily_clean_dir)
    data_dir = asset_dir / "data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"daily_clean asset directory is missing data/: {asset_dir}")
    _load_source_manifest(asset_dir)

    start = _validate_date(start_date, label="start-date")
    end = _validate_date(end_date, label="end-date")
    start_ts = pd.to_datetime(start, format="%Y%m%d")
    end_ts = pd.to_datetime(end, format="%Y%m%d")
    if start_ts > end_ts:
        raise ValueError("start-date must be <= end-date.")
    if lookback_days <= 0:
        raise ValueError("lookback-days must be positive.")
    if min_window_days <= 0 or min_window_days > lookback_days:
        raise ValueError("min-window-days must be positive and <= lookback-days.")
    if not 0 <= top_quantile < 1:
        raise ValueError("top-quantile must be between 0 and 1, or exactly 0 for full coverage.")

    loaded_symbols: list[tuple[str, pd.Series]] = []
    trade_date_set: set[pd.Timestamp] = set()
    for path in sorted(data_dir.glob("*.parquet")):
        loaded = _load_symbol_amount_frame(path)
        if loaded is None:
            continue
        symbol, amount = loaded
        loaded_symbols.append((symbol, amount))
        trade_date_set.update(
            cast(pd.Timestamp, pd.Timestamp(value)).normalize()
            for value in amount.index.tolist()
            if not pd.isna(value)
        )
    if not trade_date_set:
        raise ValueError(f"No trading dates found under {data_dir}.")

    trade_dates = sorted(trade_date_set)
    trade_dates_in_range = [value for value in trade_dates if start_ts <= value <= end_ts]
    if not trade_dates_in_range:
        raise ValueError("No trading dates in the requested range.")
    rebalance_dates = _rebalance_dates(trade_dates_in_range, rebalance_frequency)
    if not rebalance_dates:
        raise ValueError("No rebalance dates computed for the requested range.")

    trade_index = pd.DatetimeIndex(trade_dates)
    rebalance_index = pd.DatetimeIndex(rebalance_dates)
    rows: list[pd.DataFrame] = []
    symbols_selected = 0
    for symbol, amount in loaded_symbols:
        aligned = cast(pd.Series, amount.reindex(trade_index))
        liquidity = cast(
            pd.Series,
            aligned.shift(1)
            .rolling(window=lookback_days, min_periods=min_window_days)
            .median()
            .reindex(rebalance_index)
            .dropna(),
        )
        if min_turnover > 0:
            liquidity = cast(pd.Series, liquidity[liquidity >= min_turnover])
        if liquidity.empty:
            continue
        symbols_selected += 1
        rows.append(
            pd.DataFrame(
                {
                    "trade_date": pd.DatetimeIndex(liquidity.index).strftime("%Y%m%d"),
                    "symbol": symbol,
                    "liq_metric": liquidity.astype(float).to_numpy(),
                    "selected": 1,
                }
            )
        )
    if not rows:
        raise ValueError("No eligible symbols found after applying lookback and liquidity filters.")

    universe = pd.concat(rows, ignore_index=True)
    if top_quantile > 0:
        filtered: list[pd.DataFrame] = []
        for _, group in universe.groupby("trade_date", sort=True):
            values = pd.Series(group["liq_metric"].to_numpy(), index=group.index, dtype=float)
            selected = _select_liquid_symbols(values, top_quantile)
            if not selected.empty:
                filtered.append(group.loc[selected.index].copy())
        universe = pd.concat(filtered, ignore_index=True) if filtered else universe.iloc[0:0].copy()
    universe = universe.sort_values(
        ["trade_date", "liq_metric", "symbol"],
        ascending=[True, False, True],
    ).reset_index(drop=True)

    latest_date = str(universe["trade_date"].max())
    latest_symbols = int(universe.loc[universe["trade_date"] == latest_date, "symbol"].nunique())
    actual_rebalance_dates = int(universe["trade_date"].nunique())
    summary = {
        "rows": int(len(universe)),
        "symbols_seen": int(len(loaded_symbols)),
        "symbols_selected": int(symbols_selected),
        "latest_symbols": latest_symbols,
        "trade_dates": int(len(trade_dates)),
        "rebalance_dates_requested": int(len(rebalance_dates)),
        "rebalance_dates": actual_rebalance_dates,
        "first_trade_date": trade_dates[0].strftime("%Y%m%d"),
        "last_trade_date": trade_dates[-1].strftime("%Y%m%d"),
        "first_rebalance_date": str(universe["trade_date"].min()),
        "last_rebalance_date": latest_date,
    }
    return universe, summary


def _assert_output_paths_available(paths: list[Path], *, force: bool) -> None:
    if force:
        return
    existing = [str(path) for path in paths if path.exists() or path.is_symlink()]
    if existing:
        joined = ", ".join(existing)
        raise FileExistsError(f"Refusing to overwrite existing A 股 universe output(s): {joined}")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _universe_manifest(
    *,
    dataset: str,
    output: Path,
    settings: dict[str, Any],
    build: dict[str, Any],
    totals: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": f"tushare.a_share.{dataset}.v1",
        "dataset": dataset,
        "market": "a_share",
        "provider": "tushare",
        "status": "completed",
        "output_dir": str(output),
        "query": {
            "start_date": build["first_rebalance_date"],
            "end_date": build["last_rebalance_date"],
            "source_daily_clean_dir": settings["daily_clean_dir"],
            "rebalance_frequency": settings["rebalance_frequency"],
        },
        "totals": totals,
    }


def build_a_share_universe(
    *,
    artifacts_root: str | Path | None = None,
    daily_clean_dir: str | Path | None = None,
    start_date: str,
    end_date: str,
    rebalance_frequency: str = DEFAULT_REBALANCE_FREQUENCY,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_window_days: int = DEFAULT_MIN_WINDOW_DAYS,
    top_quantile: float = 0.0,
    min_turnover: float = 0.0,
    out: str | Path | None = None,
    latest_out: str | Path | None = None,
    meta_out: str | Path | None = None,
    min_rows: int = 1,
    min_symbols: int = 1,
    min_rebalance_dates: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    assets = candidate_asset_paths(root, market="a_share", provider="tushare")
    daily_clean = _resolved_path(daily_clean_dir or assets["daily_clean"])
    out_path = _resolved_path(out or assets["universe_by_date"])
    latest_out_path = _resolved_path(latest_out or assets["universe_symbols"])
    meta_out_path = _resolved_path(meta_out or assets["universe_meta"])
    output_paths = [
        out_path,
        latest_out_path,
        meta_out_path,
        _manifest_path(out_path),
        _manifest_path(latest_out_path),
        _manifest_path(meta_out_path),
    ]
    _assert_output_paths_available(output_paths, force=force)

    universe, build = build_a_share_universe_frame(
        daily_clean,
        start_date=start_date,
        end_date=end_date,
        rebalance_frequency=rebalance_frequency,
        lookback_days=lookback_days,
        min_window_days=min_window_days,
        top_quantile=top_quantile,
        min_turnover=min_turnover,
    )
    if build["rows"] < min_rows:
        raise ValueError(
            f"A 股 universe quality gate failed: rows={build['rows']} min_rows={min_rows}"
        )
    if build["latest_symbols"] < min_symbols:
        raise ValueError(
            "A 股 universe quality gate failed: "
            f"latest_symbols={build['latest_symbols']} min_symbols={min_symbols}"
        )
    if build["rebalance_dates"] < min_rebalance_dates:
        raise ValueError(
            "A 股 universe quality gate failed: "
            f"rebalance_dates={build['rebalance_dates']} min_rebalance_dates={min_rebalance_dates}"
        )

    latest_date = build["last_rebalance_date"]
    latest = universe.loc[universe["trade_date"] == latest_date].copy()
    latest = latest.sort_values(["liq_metric", "symbol"], ascending=[False, True])
    latest_symbols = latest["symbol"].drop_duplicates().tolist()
    settings = {
        "daily_clean_dir": str(daily_clean),
        "start_date": _validate_date(start_date, label="start-date"),
        "end_date": _validate_date(end_date, label="end-date"),
        "rebalance_frequency": str(rebalance_frequency).upper(),
        "lookback_days": int(lookback_days),
        "min_window_days": int(min_window_days),
        "top_quantile": float(top_quantile),
        "min_turnover": float(min_turnover),
    }
    outputs = {
        "by_date_file": str(out_path),
        "latest_symbols_file": str(latest_out_path),
        "meta_file": str(meta_out_path),
    }
    meta = {
        "schema_version": "tushare.a_share.universe.v1",
        "tool": "marketdata tushare build-a-share-universe",
        "settings": settings,
        "build": build,
        "quality": {
            "duplicate_rows": int(universe.duplicated(subset=["trade_date", "symbol"]).sum()),
            "latest_symbols": int(len(latest_symbols)),
        },
        "outputs": outputs,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(out_path, index=False)
    _write_text(latest_out_path, "\n".join(latest_symbols) + "\n")
    _write_text(meta_out_path, yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))
    common_totals = {
        "rows": int(len(universe)),
        "symbols": int(universe["symbol"].nunique()),
        "latest_symbols": int(len(latest_symbols)),
        "rebalance_dates": int(universe["trade_date"].nunique()),
        "files": 1,
    }
    _write_manifest(
        _manifest_path(out_path),
        _universe_manifest(
            dataset="universe_by_date",
            output=out_path,
            settings=settings,
            build=build,
            totals=common_totals,
        ),
    )
    _write_manifest(
        _manifest_path(latest_out_path),
        _universe_manifest(
            dataset="universe_symbols",
            output=latest_out_path,
            settings=settings,
            build=build,
            totals={"rows": len(latest_symbols), "symbols": len(latest_symbols), "files": 1},
        ),
    )
    _write_manifest(
        _manifest_path(meta_out_path),
        _universe_manifest(
            dataset="universe_meta",
            output=meta_out_path,
            settings=settings,
            build=build,
            totals={"rows": 1, "symbols": len(latest_symbols), "files": 1},
        ),
    )
    return {
        "dataset": "universe",
        "market": "a_share",
        "provider": "tushare",
        "status": "completed",
        "outputs": outputs,
        "build": build,
        "quality": meta["quality"],
    }


def validate_a_share_universe(
    *,
    by_date_file: str | Path,
    latest_symbols_file: str | Path,
    meta_file: str | Path,
    expected_as_of: str | None = None,
    min_rows: int = 1,
    min_symbols: int = 1,
    min_rebalance_dates: int = 1,
) -> dict[str, Any]:
    by_date_path = _resolved_path(by_date_file)
    latest_path = _resolved_path(latest_symbols_file)
    meta_path = _resolved_path(meta_file)
    errors: list[str] = []
    universe = pd.read_csv(by_date_path)
    required = {"trade_date", "symbol", "liq_metric", "selected"}
    missing = sorted(required.difference(universe.columns))
    if missing:
        errors.append(f"universe_by_date is missing columns: {missing}")
    if missing:
        rows = int(len(universe))
        symbols = 0
        rebalance_dates = 0
        duplicate_rows = 0
        actual_as_of = None
    else:
        universe["trade_date"] = (
            universe["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
        )
        universe["symbol"] = universe["symbol"].map(_normalize_ts_code)
        rows = int(len(universe))
        symbols = int(universe["symbol"].nunique())
        rebalance_dates = int(universe["trade_date"].nunique())
        duplicate_rows = int(universe.duplicated(subset=["trade_date", "symbol"]).sum())
        actual_as_of = str(universe["trade_date"].max()) if rows else None
        if duplicate_rows:
            errors.append(
                f"universe_by_date has duplicate trade_date/symbol rows: {duplicate_rows}"
            )

    latest_symbols = [
        _normalize_ts_code(line)
        for line in latest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(latest_symbols) != len(set(latest_symbols)):
        errors.append("latest_symbols_file contains duplicate symbols")
    if not missing and actual_as_of:
        expected_latest = set(
            universe.loc[universe["trade_date"] == actual_as_of, "symbol"].astype(str).tolist()
        )
        if set(latest_symbols) != expected_latest:
            errors.append(
                "latest_symbols_file does not match the latest universe_by_date partition"
            )
    if rows < min_rows:
        errors.append(f"rows={rows} is below min_rows={min_rows}")
    if len(latest_symbols) < min_symbols:
        errors.append(f"latest_symbols={len(latest_symbols)} is below min_symbols={min_symbols}")
    if rebalance_dates < min_rebalance_dates:
        errors.append(
            f"rebalance_dates={rebalance_dates} is below min_rebalance_dates={min_rebalance_dates}"
        )
    if expected_as_of is not None:
        expected = _validate_date(expected_as_of, label="expected-as-of")
        if actual_as_of != expected:
            errors.append(f"actual_as_of={actual_as_of} does not match expected_as_of={expected}")

    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        errors.append("meta_file is not a mapping")
    else:
        build = meta.get("build")
        if not isinstance(build, dict) or str(build.get("last_rebalance_date")) != str(
            actual_as_of
        ):
            errors.append("meta_file last_rebalance_date does not match universe_by_date")
    return {
        "dataset": "universe",
        "market": "a_share",
        "provider": "tushare",
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "totals": {
            "rows": rows,
            "symbols": symbols,
            "latest_symbols": len(latest_symbols),
            "rebalance_dates": rebalance_dates,
            "duplicate_rows": duplicate_rows,
        },
        "as_of": actual_as_of,
    }
