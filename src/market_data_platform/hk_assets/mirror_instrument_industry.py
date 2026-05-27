from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol
from .asset_io import (
    _chunked,
    _dated_audit_record,
    _ensure_requested_fields,
    _field_coverage_template,
    _load_existing_dated_entry,
    _update_field_coverage,
    _write_dated_audit_csv,
    _write_dated_symbol_frame,
)
from .fetch_runtime import _retry_fetch
from .industry_ops import (
    _prepare_hk_instrument_industry_frame,
    _resolve_hk_industry_source,
    _resolve_hk_instrument_industry_level,
    _resolve_hk_snapshot_dates,
)
from .manifest_ops import _build_dated_manifest, _validate_dated_resume_inputs
from .models import DatedMirrorAuditRecord, DatedMirrorEntry, MirrorFetchError, MirrorQuotaError
from .package_api import _package_attr
from .request_groups import _resolve_symbols
from .shared import (
    _load_existing_text_list,
    _load_manifest,
    _normalize_absolute_date,
    _path_mtime_iso,
    _prepare_daily_output_dir,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)


DEFAULT_MIRROR_MAX_ATTEMPTS = _package_attr("DEFAULT_MIRROR_MAX_ATTEMPTS")
DEFAULT_MIRROR_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_BACKOFF_SECONDS")
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_MAX_BACKOFF_SECONDS")
DEFAULT_OUT_ROOT = _package_attr("DEFAULT_OUT_ROOT")
DEFAULT_BATCH_SIZE = _package_attr("DEFAULT_BATCH_SIZE")


