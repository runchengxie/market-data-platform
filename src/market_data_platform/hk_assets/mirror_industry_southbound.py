from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol
from .asset_io import (
    _dated_audit_record,
    _ensure_requested_fields,
    _field_coverage_template,
    _load_existing_dated_entry,
    _prepare_dated_asset_frame,
    _update_field_coverage,
    _write_dated_audit_csv,
    _write_dated_symbol_frame,
)
from .fetch_runtime import _retry_fetch
from .industry_ops import (
    _resolve_hk_southbound_trading_types,
    _resolve_hk_trading_snapshot_dates,
)
from .manifest_ops import _build_dated_manifest, _validate_dated_resume_inputs
from .models import DatedMirrorAuditRecord, DatedMirrorEntry, MirrorFetchError, MirrorQuotaError
from .package_api import _package_attr
from .request_groups import _resolve_symbols
from .shared import (
    _dedupe_preserve_order,
    _load_existing_text_list,
    _load_manifest,
    _normalize_absolute_date,
    _normalize_hk_symbol,
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


@dataclass(frozen=True)
class _SouthboundMirrorContext:
    symbols: list[str]
    symbol_metadata: dict[str, object]
    start_date: str
    end_date: str
    trading_types: list[str]
    snapshot_dates: list[str]
    snapshot_metadata: dict[str, object]
    resume: bool
    skip_existing: bool
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float
    output_dir: Path
    data_dir: Path
    audit_path: Path
    manifest_path: Path
    fields: list[str]
    field_metadata: dict[str, object]
    order_book_id_by_symbol: dict[str, str]
    symbol_map: dict[str, str]
    requested_batch_keys: list[tuple[str, str]]


@dataclass
class _SouthboundResumeState:
    existing_status: str
    resume_from_partial: bool
    batches: list[dict[str, object]]
    completed_batch_keys: set[tuple[str, str]]


def _prepare_southbound_mirror_context(args, rqdatac) -> _SouthboundMirrorContext:
    symbols, symbol_metadata = _resolve_symbols(args)
    start_date = _normalize_absolute_date(args.start_date, label="--start-date")
    end_date = _normalize_absolute_date(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    trading_types = _resolve_hk_southbound_trading_types(args)
    snapshot_dates, snapshot_metadata = _resolve_hk_trading_snapshot_dates(
        rqdatac,
        args,
        start_date=start_date,
        end_date=end_date,
    )
    resume = bool(getattr(args, "resume", False))
    output_dir = _prepare_daily_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="southbound",
        start_date=start_date,
        end_date=end_date,
        name=getattr(args, "name", None),
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    fields = ["trading_type", "eligible"]
    order_book_id_by_symbol = {symbol: _to_rqdata_symbol("hk", symbol) for symbol in symbols}
    return _SouthboundMirrorContext(
        symbols=symbols,
        symbol_metadata=symbol_metadata,
        start_date=start_date,
        end_date=end_date,
        trading_types=trading_types,
        snapshot_dates=snapshot_dates,
        snapshot_metadata=snapshot_metadata,
        resume=resume,
        skip_existing=bool(getattr(args, "skip_existing", False) or resume),
        max_attempts=max(
            1,
            int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1),
        ),
        backoff_seconds=float(getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS)),
        max_backoff_seconds=float(
            getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
        ),
        output_dir=output_dir,
        data_dir=data_dir,
        audit_path=output_dir / "audit.csv",
        manifest_path=output_dir / "manifest.yml",
        fields=fields,
        field_metadata={
            "count": len(fields),
            "fields_file": [],
            "source": "southbound_membership",
            "base_fields": list(fields),
        },
        order_book_id_by_symbol=order_book_id_by_symbol,
        symbol_map={
            order_book_id: symbol
            for symbol, order_book_id in order_book_id_by_symbol.items()
        },
        requested_batch_keys=[
            (query_date, trading_type)
            for query_date in snapshot_dates
            for trading_type in trading_types
        ],
    )


