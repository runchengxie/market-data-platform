from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol
from .asset_io import (
    _daily_audit_record,
    _ensure_requested_fields,
    _field_coverage_template,
    _load_existing_daily_entry,
    _prepare_daily_batch_asset_frame,
    _update_field_coverage,
    _write_daily_audit_csv,
    _write_daily_symbol_frame,
)
from .fetch_runtime import _retry_fetch
from .fetch_runtime import _looks_like_provider_permission_error
from .manifest_ops import _build_daily_manifest, _validate_daily_resume_inputs
from .mirror_workflow import (
    _failed_batch_summary,
    _quota_blocked_batch_summary,
    _record_pending_quota_blocked,
    _run_partitioned_mirror_batches,
    _split_after_error_batch_summary,
    _status_after_batch_failure,
)
from .models import DailyMirrorAuditRecord, DailyMirrorEntry, MirrorFetchError, MirrorQuotaError
from .package_api import _package_attr
from .request_groups import _resolve_symbols
from .shared import (
    _normalize_absolute_date,
    _path_mtime_iso,
    _prepare_daily_output_dir,
    _resolve_daily_fields,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)

DEFAULT_MIRROR_MAX_ATTEMPTS = _package_attr("DEFAULT_MIRROR_MAX_ATTEMPTS")
DEFAULT_MIRROR_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_BACKOFF_SECONDS")
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_MAX_BACKOFF_SECONDS")
DEFAULT_BATCH_SIZE = _package_attr("DEFAULT_BATCH_SIZE")
DEFAULT_OUT_ROOT = _package_attr("DEFAULT_OUT_ROOT")
PROVIDER_PERMISSION_EXIT_CODE = 78


@dataclass(frozen=True)
class _DailyMirrorContext:
    fields: Sequence[str]
    field_metadata: Mapping[str, object]
    symbols: list[str]
    symbol_metadata: dict[str, object]
    start_date: str
    end_date: str
    frequency: str
    adjust_type: str | None
    skip_suspended: bool
    resume: bool
    skip_existing: bool
    batch_size: int
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float
    output_dir: Path
    data_dir: Path
    audit_path: Path
    symbol_map: dict[str, str]
    order_book_ids: list[str]
    config_ref: object
    provider_permission_preflight: bool
    preflight_symbol: str | None


def _prepare_daily_mirror_context(args) -> _DailyMirrorContext:
    fields, field_metadata = _resolve_daily_fields(args)
    symbols, symbol_metadata = _resolve_symbols(args)
    start_date = _normalize_absolute_date(args.start_date, label="--start-date")
    end_date = _normalize_absolute_date(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    adjust_type = getattr(args, "adjust_type", None)
    if adjust_type is not None:
        adjust_type = str(adjust_type).strip() or None
    skip_suspended_raw = getattr(args, "skip_suspended", None)
    skip_suspended = True if skip_suspended_raw is None else bool(skip_suspended_raw)

    resume = bool(getattr(args, "resume", False))
    output_dir = _prepare_daily_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="daily",
        start_date=start_date,
        end_date=end_date,
        name=getattr(args, "name", None),
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    symbol_map = {_to_rqdata_symbol("hk", symbol): symbol for symbol in symbols}
    return _DailyMirrorContext(
        fields=fields,
        field_metadata=field_metadata,
        symbols=symbols,
        symbol_metadata=symbol_metadata,
        start_date=start_date,
        end_date=end_date,
        frequency="1d",
        adjust_type=adjust_type,
        skip_suspended=skip_suspended,
        resume=resume,
        skip_existing=bool(getattr(args, "skip_existing", False) or resume),
        batch_size=int(getattr(args, "batch_size", DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE),
        max_attempts=max(
            1,
            int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1),
        ),
        backoff_seconds=float(
            getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS)
        ),
        max_backoff_seconds=float(
            getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
        ),
        output_dir=output_dir,
        data_dir=data_dir,
        audit_path=output_dir / "audit.csv",
        symbol_map=symbol_map,
        order_book_ids=list(symbol_map.keys()),
        config_ref=getattr(args, "config", None),
        provider_permission_preflight=bool(
            getattr(args, "provider_permission_preflight", False)
        ),
        preflight_symbol=str(getattr(args, "preflight_symbol", "") or "").strip() or None,
    )


