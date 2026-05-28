from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol

from .asset_io import (
    _audit_record,
    _chunked,
    _dated_audit_record,
    _ensure_requested_fields,
    _field_coverage_template,
    _load_existing_dated_entry,
    _load_existing_entry,
    _prepare_asset_frame,
    _prepare_dated_asset_frame,
    _update_field_coverage,
    _write_audit_csv,
    _write_dated_audit_csv,
    _write_dated_symbol_frame,
    _write_symbol_frame,
)
from .fetch_runtime import _extract_invalid_field_name, _retry_fetch
from .manifest_ops import (
    _build_dated_manifest,
    _build_manifest,
    _validate_dated_resume_inputs,
    _validate_resume_inputs,
)
from .models import (
    DatedMirrorAuditRecord,
    DatedMirrorEntry,
    MirrorAuditRecord,
    MirrorEntry,
    MirrorFetchError,
    MirrorQuotaError,
)
from .package_api import _package_attr
from .request_groups import _build_default_dated_request_groups, _resolve_symbols
from .shared import (
    _normalize_absolute_date,
    _path_mtime_iso,
    _prepare_daily_output_dir,
    _prepare_output_dir,
    _resolve_fields,
    _timestamp_now,
    _write_manifest,
    _write_text_list,
)

DEFAULT_BATCH_SIZE = _package_attr("DEFAULT_BATCH_SIZE")
DEFAULT_MIRROR_MAX_ATTEMPTS = _package_attr("DEFAULT_MIRROR_MAX_ATTEMPTS")
DEFAULT_MIRROR_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_BACKOFF_SECONDS")
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_MAX_BACKOFF_SECONDS")
DEFAULT_OUT_ROOT = _package_attr("DEFAULT_OUT_ROOT")


@dataclass(frozen=True)
class _MirrorFetchPolicy:
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float


@dataclass(frozen=True)
class _DatedMirrorContext:
    symbols: list[str]
    symbol_metadata: dict[str, object]
    start_date: str
    end_date: str
    resume: bool
    skip_existing: bool
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float
    output_dir: Path
    data_dir: Path
    audit_path: Path
    request_id_metadata: dict[str, dict[str, str | None]]
    request_ids_by_symbol: dict[str, list[str]]
    primary_order_book_id_by_symbol: dict[str, str]
    symbol_map: dict[str, str]


@dataclass(frozen=True)
class _MirrorContext:
    fields: Sequence[str]
    field_metadata: Mapping[str, object]
    symbols: list[str]
    symbol_metadata: dict[str, object]
    resume: bool
    skip_existing: bool
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float
    output_dir: Path
    data_dir: Path
    audit_path: Path
    symbol_map: dict[str, str]
    order_book_ids: list[str]


def _prepare_mirror_context(*, args, dataset_name: str) -> _MirrorContext:
    fields, field_metadata = _resolve_fields(args)
    symbols, symbol_metadata = _resolve_symbols(args)
    resume = bool(getattr(args, "resume", False))
    output_dir = _prepare_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name=dataset_name,
        start_quarter=args.start_quarter,
        end_quarter=args.end_quarter,
        statements=args.statements,
        name=getattr(args, "name", None),
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    symbol_map = {_to_rqdata_symbol("hk", symbol): symbol for symbol in symbols}
    return _MirrorContext(
        fields=fields,
        field_metadata=field_metadata,
        symbols=symbols,
        symbol_metadata=symbol_metadata,
        resume=resume,
        skip_existing=bool(getattr(args, "skip_existing", False) or resume),
        max_attempts=max(1, int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1)),
        backoff_seconds=float(getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS)),
        max_backoff_seconds=float(
            getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
        ),
        output_dir=output_dir,
        data_dir=data_dir,
        audit_path=output_dir / "audit.csv",
        symbol_map=symbol_map,
        order_book_ids=list(symbol_map.keys()),
    )


def _prepare_dated_mirror_context(
    *,
    args,
    dataset_name: str,
    resolve_request_groups,
) -> _DatedMirrorContext:
    symbols, symbol_metadata = _resolve_symbols(args)
    start_date = _normalize_absolute_date(args.start_date, label="--start-date")
    end_date = _normalize_absolute_date(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    resume = bool(getattr(args, "resume", False))
    output_dir = _prepare_daily_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name=dataset_name,
        start_date=start_date,
        end_date=end_date,
        name=getattr(args, "name", None),
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if resolve_request_groups is not None:
        request_groups, request_id_metadata, request_group_metadata = resolve_request_groups(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            args=args,
        )
    else:
        (
            request_groups,
            request_id_metadata,
            request_group_metadata,
        ) = _build_default_dated_request_groups(symbols)
    if request_group_metadata:
        symbol_metadata = dict(symbol_metadata)
        symbol_metadata["request_groups"] = dict(request_group_metadata)

    request_ids_by_symbol = {group.symbol: list(group.request_ids) for group in request_groups}
    primary_order_book_id_by_symbol = {
        group.symbol: (
            next((item for item in group.order_book_ids if str(item).strip()), None)
            or next((item for item in group.request_ids if str(item).strip()), None)
            or _to_rqdata_symbol("hk", group.symbol)
        )
        for group in request_groups
    }
    symbol_map: dict[str, str] = {}
    for group in request_groups:
        for request_id in (*group.request_ids, *group.order_book_ids):
            text = str(request_id or "").strip()
            if text:
                symbol_map[text] = group.symbol

    return _DatedMirrorContext(
        symbols=symbols,
        symbol_metadata=symbol_metadata,
        start_date=start_date,
        end_date=end_date,
        resume=resume,
        skip_existing=bool(getattr(args, "skip_existing", False) or resume),
        max_attempts=max(1, int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1)),
        backoff_seconds=float(getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS)),
        max_backoff_seconds=float(
            getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
        ),
        output_dir=output_dir,
        data_dir=data_dir,
        audit_path=output_dir / "audit.csv",
        request_id_metadata=request_id_metadata,
        request_ids_by_symbol=request_ids_by_symbol,
        primary_order_book_id_by_symbol=primary_order_book_id_by_symbol,
        symbol_map=symbol_map,
    )