def _load_southbound_resume_state(
    *,
    manifest_path: Path,
    resume: bool,
) -> _SouthboundResumeState:
    existing_manifest = _load_manifest(manifest_path) if resume and manifest_path.exists() else {}
    existing_status = (
        str(existing_manifest.get("status") or "").strip()
        if isinstance(existing_manifest, Mapping)
        else ""
    )
    batches: list[dict[str, object]] = []
    completed_batch_keys: set[tuple[str, str]] = set()
    if isinstance(existing_manifest, Mapping):
        existing_batches = existing_manifest.get("batches")
        if isinstance(existing_batches, Sequence) and not isinstance(
            existing_batches,
            (str, bytes),
        ):
            for row in existing_batches:
                if not isinstance(row, Mapping):
                    continue
                batch = dict(row)
                batches.append(batch)
                if batch.get("status") != "completed":
                    continue
                batch_date = str(batch.get("date") or "").strip()
                batch_type = str(batch.get("trading_type") or "").strip()
                if batch_date and batch_type:
                    completed_batch_keys.add((batch_date, batch_type))

    return _SouthboundResumeState(
        existing_status=existing_status,
        resume_from_partial=resume and existing_status not in {"", "completed"},
        batches=batches,
        completed_batch_keys=completed_batch_keys,
    )


def _southbound_pending_batch_count(
    *,
    context: _SouthboundMirrorContext,
    completed_batch_keys: set[tuple[str, str]],
) -> int:
    return max(0, len(context.requested_batch_keys) - len(completed_batch_keys))


def _annotate_southbound_manifest(
    manifest: dict[str, object],
    *,
    context: _SouthboundMirrorContext,
    completed_batch_keys: set[tuple[str, str]],
    symbols_with_persisted_data: int,
) -> dict[str, object]:
    manifest_query = manifest.get("query", {})
    if isinstance(manifest_query, dict):
        manifest_query["rebalance_frequency"] = context.snapshot_metadata.get(
            "rebalance_frequency"
        )
        manifest_query["dates_count"] = len(context.snapshot_dates)
        manifest_query["dates_file"] = str(context.output_dir / "dates.txt")
        manifest_query["trading_types"] = list(context.trading_types)
        manifest_query["trading_types_file"] = str(context.output_dir / "trading_types.txt")
    manifest["date_source"] = context.snapshot_metadata
    manifest["checkpoint"] = {
        "completed_batches": len(completed_batch_keys),
        "total_batches": len(context.requested_batch_keys),
        "pending_batches": _southbound_pending_batch_count(
            context=context,
            completed_batch_keys=completed_batch_keys,
        ),
        "symbols_with_persisted_data": symbols_with_persisted_data,
    }
    return manifest


def _build_southbound_manifest(
    *,
    context: _SouthboundMirrorContext,
    entries: Sequence[DatedMirrorEntry],
    missing_symbols: Sequence[str],
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    audit_records: Sequence[DatedMirrorAuditRecord],
    field_coverage: Sequence[Mapping[str, object]],
    started_at: str,
    finished_at: str,
    status: str,
    error: str | None,
    config_ref: object,
    completed_batch_keys: set[tuple[str, str]],
    symbols_with_persisted_data: int,
) -> dict[str, object]:
    manifest = _build_dated_manifest(
        dataset_name="southbound",
        api_name="rqdatac.hk.get_southbound_eligible_secs",
        output_dir=context.output_dir,
        fields=context.fields,
        field_metadata=context.field_metadata,
        symbol_metadata=context.symbol_metadata,
        symbols_requested=context.symbols,
        entries=entries,
        missing_symbols=missing_symbols,
        start_date=context.start_date,
        end_date=context.end_date,
        date_column="date",
        batches=batches,
        columns=columns,
        audit_file=context.audit_path,
        audit_records=audit_records,
        field_coverage=field_coverage,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        config_ref=config_ref,
    )
    return _annotate_southbound_manifest(
        manifest,
        context=context,
        completed_batch_keys=completed_batch_keys,
        symbols_with_persisted_data=symbols_with_persisted_data,
    )