def _fetch_daily_batch_payload(
    *,
    rqdatac,
    batch_order_book_ids: Sequence[str],
    context: _DailyMirrorContext,
):
    request_target: str | list[str]
    if len(batch_order_book_ids) == 1:
        request_target = batch_order_book_ids[0]
    else:
        request_target = list(batch_order_book_ids)
    kwargs = {
        "fields": list(context.fields),
        "skip_suspended": context.skip_suspended,
        "market": "hk",
    }
    if context.adjust_type:
        kwargs["adjust_type"] = context.adjust_type
    return rqdatac.get_price(
        request_target,
        context.start_date,
        context.end_date,
        context.frequency,
        **kwargs,
    )


def _write_prepared_daily_batch(
    *,
    prepared: pd.DataFrame,
    batch_order_book_ids: Sequence[str],
    context: _DailyMirrorContext,
    attempts: int,
    batch_started_at: str,
    batch_finished_at: str,
    record_entry,
    record_non_entry,
) -> dict[str, object]:
    if prepared.empty:
        for order_book_id in batch_order_book_ids:
            symbol = context.symbol_map[order_book_id]
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="missing_remote",
                attempts=attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
            )
        return {
            "order_book_ids": len(batch_order_book_ids),
            "rows": 0,
            "symbols_written": 0,
            "symbols_missing_remote": len(batch_order_book_ids),
            "status": "empty",
            "attempts": attempts,
        }

    batch_symbols_written = 0
    batch_symbols_missing = 0
    for order_book_id in batch_order_book_ids:
        symbol = context.symbol_map[order_book_id]
        symbol_frame = prepared[prepared["symbol"] == symbol].reset_index(drop=True)
        if symbol_frame.empty:
            batch_symbols_missing += 1
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="missing_remote",
                attempts=attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
            )
            continue
        entry = _write_daily_symbol_frame(context.data_dir, symbol_frame)
        record_entry(
            symbol=symbol,
            entry=entry,
            symbol_frame=symbol_frame,
            record_status="written",
            attempts=attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
        )
        batch_symbols_written += 1

    return {
        "order_book_ids": len(batch_order_book_ids),
        "rows": int(len(prepared)),
        "symbols_written": batch_symbols_written,
        "symbols_missing_remote": batch_symbols_missing,
        "status": "completed",
        "attempts": attempts,
    }