def _collect_pending_mirror_items(
    *,
    items: Sequence[str],
    data_dir: Path,
    skip_existing: bool,
    item_to_symbol: Callable[[str], str],
    load_existing: Callable[[Path], tuple[object, pd.DataFrame]],
    record_entry: Callable[..., None],
) -> list[str]:
    pending_items: list[str] = []
    for item in items:
        symbol = item_to_symbol(item)
        out_path = data_dir / f"{symbol}.parquet"
        if skip_existing and out_path.exists():
            try:
                entry, symbol_frame = load_existing(out_path)
            except Exception:
                pending_items.append(item)
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
        pending_items.append(item)
    return pending_items


def _run_partitioned_mirror_batches(
    *,
    pending_items: Sequence[str],
    batch_size: int,
    process_batch: Callable[[list[str]], None],
    quota_blocked: Callable[[], bool],
    on_quota_blocked: Callable[[], None],
    on_completed_without_quota: Callable[[], None],
    on_exception: Callable[[Exception], None],
    on_finalize: Callable[[], None],
) -> None:
    try:
        for batch_items in _chunked(pending_items, batch_size):
            process_batch(list(batch_items))
            if quota_blocked():
                break
        if quota_blocked():
            on_quota_blocked()
        else:
            on_completed_without_quota()
    except Exception as exc:
        on_exception(exc)
        raise
    finally:
        on_finalize()


def _retry_mirror_fetch(
    label: str,
    fetch_callable: Callable[[], object],
    *,
    policy: _MirrorFetchPolicy,
):
    return _retry_fetch(
        label,
        fetch_callable,
        max_attempts=policy.max_attempts,
        backoff_seconds=policy.backoff_seconds,
        max_backoff_seconds=policy.max_backoff_seconds,
    )


def _fetch_with_field_fallback(
    *,
    dataset_name: str,
    request_label: str,
    fields: Sequence[str],
    fetch_payload: Callable[[list[str]], object],
    prepare_payload: Callable[[object], pd.DataFrame],
    policy: _MirrorFetchPolicy,
) -> tuple[pd.DataFrame, int, list[str]]:
    active_fields = list(fields)
    dropped_fields: list[str] = []
    total_attempts = 0
    while True:
        label = f"{dataset_name} fetch failed for {request_label}"
        try:
            payload, attempts = _retry_mirror_fetch(
                label,
                partial(fetch_payload, list(active_fields)),
                policy=policy,
            )
        except MirrorQuotaError as exc:
            raise MirrorQuotaError(str(exc), attempts=total_attempts + exc.attempts) from exc
        except MirrorFetchError as exc:
            invalid_field = _extract_invalid_field_name(str(exc))
            total_attempts += exc.attempts
            if invalid_field and invalid_field in active_fields and len(active_fields) > 1:
                active_fields = [field for field in active_fields if field != invalid_field]
                dropped_fields.append(invalid_field)
                continue
            raise MirrorFetchError(str(exc), attempts=total_attempts) from exc

        total_attempts += attempts
        prepared = _ensure_requested_fields(prepare_payload(payload), fields)
        return prepared, total_attempts, dropped_fields


def _write_dated_prepared_batch_symbols(
    *,
    prepared: pd.DataFrame,
    batch_symbols: Sequence[str],
    batch_request_ids: Sequence[str],
    data_dir: Path,
    date_column: str,
    primary_order_book_id_by_symbol: Mapping[str, str],
    attempts: int,
    batch_started_at: str,
    batch_finished_at: str,
    dropped_fields: Sequence[str],
    record_entry: Callable[..., None],
    record_non_entry: Callable[..., None],
) -> dict[str, object]:
    if prepared.empty:
        for symbol in batch_symbols:
            record_non_entry(
                symbol=symbol,
                order_book_id=primary_order_book_id_by_symbol[symbol],
                record_status="missing_remote",
                attempts=attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                dropped_fields=dropped_fields,
            )
        return {
            "order_book_ids": len(batch_request_ids),
            "rows": 0,
            "symbols_written": 0,
            "symbols_missing_remote": len(batch_symbols),
            "status": "empty",
            "attempts": attempts,
            "dropped_fields": list(dropped_fields),
        }

    batch_symbols_written = 0
    batch_symbols_missing = 0
    for symbol in batch_symbols:
        symbol_frame = prepared[prepared["symbol"] == symbol].reset_index(drop=True)
        if symbol_frame.empty:
            batch_symbols_missing += 1
            record_non_entry(
                symbol=symbol,
                order_book_id=primary_order_book_id_by_symbol[symbol],
                record_status="missing_remote",
                attempts=attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                dropped_fields=dropped_fields,
            )
            continue
        entry = _write_dated_symbol_frame(data_dir, symbol_frame, date_column=date_column)
        record_entry(
            symbol=symbol,
            entry=entry,
            symbol_frame=symbol_frame,
            record_status="written",
            attempts=attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
            dropped_fields=dropped_fields,
        )
        batch_symbols_written += 1

    return {
        "order_book_ids": len(batch_request_ids),
        "rows": int(len(prepared)),
        "symbols_written": batch_symbols_written,
        "symbols_missing_remote": batch_symbols_missing,
        "status": "completed",
        "attempts": attempts,
        "dropped_fields": list(dropped_fields),
    }