def _southbound_batch_record(
    *,
    query_date: str,
    trading_type: str,
    rows: int,
    symbols: int,
    status: str,
    attempts: int,
    started_at: str | None = None,
    finished_at: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "date": query_date,
        "trading_type": trading_type,
        "rows": int(rows),
        "symbols": int(symbols),
        "status": status,
        "attempts": int(attempts),
    }
    if started_at is not None:
        record["started_at"] = started_at
    if finished_at is not None:
        record["finished_at"] = finished_at
    if error is not None:
        record["error"] = error
    return record


def _fetch_southbound_batch(
    *,
    rqdatac,
    context: _SouthboundMirrorContext,
    query_date: str,
    trading_type: str,
):
    return _retry_fetch(
        f"southbound fetch failed for {trading_type} @ {query_date}",
        lambda: rqdatac.hk.get_southbound_eligible_secs(
            trading_type=trading_type,
            date=query_date,
        ),
        max_attempts=context.max_attempts,
        backoff_seconds=context.backoff_seconds,
        max_backoff_seconds=context.max_backoff_seconds,
    )


def _prepare_southbound_batch_frame(
    *,
    payload,
    context: _SouthboundMirrorContext,
    query_date: str,
    trading_type: str,
    pending_symbol_set: set[str],
) -> pd.DataFrame:
    rows = []
    for order_book_id in list(payload or []):
        symbol = _normalize_hk_symbol(order_book_id)
        if not symbol or symbol not in pending_symbol_set:
            continue
        rows.append(
            {
                "date": query_date,
                "symbol": symbol,
                "order_book_id": context.order_book_id_by_symbol[symbol],
                "trading_type": trading_type,
                "eligible": 1,
            }
        )
    prepared = _prepare_dated_asset_frame(
        pd.DataFrame(
            rows,
            columns=["date", "symbol", "order_book_id", "trading_type", "eligible"],
        ),
        symbol_map=context.symbol_map,
        date_column="date",
        sort_columns=("trading_type",),
    )
    return _ensure_requested_fields(prepared, context.fields)


def _write_southbound_symbol_history(
    *,
    symbol: str,
    symbol_frame: pd.DataFrame,
    context: _SouthboundMirrorContext,
    frames_by_symbol: dict[str, pd.DataFrame],
    started_at: str,
    record_entry,
) -> None:
    current = frames_by_symbol.get(symbol)
    if current is None:
        out_path = context.data_dir / f"{symbol}.parquet"
        if out_path.exists():
            _, current = _load_existing_dated_entry(
                out_path,
                date_column="date",
                fields=context.fields,
            )
    if current is not None and not current.empty:
        combined = pd.concat([current, symbol_frame], ignore_index=True)
    else:
        combined = symbol_frame.copy()
    combined = combined.drop_duplicates(subset=["date", "trading_type"], keep="last")
    combined = combined.sort_values(["date", "trading_type"]).reset_index(drop=True)
    frames_by_symbol[symbol] = combined
    entry = _write_dated_symbol_frame(context.data_dir, combined, date_column="date")
    record_entry(
        symbol=symbol,
        entry=entry,
        symbol_frame=combined,
        record_status="written",
        attempts=0,
        started_at_value=started_at,
        finished_at_value=_path_mtime_iso(entry.path),
    )