def _process_daily_batch(
    *,
    batch_order_book_ids: list[str],
    rqdatac,
    context: _DailyMirrorContext,
    status: str,
    error: str | None,
    result_code: int,
    quota_blocked: bool,
    columns: list[str],
    batches: list[dict[str, object]],
    audit_by_symbol: Mapping[str, DailyMirrorAuditRecord],
    record_entry,
    record_non_entry,
) -> tuple[str, str | None, int, bool, list[str]]:
    if not batch_order_book_ids or quota_blocked:
        return status, error, result_code, quota_blocked, columns

    batch_started_at = _timestamp_now()
    try:
        payload, attempts = _retry_fetch(
            f"daily fetch failed for {', '.join(batch_order_book_ids)}",
            lambda: _fetch_daily_batch_payload(
                rqdatac=rqdatac,
                batch_order_book_ids=batch_order_book_ids,
                context=context,
            ),
            max_attempts=context.max_attempts,
            backoff_seconds=context.backoff_seconds,
            max_backoff_seconds=context.max_backoff_seconds,
        )
    except MirrorQuotaError as exc:
        batch_finished_at = _timestamp_now()
        quota_blocked = True
        status = "stopped_quota"
        error = str(exc)
        result_code = max(result_code, 2)
        for order_book_id in batch_order_book_ids:
            symbol = context.symbol_map[order_book_id]
            if symbol in audit_by_symbol:
                continue
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="quota_blocked",
                attempts=exc.attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                error_text=str(exc),
            )
        batches.append(
            _quota_blocked_batch_summary(
                order_book_ids=len(batch_order_book_ids),
                symbols_missing_remote=len(batch_order_book_ids),
                attempts=exc.attempts,
                dropped_fields=(),
                error=str(exc),
            )
        )
        return status, error, result_code, quota_blocked, columns
    except MirrorFetchError as exc:
        batch_finished_at = _timestamp_now()
        if len(batch_order_book_ids) > 1:
            batches.append(
                _split_after_error_batch_summary(
                    order_book_ids=len(batch_order_book_ids),
                    attempts=exc.attempts,
                    error=str(exc),
                )
            )
            for order_book_id in batch_order_book_ids:
                status, error, result_code, quota_blocked, columns = _process_daily_batch(
                    batch_order_book_ids=[order_book_id],
                    rqdatac=rqdatac,
                    context=context,
                    status=status,
                    error=error,
                    result_code=result_code,
                    quota_blocked=quota_blocked,
                    columns=columns,
                    batches=batches,
                    audit_by_symbol=audit_by_symbol,
                    record_entry=record_entry,
                    record_non_entry=record_non_entry,
                )
                if quota_blocked:
                    break
            return status, error, result_code, quota_blocked, columns

        order_book_id = batch_order_book_ids[0]
        symbol = context.symbol_map[order_book_id]
        record_non_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="failed",
            attempts=exc.attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
            error_text=str(exc),
        )
        batches.append(
            _failed_batch_summary(
                order_book_ids=1,
                attempts=exc.attempts,
                dropped_fields=(),
                error=str(exc),
            )
        )
        result_code = max(result_code, 1)
        status = _status_after_batch_failure(status, result_code)
        return status, error, result_code, quota_blocked, columns

    batch_finished_at = _timestamp_now()
    batch_symbol_map = {
        order_book_id: context.symbol_map[order_book_id]
        for order_book_id in batch_order_book_ids
    }
    prepared = _prepare_daily_batch_asset_frame(payload, symbol_map=batch_symbol_map)
    prepared = _ensure_requested_fields(prepared, context.fields)
    if not prepared.empty and not columns:
        columns = prepared.columns.tolist()
    batches.append(
        _write_prepared_daily_batch(
            prepared=prepared,
            batch_order_book_ids=batch_order_book_ids,
            context=context,
            attempts=attempts,
            batch_started_at=batch_started_at,
            batch_finished_at=batch_finished_at,
            record_entry=record_entry,
            record_non_entry=record_non_entry,
        )
    )
    return status, error, result_code, quota_blocked, columns