def mirror_hk_instrument_industry(args, rqdatac) -> int:
    source = _resolve_hk_industry_source(args)
    level, fields = _resolve_hk_instrument_industry_level(args)
    symbols, symbol_metadata = _resolve_symbols(args)
    start_date = _normalize_absolute_date(args.start_date, label="--start-date")
    end_date = _normalize_absolute_date(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    snapshot_dates, snapshot_metadata = _resolve_hk_snapshot_dates(
        args,
        start_date=start_date,
        end_date=end_date,
    )
    resume = bool(getattr(args, "resume", False))
    skip_existing = bool(getattr(args, "skip_existing", False) or resume)
    max_attempts = max(1, int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1))
    backoff_seconds = float(getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS))
    max_backoff_seconds = float(
        getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
    )
    output_dir = _prepare_daily_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="instrument_industry",
        start_date=start_date,
        end_date=end_date,
        name=getattr(args, "name", None),
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "audit.csv"

    symbol_map = {_to_rqdata_symbol("hk", symbol): symbol for symbol in symbols}
    order_book_ids = list(symbol_map.keys())
    entries_by_symbol: dict[str, DatedMirrorEntry] = {}
    audit_by_symbol: dict[str, DatedMirrorAuditRecord] = {}
    frames_by_symbol: dict[str, list[pd.DataFrame]] = {}
    batches: list[dict[str, object]] = []
    columns: list[str] = []
    field_metadata = {
        "count": len(fields),
        "fields_file": [],
        "source": f"rqdatac_level_{level}",
        "base_fields": list(fields),
    }
    field_coverage = _field_coverage_template(fields)
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    quota_blocked = False

    def _record_entry(
        *,
        symbol: str,
        entry: DatedMirrorEntry,
        symbol_frame: pd.DataFrame,
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        error_text: str | None = None,
    ) -> None:
        nonlocal columns
        entries_by_symbol[symbol] = entry
        if not columns and not symbol_frame.empty:
            columns = symbol_frame.columns.tolist()
        _update_field_coverage(field_coverage, symbol_frame, fields=fields)
        audit_by_symbol[symbol] = _dated_audit_record(
            symbol=symbol,
            order_book_id=entry.order_book_id,
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=_path_mtime_iso(entry.path),
            error=error_text,
            entry=entry,
        )

    def _record_non_entry(
        *,
        symbol: str,
        order_book_id: str,
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        error_text: str | None = None,
    ) -> None:
        audit_by_symbol[symbol] = _dated_audit_record(
            symbol=symbol,
            order_book_id=order_book_id,
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=None,
            error=error_text,
            entry=None,
        )

    def _process_batch(batch_order_book_ids: list[str], query_date: str) -> None:
        nonlocal status, error, result_code, quota_blocked
        if not batch_order_book_ids or quota_blocked:
            return
        batch_started_at = _timestamp_now()
        try:
            payload, attempts = _retry_fetch(
                f"instrument industry fetch failed for {query_date}: {', '.join(batch_order_book_ids)}",
                lambda: rqdatac.get_instrument_industry(
                    batch_order_book_ids,
                    source=source,
                    level=level,
                    date=query_date,
                    market="hk",
                ),
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                max_backoff_seconds=max_backoff_seconds,
            )
        except MirrorQuotaError as exc:
            quota_blocked = True
            status = "stopped_quota"
            error = str(exc)
            result_code = max(result_code, 2)
            batches.append(
                {
                    "date": query_date,
                    "order_book_ids": len(batch_order_book_ids),
                    "rows": 0,
                    "status": "quota_blocked",
                    "attempts": exc.attempts,
                    "error": str(exc),
                }
            )
            return
        except MirrorFetchError as exc:
            if len(batch_order_book_ids) > 1:
                batches.append(
                    {
                        "date": query_date,
                        "order_book_ids": len(batch_order_book_ids),
                        "rows": 0,
                        "status": "split_after_error",
                        "attempts": exc.attempts,
                        "error": str(exc),
                    }
                )
                for order_book_id in batch_order_book_ids:
                    _process_batch([order_book_id], query_date)
                    if quota_blocked:
                        break
                return
            batches.append(
                {
                    "date": query_date,
                    "order_book_ids": 1,
                    "rows": 0,
                    "status": "failed",
                    "attempts": exc.attempts,
                    "error": str(exc),
                }
            )
            if status == "completed":
                status = "completed_with_failures"
            result_code = max(result_code, 1)
            return

        prepared = _prepare_hk_instrument_industry_frame(
            payload,
            symbol_map={order_book_id: symbol_map[order_book_id] for order_book_id in batch_order_book_ids},
            query_date=query_date,
        )
        prepared = _ensure_requested_fields(prepared, fields)
        batches.append(
            {
                "date": query_date,
                "order_book_ids": len(batch_order_book_ids),
                "rows": int(len(prepared)),
                "status": "completed",
                "attempts": attempts,
            }
        )
        if prepared.empty:
            return

        for order_book_id in batch_order_book_ids:
            symbol = symbol_map[order_book_id]
            symbol_frame = prepared[prepared["symbol"] == symbol].reset_index(drop=True)
            if symbol_frame.empty:
                continue
            frames_by_symbol.setdefault(symbol, []).append(symbol_frame)

    try:
        if resume:
            _validate_dated_resume_inputs(
                output_dir=output_dir,
                dataset_name="instrument_industry",
                fields=fields,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
            )
            manifest = _load_manifest(output_dir / "manifest.yml") or {}
            query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
            if isinstance(query, Mapping):
                if query.get("source") not in {None, source}:
                    raise SystemExit(
                        f"Resume target query mismatch for source: expected {source!r}, got {query.get('source')!r}."
                    )
                if query.get("level") not in {None, level}:
                    raise SystemExit(
                        f"Resume target query mismatch for level: expected {level!r}, got {query.get('level')!r}."
                    )
                if query.get("rebalance_frequency") not in {None, snapshot_metadata.get("rebalance_frequency")}:
                    raise SystemExit("Resume target query mismatch for rebalance_frequency.")
            existing_dates = _load_existing_text_list(output_dir / "dates.txt", strip=False)
            if existing_dates and list(existing_dates) != list(snapshot_dates):
                raise SystemExit("Resume target dates.txt does not match the requested date list.")

        _write_text_list(output_dir / "fields.txt", fields)
        _write_text_list(output_dir / "symbols.txt", symbols)
        _write_text_list(output_dir / "dates.txt", snapshot_dates)

        pending_order_book_ids: list[str] = []
        for order_book_id in order_book_ids:
            symbol = symbol_map[order_book_id]
            out_path = data_dir / f"{symbol}.parquet"
            if skip_existing and out_path.exists():
                try:
                    entry, symbol_frame = _load_existing_dated_entry(
                        out_path,
                        date_column="date",
                        fields=fields,
                    )
                except Exception:
                    pending_order_book_ids.append(order_book_id)
                    continue
                _record_entry(
                    symbol=symbol,
                    entry=entry,
                    symbol_frame=symbol_frame,
                    record_status="skipped_existing",
                    attempts=0,
                    started_at_value=None,
                    finished_at_value=_path_mtime_iso(out_path),
                )
                continue
            pending_order_book_ids.append(order_book_id)

        for query_date in snapshot_dates:
            for batch_order_book_ids in _chunked(
                pending_order_book_ids,
                getattr(args, "batch_size", DEFAULT_BATCH_SIZE),
            ):
                _process_batch(batch_order_book_ids, query_date)
                if quota_blocked:
                    break
            if quota_blocked:
                break
            if result_code == 1 and status == "completed":
                status = "completed_with_failures"
    except Exception as exc:
        status = "failed"
        error = str(exc)
        result_code = max(result_code, 1)
        raise
    finally:
        finished_at = _timestamp_now()
        for order_book_id in pending_order_book_ids if "pending_order_book_ids" in locals() else order_book_ids:
            symbol = symbol_map[order_book_id]
            if symbol in audit_by_symbol:
                continue
            frames = frames_by_symbol.get(symbol) or []
            if frames:
                combined = pd.concat(frames, ignore_index=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last")
                combined = combined.sort_values(["date"]).reset_index(drop=True)
                entry = _write_dated_symbol_frame(data_dir, combined, date_column="date")
                _record_entry(
                    symbol=symbol,
                    entry=entry,
                    symbol_frame=combined,
                    record_status="written",
                    attempts=0,
                    started_at_value=started_at,
                    finished_at_value=finished_at,
                    error_text=error if quota_blocked else None,
                )
                continue
            _record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="quota_blocked" if quota_blocked else "missing_remote",
                attempts=0,
                started_at_value=None,
                finished_at_value=finished_at,
                error_text=error if quota_blocked else None,
            )

        audit_records = [audit_by_symbol[symbol] for symbol in symbols]
        _write_dated_audit_csv(audit_path, audit_records)
        manifest = _build_dated_manifest(
            dataset_name="instrument_industry",
            api_name="rqdatac.get_instrument_industry",
            output_dir=output_dir,
            fields=fields,
            field_metadata=field_metadata,
            symbol_metadata=symbol_metadata,
            symbols_requested=symbols,
            entries=[entries_by_symbol[symbol] for symbol in symbols if symbol in entries_by_symbol],
            missing_symbols=[item.symbol for item in audit_records if item.status == "missing_remote"],
            start_date=start_date,
            end_date=end_date,
            date_column="date",
            batches=batches,
            columns=columns,
            audit_file=audit_path,
            audit_records=audit_records,
            field_coverage=list(field_coverage.values()),
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            error=error,
            config_ref=getattr(args, "config", None),
        )
        manifest_query = manifest.get("query", {})
        if isinstance(manifest_query, dict):
            manifest_query["source"] = source
            manifest_query["level"] = level
            manifest_query["rebalance_frequency"] = snapshot_metadata.get("rebalance_frequency")
            manifest_query["dates_count"] = len(snapshot_dates)
            manifest_query["dates_file"] = str(output_dir / "dates.txt")
        manifest["date_source"] = snapshot_metadata
        _write_manifest(output_dir / "manifest.yml", manifest)

    totals = {
        "files": len(entries_by_symbol),
        "symbols": len(entries_by_symbol),
        "rows": sum(item.rows for item in entries_by_symbol.values()),
        "bytes": sum(item.total_bytes for item in entries_by_symbol.values()),
    }
    print(
        f"Wrote instrument_industry mirror to {output_dir} "
        f"({totals['symbols']} symbols, {totals['files']} files, {totals['rows']} rows, {totals['bytes']} bytes, status={status})"
    )
    return result_code