def _finalize_southbound_outputs(
    *,
    context: _SouthboundMirrorContext,
    audit_by_symbol: Mapping[str, DatedMirrorAuditRecord],
    batches: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    started_at: str,
    status: str,
    error: str | None,
    quota_blocked: bool,
    resume: bool,
    existing_status: str,
    completed_batch_keys: set[tuple[str, str]],
    config_ref: object,
) -> dict[str, DatedMirrorEntry]:
    finished_at = _timestamp_now()
    final_entries_by_symbol: dict[str, DatedMirrorEntry] = {}
    final_audit_by_symbol: dict[str, DatedMirrorAuditRecord] = {}
    final_columns: list[str] = list(columns)
    final_field_coverage = _field_coverage_template(context.fields)
    completed_noop_resume = (
        resume
        and existing_status == "completed"
        and _southbound_pending_batch_count(
            context=context,
            completed_batch_keys=completed_batch_keys,
        )
        == 0
    )
    for symbol in context.symbols:
        out_path = context.data_dir / f"{symbol}.parquet"
        if out_path.exists():
            entry, symbol_frame = _load_existing_dated_entry(
                out_path,
                date_column="date",
                fields=context.fields,
            )
            final_entries_by_symbol[symbol] = entry
            if not final_columns and not symbol_frame.empty:
                final_columns = symbol_frame.columns.tolist()
            _update_field_coverage(final_field_coverage, symbol_frame, fields=context.fields)
            prior_record = audit_by_symbol.get(symbol)
            record_status = (
                "skipped_existing"
                if completed_noop_resume
                or (prior_record and prior_record.status == "skipped_existing")
                else "written"
            )
            final_audit_by_symbol[symbol] = _dated_audit_record(
                symbol=symbol,
                order_book_id=entry.order_book_id,
                status=record_status,
                attempts=prior_record.attempts if prior_record else 0,
                started_at=prior_record.started_at if prior_record else started_at,
                finished_at=finished_at,
                file_mtime=_path_mtime_iso(entry.path),
                error=error if quota_blocked and record_status == "written" else None,
                entry=entry,
            )
            continue
        final_audit_by_symbol[symbol] = _dated_audit_record(
            symbol=symbol,
            order_book_id=context.order_book_id_by_symbol[symbol],
            status="quota_blocked" if quota_blocked else "missing_remote",
            attempts=0,
            started_at=None,
            finished_at=finished_at,
            file_mtime=None,
            error=error if quota_blocked else None,
            entry=None,
        )

    audit_records = [final_audit_by_symbol[symbol] for symbol in context.symbols]
    _write_dated_audit_csv(context.audit_path, audit_records)
    manifest = _build_southbound_manifest(
        context=context,
        entries=[
            final_entries_by_symbol[symbol]
            for symbol in context.symbols
            if symbol in final_entries_by_symbol
        ],
        missing_symbols=[item.symbol for item in audit_records if item.status == "missing_remote"],
        batches=batches,
        columns=final_columns,
        audit_records=audit_records,
        field_coverage=list(final_field_coverage.values()),
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        error=error,
        config_ref=config_ref,
        completed_batch_keys=completed_batch_keys,
        symbols_with_persisted_data=len(final_entries_by_symbol),
    )
    _write_manifest(context.manifest_path, manifest)
    return final_entries_by_symbol