def _write_prepared_batch_order_book_ids(
    *,
    prepared: pd.DataFrame,
    batch_order_book_ids: Sequence[str],
    data_dir: Path,
    symbol_map: Mapping[str, str],
    attempts: int,
    batch_started_at: str,
    batch_finished_at: str,
    dropped_fields: Sequence[str],
    record_entry: Callable[..., None],
    record_non_entry: Callable[..., None],
) -> dict[str, object]:
    if prepared.empty:
        for order_book_id in batch_order_book_ids:
            symbol = symbol_map[order_book_id]
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="missing_remote",
                attempts=attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                dropped_fields=dropped_fields,
            )
        return {
            "order_book_ids": len(batch_order_book_ids),
            "rows": 0,
            "symbols_written": 0,
            "symbols_missing_remote": len(batch_order_book_ids),
            "status": "empty",
            "attempts": attempts,
            "dropped_fields": list(dropped_fields),
        }

    batch_symbols_written = 0
    batch_symbols_missing = 0
    for order_book_id in batch_order_book_ids:
        symbol = symbol_map[order_book_id]
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
                dropped_fields=dropped_fields,
            )
            continue
        entry = _write_symbol_frame(data_dir, symbol_frame)
        record_entry(
            symbol=symbol,
            entry=entry,
            symbol_frame=symbol_frame,
            record_status="written",
            attempts=attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
            dropped_fields=dropped_fields,
        )
        batch_symbols_written += 1

    return {
        "order_book_ids": len(batch_order_book_ids),
        "rows": int(len(prepared)),
        "symbols_written": batch_symbols_written,
        "symbols_missing_remote": batch_symbols_missing,
        "status": "completed",
        "attempts": attempts,
        "dropped_fields": list(dropped_fields),
    }


def _quota_blocked_batch_summary(
    *,
    order_book_ids: int,
    symbols_missing_remote: int,
    attempts: int,
    dropped_fields: Sequence[str],
    error: str,
) -> dict[str, object]:
    return {
        "order_book_ids": order_book_ids,
        "rows": 0,
        "symbols_written": 0,
        "symbols_missing_remote": symbols_missing_remote,
        "status": "quota_blocked",
        "attempts": attempts,
        "dropped_fields": list(dropped_fields),
        "error": error,
    }


def _split_after_error_batch_summary(
    *,
    order_book_ids: int,
    attempts: int,
    error: str,
) -> dict[str, object]:
    return {
        "order_book_ids": order_book_ids,
        "rows": 0,
        "symbols_written": 0,
        "symbols_missing_remote": 0,
        "status": "split_after_error",
        "attempts": attempts,
        "error": error,
    }


def _failed_batch_summary(
    *,
    order_book_ids: int,
    attempts: int,
    dropped_fields: Sequence[str],
    error: str,
) -> dict[str, object]:
    return {
        "order_book_ids": order_book_ids,
        "rows": 0,
        "symbols_written": 0,
        "symbols_missing_remote": 0,
        "status": "failed",
        "attempts": attempts,
        "dropped_fields": list(dropped_fields),
        "error": error,
    }


def _status_after_batch_failure(status: str, result_code: int) -> str:
    if result_code == 1 and status == "completed":
        return "completed_with_failures"
    return status


def _record_dated_pending_quota_blocked(
    *,
    pending_symbols: Sequence[str],
    audit_by_symbol: Mapping[str, DatedMirrorAuditRecord],
    primary_order_book_id_by_symbol: Mapping[str, str],
    error: str | None,
    record_non_entry: Callable[..., None],
) -> None:
    quota_finished_at = _timestamp_now()
    for symbol in pending_symbols:
        if symbol in audit_by_symbol:
            continue
        record_non_entry(
            symbol=symbol,
            order_book_id=primary_order_book_id_by_symbol[symbol],
            record_status="quota_blocked",
            attempts=0,
            started_at_value=None,
            finished_at_value=quota_finished_at,
            error_text=error,
        )


def _record_pending_quota_blocked(
    *,
    pending_order_book_ids: Sequence[str],
    audit_by_symbol: Mapping[str, MirrorAuditRecord],
    symbol_map: Mapping[str, str],
    error: str | None,
    record_non_entry: Callable[..., None],
) -> None:
    quota_finished_at = _timestamp_now()
    for order_book_id in pending_order_book_ids:
        symbol = symbol_map[order_book_id]
        if symbol in audit_by_symbol:
            continue
        record_non_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="quota_blocked",
            attempts=0,
            started_at_value=None,
            finished_at_value=quota_finished_at,
            error_text=error,
        )


