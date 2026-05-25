from __future__ import annotations

import csv
import importlib
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DAILY_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_turnover",
)


def _require_module(name: str, *, install_hint: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(f"{name} is required for this command. {install_hint}") from exc


def normalize_cn_symbol(symbol: object) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    if text.endswith(".XSHG"):
        return f"{text[:-5].zfill(6)}.SH"
    if text.endswith(".XSHE"):
        return f"{text[:-5].zfill(6)}.SZ"
    if text.endswith(".SH"):
        return f"{text[:-3].zfill(6)}.SH"
    if text.endswith(".SZ"):
        return f"{text[:-3].zfill(6)}.SZ"
    if text.isdigit():
        code = text.zfill(6)
        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"
        if code.startswith(("0", "2", "3")):
            return f"{code}.SZ"
    return text


def to_rqdata_cn_symbol(symbol: object) -> str:
    canonical = normalize_cn_symbol(symbol)
    if canonical.endswith(".SH"):
        return f"{canonical[:-3]}.XSHG"
    if canonical.endswith(".SZ"):
        return f"{canonical[:-3]}.XSHE"
    return canonical


def _read_text_symbols(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            values.append(text.split(",", 1)[0].strip())
    return values


def read_symbols_file(path: str | Path) -> list[str]:
    input_path = Path(path).expanduser().resolve()
    if input_path.suffix.lower() != ".csv":
        return [
            symbol
            for symbol in map(normalize_cn_symbol, _read_text_symbols(input_path))
            if symbol
        ]

    with input_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        preferred = ("symbol", "ts_code", "stock_ticker", "order_book_id")
        symbol_col = next((column for column in preferred if column in reader.fieldnames), None)
        if symbol_col is None:
            symbol_col = reader.fieldnames[0]
        symbols = [normalize_cn_symbol(row.get(symbol_col)) for row in reader]
    return [symbol for symbol in symbols if symbol]


def _prepare_instruments_frame(frame: Any) -> Any:
    pd = _require_module("pandas", install_hint="Install pandas and pyarrow in this environment.")
    df = pd.DataFrame(frame).copy()
    if df.empty:
        return df
    if "order_book_id" not in df.columns and df.index.name:
        df = df.reset_index()
    if "order_book_id" not in df.columns:
        for column in ("symbol", "code", "orderbook_id"):
            if column in df.columns:
                df["order_book_id"] = df[column]
                break
    if "order_book_id" in df.columns:
        df["order_book_id"] = df["order_book_id"].astype(str).str.strip().str.upper()
        df["symbol"] = df["order_book_id"].map(normalize_cn_symbol)
    elif "symbol" in df.columns:
        df["symbol"] = df["symbol"].map(normalize_cn_symbol)
        df["order_book_id"] = df["symbol"].map(to_rqdata_cn_symbol)
    df["market"] = "cn"
    return df


def _write_frame(frame: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, index=False)
        return
    frame.to_parquet(path, index=False)


def export_cn_instruments(
    *,
    out: str | Path,
    date: str | None = None,
    instrument_type: str = "CS",
    symbols_out: str | Path | None = None,
) -> dict[str, Any]:
    rqdatac = _require_module("rqdatac", install_hint="Install and initialize rqdatac first.")
    kwargs: dict[str, Any] = {"type": instrument_type, "market": "cn"}
    if date:
        kwargs["date"] = date
    frame = rqdatac.all_instruments(**kwargs)
    df = _prepare_instruments_frame(frame)
    output = Path(out).expanduser().resolve()
    _write_frame(df, output)

    symbols = sorted({str(symbol) for symbol in df.get("symbol", []) if str(symbol)})
    if symbols_out is not None:
        symbols_path = Path(symbols_out).expanduser().resolve()
        symbols_path.parent.mkdir(parents=True, exist_ok=True)
        symbols_path.write_text("\n".join(symbols) + ("\n" if symbols else ""), encoding="utf-8")

    return {
        "dataset": "instruments",
        "market": "cn",
        "provider": "rqdata",
        "output": str(output),
        "symbols": len(symbols),
        "records": int(len(df)),
    }


def _prepare_daily_frame(frame: Any, *, symbol: str, order_book_id: str) -> Any:
    pd = _require_module("pandas", install_hint="Install pandas and pyarrow in this environment.")
    df = pd.DataFrame(frame).copy()
    if df.empty:
        return df
    if isinstance(df.index, pd.MultiIndex) or df.index.name is not None:
        df = df.reset_index()
    rename_map = {}
    for candidate in ("datetime", "date", "trading_date"):
        if candidate in df.columns and "trade_date" not in df.columns:
            rename_map[candidate] = "trade_date"
            break
    if rename_map:
        df = df.rename(columns=rename_map)
    if "trade_date" not in df.columns:
        df["trade_date"] = ""
    date_text = df["trade_date"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    parsed_dates = pd.to_datetime(date_text, errors="coerce")
    df["trade_date"] = parsed_dates.dt.strftime("%Y%m%d").fillna(date_text.str[:8])
    df["symbol"] = symbol
    df["order_book_id"] = order_book_id
    return df


def _fetch_daily_frame(
    rqdatac: Any,
    *,
    order_book_id: str,
    start_date: str,
    end_date: str,
    fields: list[str],
    adjust_type: str | None,
) -> Any:
    kwargs: dict[str, Any] = {
        "frequency": "1d",
        "fields": fields,
    }
    if adjust_type:
        kwargs["adjust_type"] = adjust_type
    try:
        return rqdatac.get_price(order_book_id, start_date, end_date, **kwargs)
    except TypeError:
        kwargs.pop("adjust_type", None)
        return rqdatac.get_price(order_book_id, start_date, end_date, **kwargs)


def mirror_cn_daily(
    *,
    symbols_file: str | Path,
    out_dir: str | Path,
    start_date: str,
    end_date: str,
    fields: list[str] | None = None,
    adjust_type: str | None = "pre",
    skip_existing: bool = False,
) -> dict[str, Any]:
    rqdatac = _require_module("rqdatac", install_hint="Install and initialize rqdatac first.")
    output_dir = Path(out_dir).expanduser().resolve()
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    requested_fields = list(fields or DEFAULT_DAILY_FIELDS)
    symbols = read_symbols_file(symbols_file)
    written_symbols: list[str] = []
    skipped_symbols: list[str] = []
    rows = 0
    for symbol in symbols:
        order_book_id = to_rqdata_cn_symbol(symbol)
        output_path = data_dir / f"{symbol}.parquet"
        if skip_existing and output_path.exists():
            skipped_symbols.append(symbol)
            continue
        raw = _fetch_daily_frame(
            rqdatac,
            order_book_id=order_book_id,
            start_date=start_date,
            end_date=end_date,
            fields=requested_fields,
            adjust_type=adjust_type,
        )
        df = _prepare_daily_frame(raw, symbol=symbol, order_book_id=order_book_id)
        if df.empty:
            skipped_symbols.append(symbol)
            continue
        _write_frame(df, output_path)
        written_symbols.append(symbol)
        rows += int(len(df))

    symbols_path = output_dir / "symbols.txt"
    symbols_path.write_text(
        "\n".join(written_symbols) + ("\n" if written_symbols else ""),
        encoding="utf-8",
    )
    manifest = {
        "dataset": "daily",
        "market": "cn",
        "provider": "rqdata",
        "status": "completed",
        "output_dir": str(output_dir),
        "query": {
            "start_date": start_date,
            "end_date": end_date,
            "fields": requested_fields,
            "adjust_type": adjust_type,
        },
        "totals": {
            "rows": rows,
            "symbols_requested": len(symbols),
            "symbols_written": len(written_symbols),
            "symbols_skipped": len(skipped_symbols),
            "files": len(written_symbols),
        },
    }
    (output_dir / "manifest.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return manifest