def _collect_pending_daily_order_book_ids(
    *,
    context: _DailyMirrorContext,
    record_entry,
) -> list[str]:
    pending_order_book_ids: list[str] = []
    for order_book_id in context.order_book_ids:
        symbol = context.symbol_map[order_book_id]
        out_path = context.data_dir / f"{symbol}.parquet"
        if context.skip_existing and out_path.exists():
            try:
                entry, symbol_frame = _load_existing_daily_entry(
                    out_path,
                    fields=context.fields,
                )
            except Exception:
                pending_order_book_ids.append(order_book_id)
                continue
            record_entry(
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
    return pending_order_book_ids


def _select_preflight_order_book_id(
    *,
    context: _DailyMirrorContext,
    pending_order_book_ids: Sequence[str],
) -> str | None:
    if not pending_order_book_ids:
        return None
    if context.preflight_symbol:
        candidate = _to_rqdata_symbol("hk", context.preflight_symbol)
        if candidate in context.symbol_map:
            return candidate
    return str(pending_order_book_ids[0])


def _run_provider_permission_preflight(
    *,
    pending_order_book_ids: Sequence[str],
    rqdatac,
    context: _DailyMirrorContext,
    audit_by_symbol: Mapping[str, DailyMirrorAuditRecord],
    batches: list[dict[str, object]],
    record_non_entry,
) -> str | None:
    if not context.provider_permission_preflight:
        return None
    preflight_order_book_id = _select_preflight_order_book_id(
        context=context,
        pending_order_book_ids=pending_order_book_ids,
    )
    if preflight_order_book_id is None:
        return None

    started_at = _timestamp_now()
    try:
        _fetch_daily_batch_payload(
            rqdatac=rqdatac,
            batch_order_book_ids=[preflight_order_book_id],
            context=context,
        )
    except Exception as exc:
        if not _looks_like_provider_permission_error(exc):
            return None
        finished_at = _timestamp_now()
        message = f"daily permission preflight failed for {preflight_order_book_id}: {exc}"
        for order_book_id in pending_order_book_ids:
            symbol = context.symbol_map[order_book_id]
            if symbol in audit_by_symbol:
                continue
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="provider_permission_blocked",
                attempts=1,
                started_at_value=started_at,
                finished_at_value=finished_at,
                error_text=message,
            )
        batches.append(
            {
                "order_book_ids": len(pending_order_book_ids),
                "rows": 0,
                "symbols_written": 0,
                "symbols_missing_remote": len(pending_order_book_ids),
                "status": "provider_permission_blocked",
                "attempts": 1,
                "error": message,
            }
        )
        return message
    return None


def _finalize_daily_mirror_outputs(
    *,
    context: _DailyMirrorContext,
    entries_by_symbol: Mapping[str, DailyMirrorEntry],
    audit_by_symbol: Mapping[str, DailyMirrorAuditRecord],
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    field_coverage: Mapping[str, Mapping[str, object]],
    started_at: str,
    status: str,
    error: str | None,
    record_non_entry,
) -> None:
    finished_at = _timestamp_now()
    for order_book_id in context.order_book_ids:
        symbol = context.symbol_map[order_book_id]
        if symbol in audit_by_symbol:
            continue
        record_non_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="failed",
            attempts=0,
            started_at_value=None,
            finished_at_value=finished_at,
            error_text=error or "missing audit status",
        )
    audit_records = [audit_by_symbol[symbol] for symbol in context.symbols]
    _write_daily_audit_csv(context.audit_path, audit_records)
    manifest = _build_daily_manifest(
        dataset_name="daily",
        api_name="rqdatac.get_price",
        output_dir=context.output_dir,
        fields=context.fields,
        field_metadata=context.field_metadata,
        symbol_metadata=context.symbol_metadata,
        symbols_requested=context.symbols,
        entries=[
            entries_by_symbol[symbol]
            for symbol in context.symbols
            if symbol in entries_by_symbol
        ],
        missing_symbols=[item.symbol for item in audit_records if item.status == "missing_remote"],
        start_date=context.start_date,
        end_date=context.end_date,
        frequency=context.frequency,
        adjust_type=context.adjust_type,
        skip_suspended=context.skip_suspended,
        batches=batches,
        columns=columns,
        audit_file=context.audit_path,
        audit_records=audit_records,
        field_coverage=list(field_coverage.values()),
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        config_ref=context.config_ref,
    )
    _write_manifest(context.output_dir / "manifest.yml", manifest)


