from __future__ import annotations

import importlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TOKEN_ENV_KEYS = ("TUSHARE_TOKEN", "TUSHARE_TOKEN_2")
DEFAULT_STOCK_BASIC_FIELDS = (
    "ts_code",
    "symbol",
    "name",
    "area",
    "industry",
    "fullname",
    "market",
    "exchange",
    "curr_type",
    "list_status",
    "list_date",
    "delist_date",
    "is_hs",
)
DEFAULT_LIST_STATUSES = ("L", "D", "P", "G")
TRADE_DATE_APIS = {
    "daily": "daily",
    "adj_factor": "adj_factor",
    "daily_basic": "daily_basic",
    "limit_status": "stk_limit",
}


def _require_module(name: str, *, install_hint: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise RuntimeError(f"{name} is required for this command. {install_hint}") from exc


def _pandas() -> Any:
    return _require_module("pandas", install_hint="Install the tushare optional dependencies.")


def _validate_date(value: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"Expected YYYYMMDD date, got: {value}")
    return text


def _normalize_ts_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text.endswith((".SH", ".SZ", ".BJ")):
        code, exchange = text.rsplit(".", 1)
        return f"{code.zfill(6)}.{exchange}"
    return text


def _normalize_date_column(frame: Any, column: str) -> None:
    if column not in frame.columns:
        return
    values = frame[column].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    frame[column] = values.str.replace("-", "", regex=False).str[:8]


def _prepare_frame(frame: Any) -> Any:
    pd = _pandas()
    df = pd.DataFrame(frame).copy()
    if df.empty:
        return df
    if "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].map(_normalize_ts_code)
        df["symbol"] = df["ts_code"]
    for column in ("trade_date", "cal_date", "pretrade_date", "list_date", "delist_date"):
        _normalize_date_column(df, column)
    df["platform_market"] = "cn"
    return df


def _write_frame(frame: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, index=False)
        return
    frame.to_parquet(path, index=False)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _single_file_manifest_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.manifest.yml")


def _fields_text(fields: Iterable[str] | None, required: tuple[str, ...] = ()) -> str | None:
    if fields is None:
        return None
    selected: list[str] = []
    for field in (*required, *fields):
        value = str(field).strip()
        if value and value not in selected:
            selected.append(value)
    return ",".join(selected)


def _resolve_token(token: str | None, token_env: str) -> str:
    value = str(token or os.environ.get(token_env) or "").strip()
    if not value:
        raise RuntimeError(f"No TuShare token found in environment variable {token_env}.")
    return value


def _redact_error(error: Exception, token: str) -> str:
    message = str(error) or error.__class__.__name__
    return message.replace(token, "<redacted>") if token else message


def get_tushare_client(
    *,
    token: str | None = None,
    token_env: str = "TUSHARE_TOKEN",
) -> Any:
    ts = _require_module("tushare", install_hint="Install with the tushare optional extra.")
    return ts.pro_api(token=_resolve_token(token, token_env))


def verify_tushare_tokens(
    *,
    env_keys: Iterable[str] | None = None,
    tushare_module: Any | None = None,
) -> dict[str, Any]:
    ts = tushare_module or _require_module(
        "tushare",
        install_hint="Install with the tushare optional extra.",
    )
    results: list[dict[str, Any]] = []
    for env_key in env_keys or DEFAULT_TOKEN_ENV_KEYS:
        key = str(env_key).strip()
        token = str(os.environ.get(key) or "").strip()
        if not token:
            results.append({"env": key, "configured": False, "valid": False, "error": "not set"})
            continue
        try:
            client = ts.pro_api(token=token)
            response = client.user(token=token)
            if response is None:
                raise RuntimeError("TuShare returned no user response")
        except Exception as exc:  # Provider errors are reported without exposing the token.
            results.append(
                {
                    "env": key,
                    "configured": True,
                    "valid": False,
                    "error": _redact_error(exc, token),
                }
            )
            continue
        results.append({"env": key, "configured": True, "valid": True})
    valid_count = sum(1 for result in results if result["valid"])
    return {
        "provider": "tushare",
        "checked": len(results),
        "valid_tokens": valid_count,
        "results": results,
    }


def export_cn_instruments(
    *,
    out: str | Path,
    list_statuses: Iterable[str] | None = None,
    fields: Iterable[str] | None = None,
    symbols_out: str | Path | None = None,
    token_env: str = "TUSHARE_TOKEN",
    client: Any | None = None,
) -> dict[str, Any]:
    pd = _pandas()
    pro = client or get_tushare_client(token_env=token_env)
    statuses = tuple(
        str(value).strip().upper() for value in (list_statuses or DEFAULT_LIST_STATUSES)
    )
    requested_fields = tuple(fields or DEFAULT_STOCK_BASIC_FIELDS)
    fields_text = _fields_text(requested_fields, required=("ts_code", "list_status"))
    frames = [
        _prepare_frame(pro.stock_basic(exchange="", list_status=status, fields=fields_text))
        for status in statuses
    ]
    non_empty = [frame for frame in frames if not frame.empty]
    df = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
    if not df.empty and "ts_code" in df.columns:
        df = df.drop_duplicates(subset=["ts_code", "list_status"]).sort_values("ts_code")

    output = Path(out).expanduser().resolve()
    _write_frame(df, output)
    symbols = sorted(df["symbol"].dropna().astype(str).unique().tolist()) if "symbol" in df else []
    if symbols_out is not None:
        symbols_path = Path(symbols_out).expanduser().resolve()
        symbols_path.parent.mkdir(parents=True, exist_ok=True)
        symbols_path.write_text("\n".join(symbols) + ("\n" if symbols else ""), encoding="utf-8")

    manifest = {
        "schema_version": "tushare.stock_basic.v1",
        "dataset": "instruments",
        "market": "cn",
        "provider": "tushare",
        "status": "completed",
        "output_dir": str(output),
        "query": {"list_statuses": list(statuses), "fields": list(requested_fields)},
        "totals": {"rows": int(len(df)), "symbols": len(symbols), "files": 1},
    }
    _write_manifest(_single_file_manifest_path(output), manifest)
    return manifest


def mirror_cn_trade_cal(
    *,
    out: str | Path,
    start_date: str,
    end_date: str,
    exchange: str = "",
    token_env: str = "TUSHARE_TOKEN",
    client: Any | None = None,
) -> dict[str, Any]:
    start = _validate_date(start_date)
    end = _validate_date(end_date)
    pro = client or get_tushare_client(token_env=token_env)
    df = _prepare_frame(pro.trade_cal(exchange=exchange, start_date=start, end_date=end))
    output = Path(out).expanduser().resolve()
    _write_frame(df, output)
    open_dates = int((df["is_open"].astype(str) == "1").sum()) if "is_open" in df else 0
    manifest = {
        "schema_version": "tushare.trade_cal.v1",
        "dataset": "trade_cal",
        "market": "cn",
        "provider": "tushare",
        "status": "completed",
        "output_dir": str(output),
        "query": {"exchange": exchange, "start_date": start, "end_date": end},
        "totals": {"rows": int(len(df)), "open_dates": open_dates, "files": 1},
    }
    _write_manifest(_single_file_manifest_path(output), manifest)
    return manifest


def _open_trade_dates(client: Any, *, start_date: str, end_date: str) -> list[str]:
    frame = _prepare_frame(
        client.trade_cal(exchange="", start_date=start_date, end_date=end_date, is_open="1")
    )
    if frame.empty:
        return []
    if "cal_date" not in frame.columns or "is_open" not in frame.columns:
        raise ValueError("TuShare trade_cal response is missing cal_date or is_open.")
    values = frame.loc[frame["is_open"].astype(str) == "1", "cal_date"].astype(str)
    return sorted({_validate_date(value) for value in values})


def mirror_cn_trade_date_dataset(
    *,
    dataset: str,
    out_dir: str | Path,
    start_date: str,
    end_date: str,
    fields: Iterable[str] | None = None,
    skip_existing: bool = False,
    token_env: str = "TUSHARE_TOKEN",
    client: Any | None = None,
) -> dict[str, Any]:
    if dataset not in TRADE_DATE_APIS:
        raise ValueError(f"Unsupported TuShare CN trade-date dataset: {dataset}")
    start = _validate_date(start_date)
    end = _validate_date(end_date)
    pro = client or get_tushare_client(token_env=token_env)
    api_name = TRADE_DATE_APIS[dataset]
    output_dir = Path(out_dir).expanduser().resolve()
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    requested_fields = tuple(fields or ())
    fields_text = _fields_text(requested_fields, required=("ts_code", "trade_date"))
    trade_dates = _open_trade_dates(pro, start_date=start, end_date=end)

    rows = 0
    symbols: set[str] = set()
    written_dates: list[str] = []
    skipped_dates: list[str] = []
    empty_dates: list[str] = []
    for trade_date in trade_dates:
        output_path = data_dir / f"trade_date={trade_date}" / "part.parquet"
        if skip_existing and output_path.exists():
            skipped_dates.append(trade_date)
            continue
        kwargs: dict[str, Any] = {"trade_date": trade_date}
        if fields_text is not None:
            kwargs["fields"] = fields_text
        df = _prepare_frame(getattr(pro, api_name)(**kwargs))
        if df.empty:
            empty_dates.append(trade_date)
            continue
        _write_frame(df, output_path)
        written_dates.append(trade_date)
        rows += int(len(df))
        if "symbol" in df.columns:
            symbols.update(df["symbol"].dropna().astype(str).tolist())

    manifest = {
        "schema_version": f"tushare.{api_name}.v1",
        "dataset": dataset,
        "market": "cn",
        "provider": "tushare",
        "status": "completed",
        "output_dir": str(output_dir),
        "query": {
            "api": api_name,
            "start_date": start,
            "end_date": end,
            "fields": list(requested_fields) if requested_fields else None,
            "partition_by": "trade_date",
        },
        "totals": {
            "rows": rows,
            "symbols": len(symbols),
            "trade_dates_requested": len(trade_dates),
            "trade_dates_written": len(written_dates),
            "trade_dates_skipped": len(skipped_dates),
            "trade_dates_empty": len(empty_dates),
            "files": len(written_dates),
        },
        "written_trade_dates": written_dates,
        "skipped_trade_dates": skipped_dates,
        "empty_trade_dates": empty_dates,
    }
    _write_manifest(output_dir / "manifest.yml", manifest)
    return manifest


def mirror_cn_daily(**kwargs: Any) -> dict[str, Any]:
    return mirror_cn_trade_date_dataset(dataset="daily", **kwargs)


def mirror_cn_adj_factor(**kwargs: Any) -> dict[str, Any]:
    return mirror_cn_trade_date_dataset(dataset="adj_factor", **kwargs)


def mirror_cn_daily_basic(**kwargs: Any) -> dict[str, Any]:
    return mirror_cn_trade_date_dataset(dataset="daily_basic", **kwargs)


def mirror_cn_limit_status(**kwargs: Any) -> dict[str, Any]:
    return mirror_cn_trade_date_dataset(dataset="limit_status", **kwargs)
