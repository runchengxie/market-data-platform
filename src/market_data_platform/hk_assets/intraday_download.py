#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from market_data_platform.repo_paths import find_repo_root, resolve_repo_path as resolve_repo_relative_path
from market_data_platform.rqdata_runtime import init_rqdatac as _init_rqdatac_runtime
from market_data_platform.symbols import normalize_symbol_for_market


REPO_ROOT = find_repo_root(__file__)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "cache" / "intraday"
DEFAULT_FIELDS = ("open", "high", "low", "close", "volume", "total_turnover")
ADJUST_TYPE_CHOICES = ("none", "pre", "post", "pre_volume", "post_volume")


def resolve_repo_path(path_text: str | Path) -> Path:
    return resolve_repo_relative_path(path_text, repo_root=REPO_ROOT)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _to_rq_order_book_id(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return text
    if text.endswith(".XHKG"):
        return text
    if text.endswith(".XSHG") or text.endswith(".XSHE") or text.endswith(".SH") or text.endswith(".SZ"):
        raise SystemExit(
            f"Unsupported symbol '{symbol}'. This script currently supports only HK symbols."
        )
    if text.endswith(".HK"):
        text = text[:-3]
    if text.isdigit():
        text = text.zfill(5)
    return f"{text}.XHKG"


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        deduped.append(text)
        seen.add(text)
    return deduped


def _read_symbol_file(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".list"}:
        values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    elif suffix in {".csv", ".parquet"}:
        if suffix == ".parquet":
            frame = pd.read_parquet(path)
        else:
            frame = pd.read_csv(path)
        for candidate in ("symbol", "ts_code", "stock_ticker", "order_book_id"):
            if candidate in frame.columns:
                values = frame[candidate].astype(str).str.strip().tolist()
                break
        else:
            raise SystemExit(
                "Unsupported symbol file schema: "
                f"{path}. Expected a canonical symbol column; legacy aliases "
                "ts_code/stock_ticker/order_book_id remain accepted."
            )
    else:
        raise SystemExit(f"Unsupported symbol file format: {path}")

    normalized = [
        normalize_symbol_for_market(value, market="hk")
        for value in values
    ]
    return _dedupe_preserve_order(normalized)


def normalize_hk_symbols(symbols: list[str]) -> list[str]:
    mapped = [_to_rq_order_book_id(symbol) for symbol in symbols]
    return sorted(dict.fromkeys(mapped))


def flatten_intraday_payload(
    payload: pd.DataFrame | None,
    *,
    order_book_to_symbol: dict[str, str],
) -> pd.DataFrame:
    if payload is None:
        return pd.DataFrame(
            columns=[
                "rq_order_book_id",
                "trade_datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "symbol",
            ]
        )
    if payload.empty:
        return pd.DataFrame(
            columns=[
                "rq_order_book_id",
                "trade_datetime",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "symbol",
            ]
        )
    if not isinstance(payload.index, pd.MultiIndex):
        raise SystemExit("Expected rqdatac.get_price(..., frequency='5m') to return a MultiIndex DataFrame.")

    frame = payload.reset_index()
    order_book_col = "order_book_id" if "order_book_id" in frame.columns else frame.columns[0]
    datetime_col = "datetime" if "datetime" in frame.columns else frame.columns[1]
    rename_map = {
        order_book_col: "rq_order_book_id",
        datetime_col: "trade_datetime",
        "total_turnover": "amount",
    }
    frame = frame.rename(columns=rename_map)
    frame["trade_datetime"] = pd.to_datetime(frame["trade_datetime"], errors="coerce")
    frame = frame.dropna(subset=["trade_datetime", "rq_order_book_id"]).copy()
    frame["rq_order_book_id"] = frame["rq_order_book_id"].astype(str).str.upper()
    frame["symbol"] = frame["rq_order_book_id"].map(order_book_to_symbol)
    keep = [
        "rq_order_book_id",
        "trade_datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "symbol",
    ]
    for column in keep:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[keep].sort_values(["rq_order_book_id", "trade_datetime"]).reset_index(drop=True)


def _default_parts_dir(output_path: Path) -> Path:
    return output_path.parent / f"{output_path.stem}.parts"


def _batch_part_path(parts_dir: Path, batch_index: int) -> Path:
    return parts_dir / f"batch_{batch_index:04d}.parquet"


def _batch_meta_path(parts_dir: Path, batch_index: int) -> Path:
    return parts_dir / f"batch_{batch_index:04d}.meta.json"


def _batch_signature(
    *,
    order_book_ids: list[str],
    start_date: str,
    end_date: str,
    frequency: str,
    fields: list[str],
    adjust_type: str,
) -> dict[str, object]:
    payload = {
        "order_book_ids": list(order_book_ids),
        "start_date": str(start_date),
        "end_date": str(end_date),
        "frequency": str(frequency),
        "fields": list(fields),
        "adjust_type": str(adjust_type),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["signature"] = hashlib.sha256(encoded).hexdigest()
    return payload


def _load_batch_meta(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _batch_meta_matches(path: Path, expected: dict[str, object]) -> bool:
    payload = _load_batch_meta(path)
    if not isinstance(payload, dict):
        return False
    return str(payload.get("signature") or "") == str(expected.get("signature") or "")


def _write_batch_meta(path: Path, *, signature: dict[str, object], rows: int) -> None:
    payload = dict(signature)
    payload["rows"] = int(rows)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _count_part_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return int(len(pd.read_parquet(path, columns=["rq_order_book_id"])))


def _trade_date_range_from_table(table: pq.Table) -> tuple[str | None, str | None]:
    if table.num_rows == 0 or "trade_datetime" not in table.column_names:
        return None, None
    trade_datetime = pd.to_datetime(table["trade_datetime"].to_pandas(), errors="coerce").dropna()
    if trade_datetime.empty:
        return None, None
    return trade_datetime.min().strftime("%Y%m%d"), trade_datetime.max().strftime("%Y%m%d")


def merge_batch_parts(
    parts_dir: Path,
    output_path: Path,
) -> tuple[int, int, str | None, str | None]:
    part_files = sorted(parts_dir.glob("batch_*.parquet"))
    if not part_files:
        raise SystemExit(f"No batch parquet files found under: {parts_dir}")

    writer = None
    total_rows = 0
    total_symbols: set[str] = set()
    min_trade_date: str | None = None
    max_trade_date: str | None = None
    try:
        for part_path in part_files:
            table = pq.read_table(part_path)
            total_rows += int(table.num_rows)
            if "rq_order_book_id" in table.column_names:
                total_symbols.update(table["rq_order_book_id"].to_pylist())
            candidate_min, candidate_max = _trade_date_range_from_table(table)
            if candidate_min is not None:
                min_trade_date = (
                    candidate_min
                    if min_trade_date is None
                    else min(min_trade_date, candidate_min)
                )
            if candidate_max is not None:
                max_trade_date = (
                    candidate_max
                    if max_trade_date is None
                    else max(max_trade_date, candidate_max)
                )
            if table.num_rows == 0:
                continue
            if writer is None:
                writer = pq.ParquetWriter(output_path, table.schema)
            writer.write_table(table)
        if writer is None:
            empty = flatten_intraday_payload(pd.DataFrame(), order_book_to_symbol={})
            empty.to_parquet(output_path, index=False)
    finally:
        if writer is not None:
            writer.close()
    return total_rows, len(total_symbols), min_trade_date, max_trade_date


def download_hk_intraday_cache(args, rqdatac) -> dict[str, object]:
    symbol_file = resolve_repo_path(args.symbols_file)
    if not symbol_file.exists():
        raise SystemExit(f"Symbol file not found: {symbol_file}")

    symbols = _read_symbol_file(symbol_file)
    if not symbols:
        raise SystemExit(f"No symbols found in: {symbol_file}")

    order_book_ids = normalize_hk_symbols(symbols)
    order_book_to_symbol = {
        _to_rq_order_book_id(symbol): symbol
        for symbol in symbols
    }

    output_path = resolve_repo_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = (
        resolve_repo_path(args.meta_output)
        if getattr(args, "meta_output", None)
        else output_path.with_suffix(".meta.json")
    )
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    parts_dir = (
        resolve_repo_path(args.parts_dir)
        if getattr(args, "parts_dir", None)
        else _default_parts_dir(output_path)
    )
    parts_dir.mkdir(parents=True, exist_ok=True)

    quota_before = rqdatac.user.get_quota()
    batch_rows: list[dict[str, int | str]] = []
    total = len(order_book_ids)
    for start in range(0, total, int(args.batch_size)):
        batch_index = start // int(args.batch_size) + 1
        batch = order_book_ids[start : start + int(args.batch_size)]
        part_path = _batch_part_path(parts_dir, batch_index)
        part_meta_path = _batch_meta_path(parts_dir, batch_index)
        batch_signature = _batch_signature(
            order_book_ids=batch,
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            frequency=str(args.frequency),
            fields=list(args.fields),
            adjust_type=str(args.adjust_type),
        )
        status = "downloaded"
        reuse_existing = False
        if (
            getattr(args, "resume", False)
            and part_path.exists()
            and _batch_meta_matches(part_meta_path, batch_signature)
        ):
            try:
                rows = _count_part_rows(part_path)
            except Exception:
                status = "refreshed_resume_corrupt"
            else:
                status = "reused"
                reuse_existing = True
        if not reuse_existing:
            if getattr(args, "resume", False) and part_path.exists():
                if status == "downloaded":
                    status = "refreshed_resume_mismatch"
            payload = rqdatac.get_price(
                batch,
                args.start_date,
                args.end_date,
                frequency=args.frequency,
                fields=list(args.fields),
                adjust_type=args.adjust_type,
                market="hk",
                expect_df=True,
            )
            frame = flatten_intraday_payload(payload, order_book_to_symbol=order_book_to_symbol)
            frame.to_parquet(part_path, index=False)
            rows = int(len(frame))
            _write_batch_meta(part_meta_path, signature=batch_signature, rows=rows)
        batch_rows.append(
            {
                "batch": batch_index,
                "symbols": len(batch),
                "rows": rows,
                "status": status,
                "part_file": _display_path(part_path),
                "part_meta_file": _display_path(part_meta_path),
            }
        )
        print(
            f"batch {batch_index}: "
            f"{start + len(batch)}/{total} symbols, {rows} rows, {status}"
        )

    total_rows, total_symbols, min_trade_date, max_trade_date = merge_batch_parts(
        parts_dir,
        output_path,
    )
    quota_after = rqdatac.user.get_quota()
    merged_columns = list(pq.ParquetFile(output_path).schema.names)

    meta = {
        "dataset": "hk_intraday_cache",
        "symbols_file": _display_path(symbol_file),
        "symbols_requested": int(len(symbols)),
        "symbols_downloaded": int(total_symbols),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "min_trade_date": min_trade_date,
        "max_trade_date": max_trade_date,
        "frequency": str(args.frequency),
        "adjust_type": str(args.adjust_type),
        "fields": list(args.fields),
        "rows": int(total_rows),
        "columns": merged_columns,
        "parts_dir": _display_path(parts_dir),
        "resume": bool(getattr(args, "resume", False)),
        "quota_before": quota_before,
        "quota_after": quota_after,
        "bytes_used_delta": float(quota_after["bytes_used"] - quota_before["bytes_used"]),
        "file_size_bytes": int(output_path.stat().st_size),
        "batches": batch_rows,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"saved parquet: {output_path}")
    print(f"saved meta: {meta_path}")
    print(f"rows={total_rows} quota_delta={meta['bytes_used_delta']}")

    return {
        "output_path": output_path,
        "meta_path": meta_path,
        "parts_dir": parts_dir,
        "rows": int(total_rows),
        "symbols_requested": int(len(symbols)),
        "symbols_downloaded": int(total_symbols),
        "meta": meta,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download HK intraday bars from RQData and save a flat parquet cache."
    )
    parser.add_argument("--symbols-file", required=True, help="TXT/CSV/Parquet file containing HK symbols.")
    parser.add_argument("--start-date", required=True, help="Start date, e.g. 20250327.")
    parser.add_argument("--end-date", required=True, help="End date, e.g. 20260326.")
    parser.add_argument("--frequency", default="5m", help="Intraday frequency. Default: 5m.")
    parser.add_argument(
        "--adjust-type",
        default="pre",
        choices=ADJUST_TYPE_CHOICES,
        help="RQData adjust_type for intraday bars. Default: pre.",
    )
    parser.add_argument(
        "--fields",
        nargs="+",
        default=list(DEFAULT_FIELDS),
        help="RQData fields. Default: open high low close volume total_turnover.",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Symbols per get_price call.")
    parser.add_argument(
        "--output",
        required=True,
        help="Output parquet path. Relative paths resolve from repo root.",
    )
    parser.add_argument(
        "--meta-output",
        help="Optional metadata JSON path. Defaults to <output>.meta.json beside the parquet.",
    )
    parser.add_argument(
        "--parts-dir",
        help="Optional batch checkpoint directory. Defaults to <output_stem>.parts beside the output parquet.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip batch files that already exist under --parts-dir and only download missing batches.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    rqdatac = _init_rqdatac_runtime(
        logger=logging.getLogger("market_data_platform.hk_assets.intraday_download"),
        load_env=True,
        error_cls=SystemExit,
        import_error_message="rqdatac is required. Install with: uv sync --extra rqdata",
    )

    download_hk_intraday_cache(args, rqdatac)


if __name__ == "__main__":
    main()