def mirror_hk_southbound(args, rqdatac) -> int:
    context = _prepare_southbound_mirror_context(args, rqdatac)
    symbols = context.symbols
    start_date = context.start_date
    end_date = context.end_date
    trading_types = context.trading_types
    snapshot_dates = context.snapshot_dates
    snapshot_metadata = context.snapshot_metadata
    resume = context.resume
    skip_existing = context.skip_existing
    output_dir = context.output_dir
    data_dir = context.data_dir
    manifest_path = context.manifest_path
    fields = context.fields
    order_book_id_by_symbol = context.order_book_id_by_symbol
    entries_by_symbol: dict[str, DatedMirrorEntry] = {}
    audit_by_symbol: dict[str, DatedMirrorAuditRecord] = {}
    frames_by_symbol: dict[str, pd.DataFrame] = {}
    columns: list[str] = []
    started_at = _timestamp_now()
    status = "completed"
    error: str | None = None
    result_code = 0
    quota_blocked = False
    resume_state = _load_southbound_resume_state(
        manifest_path=manifest_path,
        resume=resume,
    )
    existing_status = resume_state.existing_status
    resume_from_partial = resume_state.resume_from_partial
    batches = resume_state.batches
    completed_batch_keys = resume_state.completed_batch_keys
    final_entries_by_symbol: dict[str, DatedMirrorEntry] = {}

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
        record_status: str,
        attempts: int,
        started_at_value: str | None,
        finished_at_value: str | None,
        error_text: str | None = None,
    ) -> None:
        audit_by_symbol[symbol] = _dated_audit_record(
            symbol=symbol,
            order_book_id=order_book_id_by_symbol[symbol],
            status=record_status,
            attempts=attempts,
            started_at=started_at_value,
            finished_at=finished_at_value,
            file_mtime=None,
            error=error_text,
            entry=None,
        )

    def _current_pending_batches() -> int:
        return _southbound_pending_batch_count(
            context=context,
            completed_batch_keys=completed_batch_keys,
        )

    def _write_checkpoint_manifest(*, checkpoint_status: str, checkpoint_error: str | None) -> None:
        effective_status = checkpoint_status
        if _current_pending_batches() > 0 and effective_status == "completed":
            effective_status = "running"
        checkpoint_finished_at = _timestamp_now()
        checkpoint_manifest = _build_southbound_manifest(
            context=context,
            entries=[
                entries_by_symbol[symbol]
                for symbol in symbols
                if symbol in entries_by_symbol
            ],
            missing_symbols=[],
            batches=batches,
            columns=columns,
            audit_records=[
                audit_by_symbol[symbol]
                for symbol in symbols
                if symbol in audit_by_symbol
            ],
            field_coverage=[],
            started_at=started_at,
            finished_at=checkpoint_finished_at,
            status=effective_status,
            error=checkpoint_error,
            config_ref=getattr(args, "config", None),
            symbols_with_persisted_data=sum(
                1 for symbol in symbols if (data_dir / f"{symbol}.parquet").exists()
            ),
            completed_batch_keys=completed_batch_keys,
        )
        _write_manifest(manifest_path, checkpoint_manifest)

    try:
        if resume:
            _validate_dated_resume_inputs(
                output_dir=output_dir,
                dataset_name="southbound",
                fields=fields,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
            )
            manifest = _load_manifest(output_dir / "manifest.yml") or {}
            query = manifest.get("query") if isinstance(manifest.get("query"), Mapping) else {}
            if isinstance(query, Mapping):
                if query.get("rebalance_frequency") not in {
                    None,
                    snapshot_metadata.get("rebalance_frequency"),
                }:
                    raise SystemExit("Resume target query mismatch for rebalance_frequency.")
                existing_types = query.get("trading_types")
                if existing_types is not None:
                    normalized_existing_types = (
                        _dedupe_preserve_order(existing_types)
                        if isinstance(existing_types, Sequence) and not isinstance(existing_types, str)
                        else _dedupe_preserve_order([existing_types])
                    )
                    if normalized_existing_types != list(trading_types):
                        raise SystemExit("Resume target query mismatch for trading_types.")
            existing_dates = _load_existing_text_list(output_dir / "dates.txt", strip=False)
            if existing_dates and list(existing_dates) != list(snapshot_dates):
                raise SystemExit("Resume target dates.txt does not match the requested date list.")
            existing_trading_types = _load_existing_text_list(
                output_dir / "trading_types.txt",
                strip=False,
            )
            if existing_trading_types and list(existing_trading_types) != list(trading_types):
                raise SystemExit(
                    "Resume target trading_types.txt does not match the requested trading type list."
                )

        _write_text_list(output_dir / "fields.txt", fields)
        _write_text_list(output_dir / "symbols.txt", symbols)
        _write_text_list(output_dir / "dates.txt", snapshot_dates)
        _write_text_list(output_dir / "trading_types.txt", trading_types)

        pending_symbols: list[str] = []
        for symbol in symbols:
            out_path = data_dir / f"{symbol}.parquet"
            if skip_existing and not resume_from_partial and out_path.exists():
                try:
                    entry, symbol_frame = _load_existing_dated_entry(
                        out_path,
                        date_column="date",
                        fields=fields,
                    )
                except Exception:
                    pending_symbols.append(symbol)
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
            pending_symbols.append(symbol)

        pending_symbol_set = set(pending_symbols)
        if _current_pending_batches() > 0:
            _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)
        for query_date in snapshot_dates:
            if quota_blocked or not pending_symbol_set:
                break
            for trading_type in trading_types:
                if quota_blocked or not pending_symbol_set:
                    break
                batch_key = (query_date, trading_type)
                if batch_key in completed_batch_keys:
                    continue
                batch_started_at = _timestamp_now()
                try:
                    payload, attempts = _fetch_southbound_batch(
                        rqdatac=rqdatac,
                        context=context,
                        query_date=query_date,
                        trading_type=trading_type,
                    )
                except MirrorQuotaError as exc:
                    quota_blocked = True
                    status = "stopped_quota"
                    error = str(exc)
                    result_code = max(result_code, 2)
                    batches.append(
                        _southbound_batch_record(
                            query_date=query_date,
                            trading_type=trading_type,
                            rows=0,
                            symbols=0,
                            status="quota_blocked",
                            attempts=exc.attempts,
                            error=str(exc),
                        )
                    )
                    _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)
                    break
                except MirrorFetchError as exc:
                    batches.append(
                        _southbound_batch_record(
                            query_date=query_date,
                            trading_type=trading_type,
                            rows=0,
                            symbols=0,
                            status="failed",
                            attempts=exc.attempts,
                            error=str(exc),
                        )
                    )
                    if status == "completed":
                        status = "completed_with_failures"
                    result_code = max(result_code, 1)
                    _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)
                    continue

                prepared = _prepare_southbound_batch_frame(
                    payload=payload,
                    context=context,
                    query_date=query_date,
                    trading_type=trading_type,
                    pending_symbol_set=pending_symbol_set,
                )
                batches.append(
                    _southbound_batch_record(
                        query_date=query_date,
                        trading_type=trading_type,
                        rows=int(len(prepared)),
                        symbols=int(prepared["symbol"].nunique()) if not prepared.empty else 0,
                        status="completed",
                        attempts=attempts,
                        started_at=batch_started_at,
                        finished_at=_timestamp_now(),
                    )
                )
                completed_batch_keys.add(batch_key)
                if prepared.empty:
                    _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)
                    continue
                for symbol in prepared["symbol"].drop_duplicates().tolist():
                    symbol_frame = prepared[prepared["symbol"] == symbol].reset_index(drop=True)
                    if symbol_frame.empty:
                        continue
                    _write_southbound_symbol_history(
                        symbol=symbol,
                        symbol_frame=symbol_frame,
                        context=context,
                        frames_by_symbol=frames_by_symbol,
                        started_at=started_at,
                        record_entry=_record_entry,
                    )
                _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)

        if result_code == 1 and status == "completed":
            status = "completed_with_failures"
    except KeyboardInterrupt:
        status = "interrupted"
        error = "Interrupted by user"
        result_code = max(result_code, 1)
        _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)
        raise
    except Exception as exc:
        status = "failed"
        error = str(exc)
        result_code = max(result_code, 1)
        _write_checkpoint_manifest(checkpoint_status=status, checkpoint_error=error)
        raise
    finally:
        final_entries_by_symbol = _finalize_southbound_outputs(
            context=context,
            audit_by_symbol=audit_by_symbol,
            batches=batches,
            columns=columns,
            started_at=started_at,
            status=status,
            error=error,
            quota_blocked=quota_blocked,
            resume=resume,
            existing_status=existing_status,
            completed_batch_keys=completed_batch_keys,
            config_ref=getattr(args, "config", None),
        )

    totals = {
        "files": len(final_entries_by_symbol),
        "symbols": len(final_entries_by_symbol),
        "rows": sum(item.rows for item in final_entries_by_symbol.values()),
        "bytes": sum(item.total_bytes for item in final_entries_by_symbol.values()),
    }
    print(
        f"Wrote southbound mirror to {output_dir} "
        f"({totals['symbols']} symbols, {totals['files']} files, {totals['rows']} rows, {totals['bytes']} bytes, status={status})"
    )
    return result_code