def _process_dated_mirror_batch(
    *,
    batch_symbols: list[str],
    dataset_name: str,
    fields: Sequence[str],
    start_date: str,
    end_date: str,
    fetch_batch,
    fetch_policy: _MirrorFetchPolicy,
    request_ids_by_symbol: Mapping[str, Sequence[str]],
    primary_order_book_id_by_symbol: Mapping[str, str],
    data_dir: Path,
    date_column: str,
    status: str,
    error: str | None,
    result_code: int,
    quota_blocked: bool,
    columns: list[str],
    batches: list[dict[str, object]],
    audit_by_symbol: Mapping[str, DatedMirrorAuditRecord],
    fetch_single_symbol_with_field_fallback: Callable[
        [Sequence[str]], tuple[pd.DataFrame, int, list[str]]
    ],
    prepare_batch_payload: Callable[[object], pd.DataFrame],
    record_entry: Callable[..., None],
    record_non_entry: Callable[..., None],
) -> tuple[str, str | None, int, bool, list[str]]:
    if not batch_symbols or quota_blocked:
        return status, error, result_code, quota_blocked, columns

    batch_request_ids = [
        request_id
        for symbol in batch_symbols
        for request_id in request_ids_by_symbol.get(symbol, ())
        if str(request_id).strip()
    ]
    if not batch_request_ids:
        return status, error, result_code, quota_blocked, columns

    batch_started_at = _timestamp_now()
    dropped_fields: list[str] = []
    try:
        if len(batch_symbols) == 1:
            prepared, attempts, dropped_fields = fetch_single_symbol_with_field_fallback(
                batch_request_ids
            )
        else:
            label = f"{dataset_name} fetch failed for {', '.join(batch_symbols)}"
            payload, attempts = _retry_mirror_fetch(
                label,
                lambda: fetch_batch(batch_request_ids, fields, start_date, end_date),
                policy=fetch_policy,
            )
            prepared = _ensure_requested_fields(prepare_batch_payload(payload), fields)
    except MirrorQuotaError as exc:
        batch_finished_at = _timestamp_now()
        quota_blocked = True
        status = "stopped_quota"
        error = str(exc)
        result_code = max(result_code, 2)
        for symbol in batch_symbols:
            if symbol in audit_by_symbol:
                continue
            record_non_entry(
                symbol=symbol,
                order_book_id=primary_order_book_id_by_symbol[symbol],
                record_status="quota_blocked",
                attempts=exc.attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                dropped_fields=dropped_fields,
                error_text=str(exc),
            )
        batches.append(
            _quota_blocked_batch_summary(
                order_book_ids=len(batch_request_ids),
                symbols_missing_remote=len(batch_symbols),
                attempts=exc.attempts,
                dropped_fields=dropped_fields,
                error=str(exc),
            )
        )
        return status, error, result_code, quota_blocked, columns
    except MirrorFetchError as exc:
        batch_finished_at = _timestamp_now()
        if len(batch_symbols) > 1:
            batches.append(
                _split_after_error_batch_summary(
                    order_book_ids=len(batch_request_ids),
                    attempts=exc.attempts,
                    error=str(exc),
                )
            )
            for symbol in batch_symbols:
                status, error, result_code, quota_blocked, columns = _process_dated_mirror_batch(
                    batch_symbols=[symbol],
                    dataset_name=dataset_name,
                    fields=fields,
                    start_date=start_date,
                    end_date=end_date,
                    fetch_batch=fetch_batch,
                    fetch_policy=fetch_policy,
                    request_ids_by_symbol=request_ids_by_symbol,
                    primary_order_book_id_by_symbol=primary_order_book_id_by_symbol,
                    data_dir=data_dir,
                    date_column=date_column,
                    status=status,
                    error=error,
                    result_code=result_code,
                    quota_blocked=quota_blocked,
                    columns=columns,
                    batches=batches,
                    audit_by_symbol=audit_by_symbol,
                    fetch_single_symbol_with_field_fallback=(
                        fetch_single_symbol_with_field_fallback
                    ),
                    prepare_batch_payload=prepare_batch_payload,
                    record_entry=record_entry,
                    record_non_entry=record_non_entry,
                )
                if quota_blocked:
                    break
            return status, error, result_code, quota_blocked, columns

        symbol = batch_symbols[0]
        record_non_entry(
            symbol=symbol,
            order_book_id=primary_order_book_id_by_symbol[symbol],
            record_status="failed",
            attempts=exc.attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
            dropped_fields=dropped_fields,
            error_text=str(exc),
        )
        batches.append(
            _failed_batch_summary(
                order_book_ids=len(batch_request_ids),
                attempts=exc.attempts,
                dropped_fields=dropped_fields,
                error=str(exc),
            )
        )
        result_code = max(result_code, 1)
        status = _status_after_batch_failure(status, result_code)
        return status, error, result_code, quota_blocked, columns

    batch_finished_at = _timestamp_now()
    if not prepared.empty and not columns:
        columns = prepared.columns.tolist()
    batches.append(
        _write_dated_prepared_batch_symbols(
            prepared=prepared,
            batch_symbols=batch_symbols,
            batch_request_ids=batch_request_ids,
            data_dir=data_dir,
            date_column=date_column,
            primary_order_book_id_by_symbol=primary_order_book_id_by_symbol,
            attempts=attempts,
            batch_started_at=batch_started_at,
            batch_finished_at=batch_finished_at,
            dropped_fields=dropped_fields,
            record_entry=record_entry,
            record_non_entry=record_non_entry,
        )
    )
    return status, error, result_code, quota_blocked, columns