def mirror_hk_daily(args, rqdatac) -> int:
    context = _prepare_daily_mirror_context(args)
    entries_by_symbol: dict[str, DailyMirrorEntry] = {}
    audit_by_symbol: dict[str, DailyMirrorAuditRecord] = {}
    batches: list[dict[str, object]] = []
    columns: list[str] = []
    field_coverage = _field_coverage_template(context.fields)
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    quota_blocked = False

    def _record_entry(
        *,
        symbol: str,
        entry: DailyMirrorEntry,
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
        _update_field_coverage(field_coverage, symbol_frame, fields=context.fields)
        audit_by_symbol[symbol] = _daily_audit_record(
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
        audit_by_symbol[symbol] = _daily_audit_record(
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

    def _process_batch(batch_order_book_ids: list[str]) -> None:
        nonlocal status, error, result_code, quota_blocked, columns
        status, error, result_code, quota_blocked, columns = _process_daily_batch(
            batch_order_book_ids=batch_order_book_ids,
            rqdatac=rqdatac,
            context=context,
            status=status,
            error=error,
            result_code=result_code,
            quota_blocked=quota_blocked,
            columns=columns,
            batches=batches,
            audit_by_symbol=audit_by_symbol,
            record_entry=_record_entry,
            record_non_entry=_record_non_entry,
        )

    try:
        if context.resume:
            _validate_daily_resume_inputs(
                output_dir=context.output_dir,
                dataset_name="daily",
                fields=context.fields,
                symbols=context.symbols,
                start_date=context.start_date,
                end_date=context.end_date,
                frequency=context.frequency,
                adjust_type=context.adjust_type,
                skip_suspended=context.skip_suspended,
            )

        _write_text_list(context.output_dir / "fields.txt", list(context.fields))
        _write_text_list(context.output_dir / "symbols.txt", context.symbols)

        pending_order_book_ids = _collect_pending_daily_order_book_ids(
            context=context,
            record_entry=_record_entry,
        )
        provider_permission_error = _run_provider_permission_preflight(
            pending_order_book_ids=pending_order_book_ids,
            rqdatac=rqdatac,
            context=context,
            audit_by_symbol=audit_by_symbol,
            batches=batches,
            record_non_entry=_record_non_entry,
        )
        if provider_permission_error is not None:
            status = "blocked_provider_permission"
            error = provider_permission_error
            result_code = max(result_code, PROVIDER_PERMISSION_EXIT_CODE)
            pending_order_book_ids = []

        def _quota_blocked() -> bool:
            return quota_blocked

        def _on_quota_blocked() -> None:
            _record_pending_quota_blocked(
                pending_order_book_ids=pending_order_book_ids,
                audit_by_symbol=audit_by_symbol,
                symbol_map=context.symbol_map,
                error=error,
                record_non_entry=_record_non_entry,
            )

        def _on_completed_without_quota() -> None:
            nonlocal status
            status = _status_after_batch_failure(status, result_code)

        _run_partitioned_mirror_batches(
            pending_items=pending_order_book_ids,
            batch_size=context.batch_size,
            process_batch=_process_batch,
            quota_blocked=_quota_blocked,
            on_quota_blocked=_on_quota_blocked,
            on_completed_without_quota=_on_completed_without_quota,
            on_exception=lambda exc: None,
            on_finalize=lambda: None,
        )
    except Exception as exc:
        status = "failed"
        error = str(exc)
        result_code = max(result_code, 1)
        raise
    finally:
        _finalize_daily_mirror_outputs(
            context=context,
            entries_by_symbol=entries_by_symbol,
            audit_by_symbol=audit_by_symbol,
            batches=batches,
            columns=columns,
            field_coverage=field_coverage,
            started_at=started_at,
            status=status,
            error=error,
            record_non_entry=_record_non_entry,
        )

    totals = {
        "files": len(entries_by_symbol),
        "symbols": len(entries_by_symbol),
        "rows": sum(item.rows for item in entries_by_symbol.values()),
        "bytes": sum(item.total_bytes for item in entries_by_symbol.values()),
    }
    print(
        f"Wrote daily mirror to {context.output_dir} "
        f"({totals['symbols']} symbols, {totals['files']} files, {totals['rows']} rows, {totals['bytes']} bytes, status={status})"
    )
    return result_code