def _process_mirror_batch(
    *,
    batch_order_book_ids: list[str],
    dataset_name: str,
    fields: Sequence[str],
    fetch_batch,
    fetch_policy: _MirrorFetchPolicy,
    start_quarter: str,
    end_quarter: str,
    statements: Sequence[str],
    query_date: str | None,
    data_dir: Path,
    symbol_map: Mapping[str, str],
    status: str,
    error: str | None,
    result_code: int,
    quota_blocked: bool,
    columns: list[str],
    batches: list[dict[str, object]],
    audit_by_symbol: Mapping[str, MirrorAuditRecord],
    fetch_single_symbol_with_field_fallback: Callable[[str], tuple[pd.DataFrame, int, list[str]]],
    prepare_batch_payload: Callable[[object], pd.DataFrame],
    record_entry: Callable[..., None],
    record_non_entry: Callable[..., None],
) -> tuple[str, str | None, int, bool, list[str]]:
    if not batch_order_book_ids or quota_blocked:
        return status, error, result_code, quota_blocked, columns

    batch_started_at = _timestamp_now()
    dropped_fields: list[str] = []
    try:
        if len(batch_order_book_ids) == 1:
            prepared, attempts, dropped_fields = fetch_single_symbol_with_field_fallback(
                batch_order_book_ids[0]
            )
        else:
            label = f"{dataset_name} fetch failed for {', '.join(batch_order_book_ids)}"
            payload, attempts = _retry_mirror_fetch(
                label,
                lambda: fetch_batch(
                    batch_order_book_ids,
                    fields,
                    start_quarter,
                    end_quarter,
                    date=query_date,
                    statements=statements,
                ),
                policy=fetch_policy,
            )
            prepared = _ensure_requested_fields(prepare_batch_payload(payload), fields)
    except MirrorQuotaError as exc:
        batch_finished_at = _timestamp_now()
        quota_blocked = True
        status = "stopped_quota"
        error = str(exc)
        result_code = max(result_code, 2)
        for order_book_id in batch_order_book_ids:
            symbol = symbol_map[order_book_id]
            if symbol in audit_by_symbol:
                continue
            record_non_entry(
                symbol=symbol,
                order_book_id=order_book_id,
                record_status="quota_blocked",
                attempts=exc.attempts,
                started_at_value=batch_started_at,
                finished_at_value=batch_finished_at,
                dropped_fields=dropped_fields,
                error_text=str(exc),
            )
        batches.append(
            _quota_blocked_batch_summary(
                order_book_ids=len(batch_order_book_ids),
                symbols_missing_remote=0,
                attempts=exc.attempts,
                dropped_fields=dropped_fields,
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
                status, error, result_code, quota_blocked, columns = _process_mirror_batch(
                    batch_order_book_ids=[order_book_id],
                    dataset_name=dataset_name,
                    fields=fields,
                    fetch_batch=fetch_batch,
                    fetch_policy=fetch_policy,
                    start_quarter=start_quarter,
                    end_quarter=end_quarter,
                    statements=statements,
                    query_date=query_date,
                    data_dir=data_dir,
                    symbol_map=symbol_map,
                    status=status,
                    error=error,
                    result_code=result_code,
                    quota_blocked=quota_blocked,
                    columns=columns,
                    batches=batches,
                    audit_by_symbol=audit_by_symbol,
                    fetch_single_symbol_with_field_fallback=(
                        fetch_single_symbol_with_field_fallback
                    ),
                    prepare_batch_payload=prepare_batch_payload,
                    record_entry=record_entry,
                    record_non_entry=record_non_entry,
                )
                if quota_blocked:
                    break
            return status, error, result_code, quota_blocked, columns

        order_book_id = batch_order_book_ids[0]
        symbol = symbol_map[order_book_id]
        record_non_entry(
            symbol=symbol,
            order_book_id=order_book_id,
            record_status="failed",
            attempts=exc.attempts,
            started_at_value=batch_started_at,
            finished_at_value=batch_finished_at,
            dropped_fields=dropped_fields,
            error_text=str(exc),
        )
        batches.append(
            _failed_batch_summary(
                order_book_ids=1,
                attempts=exc.attempts,
                dropped_fields=dropped_fields,
                error=str(exc),
            )
        )
        result_code = max(result_code, 1)
        status = _status_after_batch_failure(status, result_code)
        return status, error, result_code, quota_blocked, columns

    batch_finished_at = _timestamp_now()
    if not prepared.empty and not columns:
        columns = prepared.columns.tolist()
    batches.append(
        _write_prepared_batch_order_book_ids(
            prepared=prepared,
            batch_order_book_ids=batch_order_book_ids,
            data_dir=data_dir,
            symbol_map=symbol_map,
            attempts=attempts,
            batch_started_at=batch_started_at,
            batch_finished_at=batch_finished_at,
            dropped_fields=dropped_fields,
            record_entry=record_entry,
            record_non_entry=record_non_entry,
        )
    )
    return status, error, result_code, quota_blocked, columns


def _finalize_dated_mirror_outputs(
    *,
    context: _DatedMirrorContext,
    dataset_name: str,
    api_name: str,
    date_column: str,
    fields: Sequence[str],
    field_metadata: Mapping[str, object],
    entries_by_symbol: Mapping[str, DatedMirrorEntry],
    audit_by_symbol: Mapping[str, DatedMirrorAuditRecord],
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    field_coverage: Mapping[str, Mapping[str, object]],
    started_at: str,
    status: str,
    error: str | None,
    config_ref: object,
    record_non_entry: Callable[..., None],
) -> None:
    finished_at = _timestamp_now()
    for symbol in context.symbols:
        if symbol in audit_by_symbol:
            continue
        record_non_entry(
            symbol=symbol,
            order_book_id=context.primary_order_book_id_by_symbol[symbol],
            record_status="failed",
            attempts=0,
            started_at_value=None,
            finished_at_value=finished_at,
            error_text=error or "missing audit status",
        )
    audit_records = [audit_by_symbol[symbol] for symbol in context.symbols]
    _write_dated_audit_csv(context.audit_path, audit_records)
    manifest = _build_dated_manifest(
        dataset_name=dataset_name,
        api_name=api_name,
        output_dir=context.output_dir,
        fields=fields,
        field_metadata=field_metadata,
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
        date_column=date_column,
        batches=batches,
        columns=columns,
        audit_file=context.audit_path,
        audit_records=audit_records,
        field_coverage=list(field_coverage.values()),
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        config_ref=config_ref,
    )
    _write_manifest(context.output_dir / "manifest.yml", manifest)


def _finalize_mirror_outputs(
    *,
    context: _MirrorContext,
    args,
    dataset_name: str,
    api_name: str,
    entries_by_symbol: Mapping[str, MirrorEntry],
    audit_by_symbol: Mapping[str, MirrorAuditRecord],
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    field_coverage: Mapping[str, Mapping[str, object]],
    started_at: str,
    status: str,
    error: str | None,
    record_non_entry: Callable[..., None],
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
    _write_audit_csv(context.audit_path, audit_records)
    manifest = _build_manifest(
        dataset_name=dataset_name,
        api_name=api_name,
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
        query_date=getattr(args, "date", None),
        start_quarter=args.start_quarter,
        end_quarter=args.end_quarter,
        statements=args.statements,
        batches=batches,
        columns=columns,
        audit_file=context.audit_path,
        audit_records=audit_records,
        field_coverage=list(field_coverage.values()),
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        config_ref=getattr(args, "config", None),
    )
    _write_manifest(context.output_dir / "manifest.yml", manifest)


def _mirror_dated_dataset(
    *,
    args,
    rqdatac,
    dataset_name: str,
    api_name: str,
    date_column: str,
    fields: Sequence[str],
    field_metadata: Mapping[str, object],
    fetch_batch,
    sort_columns: Sequence[str] = (),
    resolve_request_groups=None,
    normalize_payload=None,
) -> int:
    context = _prepare_dated_mirror_context(
        args=args,
        dataset_name=dataset_name,
        resolve_request_groups=resolve_request_groups,
    )
    symbols = context.symbols
    start_date = context.start_date
    end_date = context.end_date
    resume = context.resume
    skip_existing = context.skip_existing
    max_attempts = context.max_attempts
    backoff_seconds = context.backoff_seconds
    max_backoff_seconds = context.max_backoff_seconds
    output_dir = context.output_dir
    data_dir = context.data_dir
    request_id_metadata = context.request_id_metadata
    request_ids_by_symbol = context.request_ids_by_symbol
    primary_order_book_id_by_symbol = context.primary_order_book_id_by_symbol
    symbol_map = context.symbol_map

    entries_by_symbol: dict[str, DatedMirrorEntry] = {}
    audit_by_symbol: dict[str, DatedMirrorAuditRecord] = {}
    batches: list[dict[str, object]] = []
    columns: list[str] = []
    field_coverage = _field_coverage_template(fields)
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    quota_blocked = False
    fetch_policy = _MirrorFetchPolicy(
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )

    def _payload_to_frame(payload):
        if normalize_payload is None:
            return payload
        return normalize_payload(payload, request_id_metadata=request_id_metadata)

    def _prepare_batch_payload(payload) -> pd.DataFrame:
        return _prepare_dated_asset_frame(
            _payload_to_frame(payload),
            symbol_map=symbol_map,
            date_column=date_column,
            sort_columns=sort_columns,
        )

    def _record_entry(
        *,
        symbol: str,
        entry: DatedMirrorEntry,
        symbol_frame: pd.DataFrame,
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        dropped_fields: Sequence[str] | None = None,
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
            dropped_fields=dropped_fields,
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
        dropped_fields: Sequence[str] | None = None,
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
            dropped_fields=dropped_fields,
            error=error_text,
            entry=None,
        )

    def _fetch_single_symbol_with_field_fallback(
        request_ids: Sequence[str],
    ) -> tuple[pd.DataFrame, int, list[str]]:
        request_ids = [str(item).strip() for item in request_ids if str(item).strip()]
        return _fetch_with_field_fallback(
            dataset_name=dataset_name,
            request_label=", ".join(request_ids),
            fields=fields,
            fetch_payload=lambda active_fields: fetch_batch(
                list(request_ids),
                list(active_fields),
                start_date,
                end_date,
            ),
            prepare_payload=_prepare_batch_payload,
            policy=fetch_policy,
        )

    def _process_batch(batch_symbols: list[str]) -> None:
        nonlocal status, error, result_code, quota_blocked, columns
        status, error, result_code, quota_blocked, columns = _process_dated_mirror_batch(
            batch_symbols=batch_symbols,
            dataset_name=dataset_name,
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            fetch_batch=fetch_batch,
            fetch_policy=fetch_policy,
            request_ids_by_symbol=request_ids_by_symbol,
            primary_order_book_id_by_symbol=primary_order_book_id_by_symbol,
            data_dir=data_dir,
            date_column=date_column,
            status=status,
            error=error,
            result_code=result_code,
            quota_blocked=quota_blocked,
            columns=columns,
            batches=batches,
            audit_by_symbol=audit_by_symbol,
            fetch_single_symbol_with_field_fallback=_fetch_single_symbol_with_field_fallback,
            prepare_batch_payload=_prepare_batch_payload,
            record_entry=_record_entry,
            record_non_entry=_record_non_entry,
        )

    if resume:
        _validate_dated_resume_inputs(
            output_dir=output_dir,
            dataset_name=dataset_name,
            fields=fields,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )

    _write_text_list(output_dir / "fields.txt", list(fields))
    _write_text_list(output_dir / "symbols.txt", symbols)

    pending_symbols = _collect_pending_mirror_items(
        items=symbols,
        data_dir=data_dir,
        skip_existing=skip_existing,
        item_to_symbol=lambda symbol: symbol,
        load_existing=lambda path: _load_existing_dated_entry(
            path,
            date_column=date_column,
            fields=fields,
        ),
        record_entry=_record_entry,
    )

    def _quota_blocked() -> bool:
        return quota_blocked

    def _on_quota_blocked() -> None:
        _record_dated_pending_quota_blocked(
            pending_symbols=pending_symbols,
            audit_by_symbol=audit_by_symbol,
            primary_order_book_id_by_symbol=primary_order_book_id_by_symbol,
            error=error,
            record_non_entry=_record_non_entry,
        )

    def _on_completed_without_quota() -> None:
        nonlocal status
        status = _status_after_batch_failure(status, result_code)

    def _on_exception(exc: Exception) -> None:
        nonlocal status, error, result_code
        status = "failed"
        error = str(exc)
        result_code = max(result_code, 1)

    def _on_finalize() -> None:
        _finalize_dated_mirror_outputs(
            context=context,
            dataset_name=dataset_name,
            api_name=api_name,
            date_column=date_column,
            fields=fields,
            field_metadata=field_metadata,
            entries_by_symbol=entries_by_symbol,
            audit_by_symbol=audit_by_symbol,
            batches=batches,
            columns=columns,
            field_coverage=field_coverage,
            started_at=started_at,
            status=status,
            error=error,
            config_ref=getattr(args, "config", None),
            record_non_entry=_record_non_entry,
        )

    _run_partitioned_mirror_batches(
        pending_items=pending_symbols,
        batch_size=getattr(args, "batch_size", DEFAULT_BATCH_SIZE),
        process_batch=_process_batch,
        quota_blocked=_quota_blocked,
        on_quota_blocked=_on_quota_blocked,
        on_completed_without_quota=_on_completed_without_quota,
        on_exception=_on_exception,
        on_finalize=_on_finalize,
    )

    totals = {
        "files": len(entries_by_symbol),
        "symbols": len(entries_by_symbol),
        "rows": sum(item.rows for item in entries_by_symbol.values()),
        "bytes": sum(item.total_bytes for item in entries_by_symbol.values()),
    }
    print(
        f"Wrote {dataset_name} mirror to {output_dir} "
        f"({totals['symbols']} symbols, {totals['files']} files, "
        f"{totals['rows']} rows, {totals['bytes']} bytes, status={status})"
    )
    return result_code


def _mirror_dataset(
    *,
    args,
    rqdatac,
    dataset_name: str,
    api_name: str,
    fetch_batch,
) -> int:
    context = _prepare_mirror_context(args=args, dataset_name=dataset_name)
    fields = context.fields
    symbols = context.symbols
    resume = context.resume
    skip_existing = context.skip_existing
    max_attempts = context.max_attempts
    backoff_seconds = context.backoff_seconds
    max_backoff_seconds = context.max_backoff_seconds
    output_dir = context.output_dir
    data_dir = context.data_dir
    symbol_map = context.symbol_map
    order_book_ids = context.order_book_ids
    entries_by_symbol: dict[str, MirrorEntry] = {}
    audit_by_symbol: dict[str, MirrorAuditRecord] = {}
    batches: list[dict[str, object]] = []
    columns: list[str] = []
    field_coverage = _field_coverage_template(fields)
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    quota_blocked = False
    fetch_policy = _MirrorFetchPolicy(
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )

    def _prepare_batch_payload(payload) -> pd.DataFrame:
        return _prepare_asset_frame(payload, symbol_map=symbol_map)

    def _record_entry(
        *,
        symbol: str,
        entry: MirrorEntry,
        symbol_frame: pd.DataFrame,
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        dropped_fields: Sequence[str] | None = None,
        error_text: str | None = None,
    ) -> None:
        nonlocal columns
        entries_by_symbol[symbol] = entry
        if not columns and not symbol_frame.empty:
            columns = symbol_frame.columns.tolist()
        _update_field_coverage(field_coverage, symbol_frame, fields=fields)
        audit_by_symbol[symbol] = _audit_record(
            symbol=symbol,
            order_book_id=entry.order_book_id,
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=_path_mtime_iso(entry.path),
            dropped_fields=dropped_fields,
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
        dropped_fields: Sequence[str] | None = None,
        error_text: str | None = None,
    ) -> None:
        audit_by_symbol[symbol] = _audit_record(
            symbol=symbol,
            order_book_id=order_book_id,
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=None,
            dropped_fields=dropped_fields,
            error=error_text,
            entry=None,
        )

    def _fetch_single_symbol_with_field_fallback(
        order_book_id: str,
    ) -> tuple[pd.DataFrame, int, list[str]]:
        return _fetch_with_field_fallback(
            dataset_name=dataset_name,
            request_label=order_book_id,
            fields=fields,
            fetch_payload=lambda active_fields: fetch_batch(
                [order_book_id],
                list(active_fields),
                args.start_quarter,
                args.end_quarter,
                date=getattr(args, "date", None),
                statements=args.statements,
            ),
            prepare_payload=_prepare_batch_payload,
            policy=fetch_policy,
        )

    def _process_batch(batch_order_book_ids: list[str]) -> None:
        nonlocal status, error, result_code, quota_blocked, columns
        status, error, result_code, quota_blocked, columns = _process_mirror_batch(
            batch_order_book_ids=batch_order_book_ids,
            dataset_name=dataset_name,
            fields=fields,
            fetch_batch=fetch_batch,
            fetch_policy=fetch_policy,
            start_quarter=args.start_quarter,
            end_quarter=args.end_quarter,
            statements=args.statements,
            query_date=getattr(args, "date", None),
            data_dir=data_dir,
            symbol_map=symbol_map,
            status=status,
            error=error,
            result_code=result_code,
            quota_blocked=quota_blocked,
            columns=columns,
            batches=batches,
            audit_by_symbol=audit_by_symbol,
            fetch_single_symbol_with_field_fallback=_fetch_single_symbol_with_field_fallback,
            prepare_batch_payload=_prepare_batch_payload,
            record_entry=_record_entry,
            record_non_entry=_record_non_entry,
        )

    if resume:
        _validate_resume_inputs(
            output_dir=output_dir,
            dataset_name=dataset_name,
            fields=fields,
            symbols=symbols,
            start_quarter=args.start_quarter,
            end_quarter=args.end_quarter,
            statements=args.statements,
            query_date=getattr(args, "date", None),
        )

    _write_text_list(output_dir / "fields.txt", fields)
    _write_text_list(output_dir / "symbols.txt", symbols)

    pending_order_book_ids = _collect_pending_mirror_items(
        items=order_book_ids,
        data_dir=data_dir,
        skip_existing=skip_existing,
        item_to_symbol=lambda order_book_id: symbol_map[order_book_id],
        load_existing=lambda path: _load_existing_entry(path, fields=fields),
        record_entry=_record_entry,
    )

    def _quota_blocked() -> bool:
        return quota_blocked

    def _on_quota_blocked() -> None:
        _record_pending_quota_blocked(
            pending_order_book_ids=pending_order_book_ids,
            audit_by_symbol=audit_by_symbol,
            symbol_map=symbol_map,
            error=error,
            record_non_entry=_record_non_entry,
        )

    def _on_completed_without_quota() -> None:
        nonlocal status
        status = _status_after_batch_failure(status, result_code)

    def _on_exception(exc: Exception) -> None:
        nonlocal status, error, result_code
        status = "failed"
        error = str(exc)
        result_code = max(result_code, 1)

    def _on_finalize() -> None:
        _finalize_mirror_outputs(
            context=context,
            args=args,
            dataset_name=dataset_name,
            api_name=api_name,
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

    _run_partitioned_mirror_batches(
        pending_items=pending_order_book_ids,
        batch_size=getattr(args, "batch_size", DEFAULT_BATCH_SIZE),
        process_batch=_process_batch,
        quota_blocked=_quota_blocked,
        on_quota_blocked=_on_quota_blocked,
        on_completed_without_quota=_on_completed_without_quota,
        on_exception=_on_exception,
        on_finalize=_on_finalize,
    )

    totals = {
        "files": len(entries_by_symbol),
        "symbols": len(entries_by_symbol),
        "rows": sum(item.rows for item in entries_by_symbol.values()),
        "bytes": sum(item.total_bytes for item in entries_by_symbol.values()),
    }
    print(
        f"Wrote {dataset_name} mirror to {output_dir} "
        f"({totals['symbols']} symbols, {totals['files']} files, "
        f"{totals['rows']} rows, {totals['bytes']} bytes, status={status})"
    )
    return result_code
