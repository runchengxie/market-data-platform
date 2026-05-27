"""Probe and download workflows."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pandas as pd

from market_data_platform.hk_depth.audit import (
    AuditRecord,
    IncrementalAuditWriter,
    default_audit_path,
    summarize_audit,
)
from market_data_platform.hk_depth.coverage import (
    STATUS_MISSING,
    VALID_STATUS,
    coverage_summary,
    scan_raw_coverage,
)
from market_data_platform.hk_depth.download_state import (
    DownloadMetadataRecorder,
    download_detail_path,
)
from market_data_platform.hk_depth.exceptions import DownloadError, ProviderRequestError
from market_data_platform.hk_depth.fields import DEFAULT_TICK_DEPTH_FIELDS
from market_data_platform.hk_depth.rq_client import TickDataProvider
from market_data_platform.hk_depth.runtime import retry_provider_call
from market_data_platform.hk_depth.schema import normalize_tick_frame
from market_data_platform.hk_depth.storage import (
    DEFAULT_PARQUET_COMPRESSION,
    DEFAULT_PARQUET_ENGINE,
    atomic_write_parquet,
    batch_part_path,
    metadata_path,
    symbol_date_part_path,
    validate_parquet_write_options,
    write_json,
)
from market_data_platform.hk_depth.symbols import format_date, iter_dates


@dataclass(frozen=True)
class BatchPlan:
    trade_date: str
    batch_number: int
    symbols: tuple[str, ...]
    part_path: Path


@dataclass(frozen=True)
class UnitPlan:
    trade_date: str
    order_book_id: str
    part_path: Path


@dataclass(frozen=True)
class ProviderBatch:
    trade_date: str
    batch_number: int
    symbols: tuple[str, ...]
    units: tuple[UnitPlan, ...]


@dataclass(frozen=True)
class DownloadConfig:
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    output_root: Path
    fields: tuple[str, ...]
    batch_size: int = 5
    resume: bool = True
    continue_on_error: bool = False
    dry_run: bool = False
    metadata_kind: str = "download"
    raw_layout: str = "symbol-date"
    calendar: str = "provider"
    adjust_type: str = "none"
    time_slice: str | None = None
    parquet_engine: str = DEFAULT_PARQUET_ENGINE
    parquet_compression: str | None = DEFAULT_PARQUET_COMPRESSION
    parquet_compression_level: int | None = None
    retry_max_attempts: int = 1
    retry_backoff_seconds: float = 0.0
    retry_max_backoff_seconds: float = 60.0
    quota_guard: bool = True
    quota_stop_ratio: float = 0.95
    quota_safety_multiplier: float = 1.2
    audit_output: str | Path | None = None
    metadata_detail_limit: int = 1000


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def chunked(items: Sequence[str], size: int) -> list[tuple[str, ...]]:
    if size < 1:
        raise ValueError("batch_size must be at least 1.")
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def build_batch_plan(
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    batch_size: int,
    trade_dates: Sequence[str] | None = None,
) -> list[BatchPlan]:
    root = Path(output_root)
    batches: list[BatchPlan] = []
    dates = list(trade_dates) if trade_dates is not None else list(iter_dates(start_date, end_date))
    for trade_date in dates:
        for batch_number, symbol_batch in enumerate(chunked(list(symbols), batch_size)):
            batches.append(
                BatchPlan(
                    trade_date=trade_date,
                    batch_number=batch_number,
                    symbols=symbol_batch,
                    part_path=batch_part_path(root, trade_date, batch_number),
                )
            )
    return batches


def iter_symbol_date_plan(
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    trade_dates: Sequence[str] | None = None,
) -> Iterator[UnitPlan]:
    root = Path(output_root)
    dates = list(trade_dates) if trade_dates is not None else list(iter_dates(start_date, end_date))
    for trade_date in dates:
        for symbol in symbols:
            yield UnitPlan(
                trade_date=trade_date,
                order_book_id=symbol,
                part_path=symbol_date_part_path(root, trade_date, symbol),
            )


def build_symbol_date_plan(
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    trade_dates: Sequence[str] | None = None,
) -> list[UnitPlan]:
    return list(iter_symbol_date_plan(symbols, start_date, end_date, output_root, trade_dates))


def normalize_raw_layout(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"symbol-date", "symbol", "symbol-date-v1"}:
        return "symbol-date"
    if normalized in {"batch", "legacy-batch", "legacy"}:
        return "batch"
    raise ValueError("raw_layout must be one of: symbol-date, batch.")


def normalize_calendar(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"provider", "rqdata", "trading", "trading-days"}:
        return "provider"
    if normalized in {"calendar", "natural", "all-days"}:
        return "calendar"
    raise ValueError("calendar must be one of: provider, calendar.")


def _resolve_trade_dates(
    *,
    provider: TickDataProvider | None,
    start_date: str,
    end_date: str,
    calendar: str,
) -> tuple[list[str], str]:
    normalized = normalize_calendar(calendar)
    if normalized == "provider" and provider is not None and hasattr(provider, "get_trading_dates"):
        dates = [format_date(value) for value in provider.get_trading_dates(start_date, end_date)]
        return dates, "provider"
    source = "calendar"
    if normalized == "provider" and provider is None:
        source = "calendar_fallback_no_provider"
    elif normalized == "provider":
        source = "calendar_fallback_no_provider_method"
    return list(iter_dates(start_date, end_date)), source


def _quota_snapshot(provider: TickDataProvider | None) -> dict[str, Any]:
    if provider is None or not hasattr(provider, "quota_snapshot"):
        return {"available": False}
    try:
        snapshot = provider.quota_snapshot()
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    if snapshot is None:
        return {"available": False}
    return {"available": True, "value": snapshot}


def _quota_payload(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot or not snapshot.get("available"):
        return None
    value = snapshot.get("value")
    return value if isinstance(value, dict) else None


def _quota_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def _quota_used_limit(snapshot: dict[str, Any] | None) -> tuple[int, int] | None:
    payload = _quota_payload(snapshot)
    if payload is None:
        return None
    used = _quota_int(payload, "bytes_used", "used_bytes", "traffic_used", "used")
    limit = _quota_int(payload, "bytes_limit", "limit_bytes", "traffic_limit", "limit")
    if used is None or limit is None or limit <= 0:
        return None
    return used, limit


def _quota_used(snapshot: dict[str, Any] | None) -> int | None:
    values = _quota_used_limit(snapshot)
    return values[0] if values else None


def _quota_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> int | None:
    before_used = _quota_used(before)
    after_used = _quota_used(after)
    if before_used is None or after_used is None:
        return None
    return max(0, after_used - before_used)


def _estimate_next_quota_delta(
    successful_deltas: Sequence[int],
    *,
    safety_multiplier: float,
) -> int | None:
    deltas = [int(value) for value in successful_deltas if int(value) > 0]
    if not deltas:
        return None
    series = pd.Series(deltas, dtype="float64")
    estimate = max(float(series.quantile(0.90)), float(deltas[-1])) * safety_multiplier
    return int(estimate)


def _quota_guard_decision(
    snapshot: dict[str, Any],
    successful_deltas: Sequence[int],
    *,
    enabled: bool,
    stop_ratio: float,
    safety_multiplier: float,
) -> dict[str, Any]:
    values = _quota_used_limit(snapshot)
    if not enabled or values is None:
        return {
            "available": values is not None,
            "blocked": False,
            "estimated_next_delta_bytes": None,
        }
    used, limit = values
    estimate = _estimate_next_quota_delta(
        successful_deltas,
        safety_multiplier=safety_multiplier,
    )
    if estimate is None:
        return {
            "available": True,
            "blocked": False,
            "bytes_used": used,
            "bytes_limit": limit,
            "estimated_next_delta_bytes": None,
        }
    threshold = int(limit * stop_ratio)
    blocked = used + estimate >= threshold
    return {
        "available": True,
        "blocked": blocked,
        "bytes_used": used,
        "bytes_limit": limit,
        "stop_threshold_bytes": threshold,
        "estimated_next_delta_bytes": estimate,
    }


def _audit_record(
    *,
    run_id: str,
    chunk_id: str,
    unit: UnitPlan,
    status: str,
    rows: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    quota_before: dict[str, Any] | None = None,
    quota_after: dict[str, Any] | None = None,
    quota_delta: int | None = None,
    attempts: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> AuditRecord:
    return AuditRecord(
        run_id=run_id,
        chunk_id=chunk_id,
        trade_date=unit.trade_date,
        order_book_id=unit.order_book_id,
        status=status,
        part_path=str(unit.part_path),
        rows=rows,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        quota_before_bytes_used=_quota_used(quota_before),
        quota_after_bytes_used=_quota_used(quota_after),
        quota_delta_bytes=quota_delta,
        attempts=attempts,
        error_type=error_type,
        error_message=error_message,
    )


def _storage_settings(
    *,
    raw_layout: str,
    parquet_engine: str,
    parquet_compression: str | None,
    parquet_compression_level: int | None,
) -> dict[str, Any]:
    writer = validate_parquet_write_options(
        engine=parquet_engine,
        compression=parquet_compression,
        compression_level=parquet_compression_level,
    )
    return {
        "raw_layout": raw_layout,
        "layout_version": "symbol_date.v1" if raw_layout == "symbol-date" else "legacy_batch.v1",
        "parquet": writer,
    }


def _layout_deprecations(raw_layout: str) -> list[dict[str, str]]:
    if raw_layout != "batch":
        return []
    return [
        {
            "feature": "raw_layout=batch",
            "status": "deprecated",
            "replacement": "raw_layout=symbol-date",
            "message": (
                "Legacy batch layout remains readable for compatibility, but new downloads "
                "should use symbol-date layout."
            ),
        }
    ]


def _base_metadata(
    *,
    kind: str,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    fields: Sequence[str],
    output_root: str | Path,
    batch_size: int,
    storage: dict[str, Any],
    trade_dates: Sequence[str],
    calendar_source: str,
    adjust_type: str,
    time_slice: str | None,
) -> dict[str, Any]:
    return {
        "run_id": uuid.uuid4().hex,
        "kind": kind,
        "provider": "rqdata",
        "market": "hk",
        "frequency": "tick",
        "start_date": format_date(start_date),
        "end_date": format_date(end_date),
        "symbols_requested": list(symbols),
        "fields_requested": list(fields),
        "batch_size": batch_size,
        "trade_dates": list(trade_dates),
        "trade_date_count": len(trade_dates),
        "calendar_source": calendar_source,
        "adjust_type": adjust_type,
        "time_slice": time_slice,
        "output_root": str(output_root),
        "storage": storage,
        "deprecations": _layout_deprecations(storage["raw_layout"]),
        "raw_layout": storage["raw_layout"],
        "layout_version": storage["layout_version"],
        "parquet": storage["parquet"],
        "created_at": utc_now_iso(),
        "planned_batches": [],
        "completed_batches": [],
        "skipped_batches": [],
        "failed_batches": [],
        "planned_units": [],
        "completed_units": [],
        "skipped_units": [],
        "invalid_units": [],
        "empty_units": [],
        "failed_units": [],
        "quota_blocked_batches": [],
        "quota_blocked_units": [],
        "audit_path": None,
        "audit_status_counts": {},
        "quota_guard": {},
        "rows": 0,
    }


def _build_download_config(
    *,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    fields: Sequence[str] | None,
    batch_size: int,
    resume: bool,
    continue_on_error: bool,
    dry_run: bool,
    metadata_kind: str,
    raw_layout: str,
    calendar: str,
    adjust_type: str,
    time_slice: str | None,
    parquet_engine: str,
    parquet_compression: str | None,
    parquet_compression_level: int | None,
    retry_max_attempts: int,
    retry_backoff_seconds: float,
    retry_max_backoff_seconds: float,
    quota_guard: bool,
    quota_stop_ratio: float,
    quota_safety_multiplier: float,
    audit_output: str | Path | None,
    metadata_detail_limit: int,
) -> DownloadConfig:
    if quota_stop_ratio <= 0 or quota_stop_ratio > 1:
        raise ValueError("quota_stop_ratio must be in (0, 1].")
    if quota_safety_multiplier <= 0:
        raise ValueError("quota_safety_multiplier must be positive.")
    if metadata_detail_limit < 0:
        raise ValueError("metadata_detail_limit must be non-negative.")
    return DownloadConfig(
        symbols=tuple(symbols),
        start_date=format_date(start_date),
        end_date=format_date(end_date),
        output_root=Path(output_root),
        fields=tuple(fields or DEFAULT_TICK_DEPTH_FIELDS),
        batch_size=batch_size,
        resume=resume,
        continue_on_error=continue_on_error,
        dry_run=dry_run,
        metadata_kind=metadata_kind,
        raw_layout=normalize_raw_layout(raw_layout),
        calendar=normalize_calendar(calendar),
        adjust_type=adjust_type,
        time_slice=time_slice,
        parquet_engine=parquet_engine,
        parquet_compression=parquet_compression,
        parquet_compression_level=parquet_compression_level,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_max_backoff_seconds=retry_max_backoff_seconds,
        quota_guard=quota_guard,
        quota_stop_ratio=quota_stop_ratio,
        quota_safety_multiplier=quota_safety_multiplier,
        audit_output=audit_output,
        metadata_detail_limit=metadata_detail_limit,
    )


def _unit_info(unit: UnitPlan, **extra: Any) -> dict[str, Any]:
    return {
        "trade_date": unit.trade_date,
        "order_book_id": unit.order_book_id,
        "part_path": str(unit.part_path),
        **extra,
    }


def _iter_provider_batches(units: Iterable[UnitPlan], batch_size: int) -> Iterator[ProviderBatch]:
    current_date: str | None = None
    pending: list[UnitPlan] = []
    batch_number = 0
    for unit in units:
        if current_date != unit.trade_date:
            if pending:
                yield ProviderBatch(
                    trade_date=str(current_date),
                    batch_number=batch_number,
                    symbols=tuple(item.order_book_id for item in pending),
                    units=tuple(pending),
                )
            current_date = unit.trade_date
            pending = []
            batch_number = 0
        pending.append(unit)
        if len(pending) == batch_size:
            yield ProviderBatch(
                trade_date=unit.trade_date,
                batch_number=batch_number,
                symbols=tuple(item.order_book_id for item in pending),
                units=tuple(pending),
            )
            pending = []
            batch_number += 1
    if pending:
        yield ProviderBatch(
            trade_date=str(current_date),
            batch_number=batch_number,
            symbols=tuple(item.order_book_id for item in pending),
            units=tuple(pending),
        )


def _provider_batches(units: Sequence[UnitPlan], batch_size: int) -> list[ProviderBatch]:
    return list(_iter_provider_batches(units, batch_size))


def _provider_batch_info(batch: ProviderBatch, **extra: Any) -> dict[str, Any]:
    return {
        "trade_date": batch.trade_date,
        "batch_number": batch.batch_number,
        "symbols": list(batch.symbols),
        "units": [_unit_info(unit) for unit in batch.units],
        **extra,
    }


def _coverage_by_unit(
    rows: Sequence[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        trade_date = row.get("trading_date")
        symbol = row.get("order_book_id")
        if trade_date and symbol:
            indexed.setdefault((str(trade_date), str(symbol)), []).append(row)
    return indexed


def _valid_coverage_for_unit(
    rows_by_unit: dict[tuple[str, str], list[dict[str, Any]]],
    unit: UnitPlan,
) -> dict[str, Any] | None:
    for row in rows_by_unit.get((unit.trade_date, unit.order_book_id), []):
        if row.get("status") == VALID_STATUS:
            return row
    return None


def _invalid_coverage_for_unit(
    rows_by_unit: dict[tuple[str, str], list[dict[str, Any]]],
    unit: UnitPlan,
    coverage_rows: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    for row in rows_by_unit.get((unit.trade_date, unit.order_book_id), []):
        if row.get("status") != VALID_STATUS:
            return row
    unit_path = str(unit.part_path)
    for row in coverage_rows:
        if row.get("file_path") == unit_path and row.get("status") != VALID_STATUS:
            return row
    return None


@dataclass
class SymbolDatePlanner:
    symbols: Sequence[str]
    start_date: str
    end_date: str
    root: Path
    trade_dates: Sequence[str]
    resume: bool
    rows_by_unit: dict[tuple[str, str], list[dict[str, Any]]]
    coverage_rows: Sequence[dict[str, Any]]
    batch_size: int
    run_id: str
    detail_recorder: DownloadMetadataRecorder
    audit_writer: IncrementalAuditWriter | None
    dry_audit_counts: Counter[str]

    def _persist_audit(self, records: list[AuditRecord]) -> None:
        if self.audit_writer is not None:
            self.audit_writer.append(records)
            return
        for status, count in summarize_audit(records).items():
            self.dry_audit_counts[status] += count

    def iter_units_to_download(self) -> Iterator[UnitPlan]:
        skipped_units: list[UnitPlan] = []
        skipped_date: str | None = None
        skipped_batch_number = 0

        def flush_skipped_batch() -> None:
            nonlocal skipped_batch_number
            if not skipped_units:
                return
            batch = ProviderBatch(
                trade_date=str(skipped_date),
                batch_number=skipped_batch_number,
                symbols=tuple(unit.order_book_id for unit in skipped_units),
                units=tuple(skipped_units),
            )
            self.detail_recorder.record(
                "skipped_batches",
                _provider_batch_info(batch, validation_status=VALID_STATUS),
            )
            self._persist_audit(
                [
                    _audit_record(
                        run_id=self.run_id,
                        chunk_id=f"{unit.trade_date}:resume",
                        unit=unit,
                        status="skipped_existing",
                        rows=int(
                            (_valid_coverage_for_unit(self.rows_by_unit, unit) or {}).get(
                                "row_count", 0
                            )
                            or 0
                        ),
                        attempts=0,
                    )
                    for unit in skipped_units
                ]
            )
            skipped_units.clear()
            skipped_batch_number += 1

        for unit in iter_symbol_date_plan(
            self.symbols,
            self.start_date,
            self.end_date,
            self.root,
            trade_dates=self.trade_dates,
        ):
            self.detail_recorder.record("planned_units", _unit_info(unit))
            if skipped_date != unit.trade_date:
                flush_skipped_batch()
                skipped_date = unit.trade_date
                skipped_batch_number = 0
            if self.resume:
                valid = _valid_coverage_for_unit(self.rows_by_unit, unit)
                if valid is not None:
                    self.detail_recorder.record(
                        "skipped_units",
                        _unit_info(
                            unit,
                            validation_status=VALID_STATUS,
                            existing_file_path=valid.get("file_path"),
                            row_count=valid.get("row_count", 0),
                        ),
                    )
                    skipped_units.append(unit)
                    if len(skipped_units) == self.batch_size:
                        flush_skipped_batch()
                    continue
                invalid = _invalid_coverage_for_unit(
                    self.rows_by_unit, unit, self.coverage_rows
                )
                if invalid is not None:
                    self.detail_recorder.record(
                        "invalid_units",
                        _unit_info(
                            unit,
                            validation_status=invalid.get("status"),
                            existing_file_path=invalid.get("file_path"),
                            reason=invalid.get("reason"),
                        ),
                    )
                else:
                    self.detail_recorder.record(
                        "invalid_units",
                        _unit_info(
                            unit,
                            validation_status=STATUS_MISSING,
                            reason="missing local part",
                        ),
                    )
            yield unit
        flush_skipped_batch()


def _fetch_provider_tick_frame(
    *,
    provider: TickDataProvider,
    symbols: Sequence[str],
    trade_date: str,
    fields: Sequence[str],
    adjust_type: str,
    time_slice: str | None,
    retry_max_attempts: int,
    retry_backoff_seconds: float,
    retry_max_backoff_seconds: float,
) -> Any:
    def fetch() -> pd.DataFrame:
        return provider.get_price(
            order_book_ids=symbols,
            start_date=trade_date,
            end_date=trade_date,
            fields=fields,
            adjust_type=adjust_type,
            time_slice=time_slice,
        )

    return retry_provider_call(
        "get_price",
        fetch,
        max_attempts=retry_max_attempts,
        backoff_seconds=retry_backoff_seconds,
        max_backoff_seconds=retry_max_backoff_seconds,
    )


def _mark_quota_guard_availability(metadata: dict[str, Any], guard: dict[str, Any]) -> None:
    metadata["quota_guard"]["available"] = bool(
        metadata["quota_guard"].get("available") or guard.get("available")
    )


def _write_download_checkpoint(
    *,
    provider: TickDataProvider,
    metadata: dict[str, Any],
    detail_recorder: DownloadMetadataRecorder,
    audit_writer: IncrementalAuditWriter,
    metadata_file: Path,
    run_status: str,
) -> None:
    metadata["run_status"] = run_status
    metadata["updated_at"] = utc_now_iso()
    metadata["quota_latest"] = _quota_snapshot(provider)
    metadata["audit_status_counts"] = audit_writer.summary()
    detail_recorder.flush()
    write_json(metadata_file, metadata)


def _finalize_download_run(
    *,
    provider: TickDataProvider,
    metadata: dict[str, Any],
    detail_recorder: DownloadMetadataRecorder,
    audit_writer: IncrementalAuditWriter,
    metadata_file: Path,
    completed: bool,
) -> None:
    metadata["quota_after"] = _quota_snapshot(provider)
    if not completed:
        run_status = "interrupted"
    elif detail_recorder.count("failed_batches"):
        run_status = "completed_with_failures"
    elif detail_recorder.count("quota_blocked_batches"):
        run_status = "completed_with_quota_blocks"
    else:
        run_status = "complete"
    _write_download_checkpoint(
        provider=provider,
        metadata=metadata,
        detail_recorder=detail_recorder,
        audit_writer=audit_writer,
        metadata_file=metadata_file,
        run_status=run_status,
    )
    metadata["metadata_path"] = str(metadata_file)


def _filter_unit_frame(normalized: pd.DataFrame, unit: UnitPlan) -> pd.DataFrame:
    if normalized.empty or not {"order_book_id", "trading_date"}.issubset(normalized.columns):
        return normalized.iloc[0:0].copy()
    symbol_values = normalized["order_book_id"].astype("string")
    date_values = normalized["trading_date"].astype("string")
    mask = (symbol_values == unit.order_book_id) & (date_values == unit.trade_date)
    return normalized.loc[mask].copy()


def _download_symbol_date_tick_depth(
    *,
    provider: TickDataProvider | None,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    fields: Sequence[str],
    batch_size: int,
    resume: bool,
    continue_on_error: bool,
    dry_run: bool,
    metadata_kind: str,
    storage: dict[str, Any],
    trade_dates: Sequence[str],
    calendar_source: str,
    adjust_type: str,
    time_slice: str | None,
    retry_max_attempts: int,
    retry_backoff_seconds: float,
    retry_max_backoff_seconds: float,
    quota_guard_enabled: bool,
    quota_stop_ratio: float,
    quota_safety_multiplier: float,
    audit_output: str | Path | None,
    metadata_detail_limit: int,
) -> dict[str, Any]:
    root = Path(output_root)
    metadata = _base_metadata(
        kind=metadata_kind,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        fields=fields,
        output_root=root,
        batch_size=batch_size,
        storage=storage,
        trade_dates=trade_dates,
        calendar_source=calendar_source,
        adjust_type=adjust_type,
        time_slice=time_slice,
    )
    if not dry_run and provider is None:
        raise DownloadError("A provider is required unless dry_run=True.")
    run_id = str(metadata["run_id"])
    audit_file = Path(audit_output) if audit_output else default_audit_path(root, metadata_kind)
    metadata["audit_path"] = str(audit_file)
    metadata["quota_guard"] = {
        "enabled": quota_guard_enabled,
        "stop_ratio": quota_stop_ratio,
        "safety_multiplier": quota_safety_multiplier,
        "available": False,
    }
    metadata["dry_run"] = dry_run
    detail_recorder = DownloadMetadataRecorder(
        metadata,
        download_detail_path(root, metadata_kind, run_id),
        inline_limit=metadata_detail_limit,
    )

    coverage_rows = scan_raw_coverage(root, requested_fields=fields) if resume else []
    rows_by_unit = _coverage_by_unit(coverage_rows)
    metadata["coverage"] = coverage_summary(coverage_rows)
    audit_writer = None if dry_run else IncrementalAuditWriter(audit_file)
    dry_audit_counts: Counter[str] = Counter()
    planner = SymbolDatePlanner(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        root=root,
        trade_dates=trade_dates,
        resume=resume,
        rows_by_unit=rows_by_unit,
        coverage_rows=coverage_rows,
        batch_size=batch_size,
        run_id=run_id,
        detail_recorder=detail_recorder,
        audit_writer=audit_writer,
        dry_audit_counts=dry_audit_counts,
    )
    provider_batches = _iter_provider_batches(planner.iter_units_to_download(), batch_size)
    if dry_run:
        for batch in provider_batches:
            detail_recorder.record("planned_batches", _provider_batch_info(batch))
        metadata["audit_status_counts"] = {
            status: int(dry_audit_counts.get(status, 0))
            for status in summarize_audit([]).keys()
        }
        metadata["run_status"] = "dry_run"
        detail_recorder.close()
        return metadata

    assert provider is not None
    metadata["quota_before"] = _quota_snapshot(provider)
    metadata_file = metadata_path(root, metadata_kind)
    metadata["metadata_path"] = str(metadata_file)
    writer = storage["parquet"]
    successful_quota_deltas: list[int] = []
    assert audit_writer is not None
    completed = False

    try:
        _write_download_checkpoint(
            provider=provider,
            metadata=metadata,
            detail_recorder=detail_recorder,
            audit_writer=audit_writer,
            metadata_file=metadata_file,
            run_status="running",
        )
        for batch in provider_batches:
            batch_info = _provider_batch_info(batch)
            detail_recorder.record("planned_batches", batch_info)
            chunk_id = f"{batch.trade_date}:{batch.batch_number:04d}"
            quota_before = _quota_snapshot(provider)
            guard = _quota_guard_decision(
                quota_before,
                successful_quota_deltas,
                enabled=quota_guard_enabled,
                stop_ratio=quota_stop_ratio,
                safety_multiplier=quota_safety_multiplier,
            )
            _mark_quota_guard_availability(metadata, guard)
            batch_info["quota_before"] = quota_before
            batch_info["quota_guard"] = guard
            if guard["blocked"]:
                batch_info["category"] = "quota_guard"
                batch_info["error"] = "quota guard blocked provider request"
                detail_recorder.record("quota_blocked_batches", batch_info)
                batch_audit_records: list[AuditRecord] = []
                for unit in batch.units:
                    info = _unit_info(
                        unit,
                        category="quota_guard",
                        estimated_next_delta_bytes=guard.get("estimated_next_delta_bytes"),
                    )
                    detail_recorder.record("quota_blocked_units", info)
                    batch_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            unit=unit,
                            status="quota_blocked",
                            quota_before=quota_before,
                            error_type="quota_guard",
                            error_message="quota guard blocked provider request",
                        )
                    )
                audit_writer.append(batch_audit_records)
                _write_download_checkpoint(
                    provider=provider,
                    metadata=metadata,
                    detail_recorder=detail_recorder,
                    audit_writer=audit_writer,
                    metadata_file=metadata_file,
                    run_status="running",
                )
                continue

            started_at = utc_now_iso()
            started_clock = perf_counter()
            try:
                result = _fetch_provider_tick_frame(
                    provider=provider,
                    symbols=batch.symbols,
                    trade_date=batch.trade_date,
                    fields=fields,
                    adjust_type=adjust_type,
                    time_slice=time_slice,
                    retry_max_attempts=retry_max_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                    retry_max_backoff_seconds=retry_max_backoff_seconds,
                )
                raw = result.value
                quota_after = _quota_snapshot(provider)
                quota_delta = _quota_delta(quota_before, quota_after)
                if quota_delta:
                    successful_quota_deltas.append(quota_delta)
                normalized = normalize_tick_frame(raw, fields)
                batch_rows = 0
                batch_columns = list(normalized.columns)
                finished_at = utc_now_iso()
                duration_seconds = round(perf_counter() - started_clock, 6)
                batch_audit_records = []
                for unit in batch.units:
                    unit_frame = _filter_unit_frame(normalized, unit)
                    atomic_write_parquet(unit_frame, unit.part_path, **writer)
                    row_count = int(len(unit_frame))
                    batch_rows += row_count
                    status = "written"
                    if row_count == 0:
                        status = "empty_remote"
                        detail_recorder.record(
                            "empty_units",
                            _unit_info(unit, reason="provider returned no rows")
                        )
                    detail_recorder.record(
                        "completed_units",
                        _unit_info(
                            unit,
                            rows=row_count,
                            columns=list(unit_frame.columns),
                            attempts=result.attempts,
                            quota_delta_bytes=quota_delta,
                        )
                    )
                    batch_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            unit=unit,
                            status=status,
                            rows=row_count,
                            started_at=started_at,
                            finished_at=finished_at,
                            duration_seconds=duration_seconds,
                            quota_before=quota_before,
                            quota_after=quota_after,
                            quota_delta=quota_delta,
                            attempts=result.attempts,
                        )
                    )
                batch_info["rows"] = batch_rows
                batch_info["columns"] = batch_columns
                batch_info["attempts"] = result.attempts
                batch_info["quota_after"] = quota_after
                batch_info["quota_delta_bytes"] = quota_delta
                metadata["rows"] += batch_rows
                detail_recorder.record("completed_batches", batch_info)
                audit_writer.append(batch_audit_records)
                _write_download_checkpoint(
                    provider=provider,
                    metadata=metadata,
                    detail_recorder=detail_recorder,
                    audit_writer=audit_writer,
                    metadata_file=metadata_file,
                    run_status="running",
                )
            except Exception as exc:
                category = getattr(exc, "category", "download_error")
                quota_after = _quota_snapshot(provider)
                quota_delta = _quota_delta(quota_before, quota_after)
                failed_batch = {
                    **batch_info,
                    "category": category,
                    "error": str(exc),
                    "quota_after": quota_after,
                    "quota_delta_bytes": quota_delta,
                }
                detail_recorder.record("failed_batches", failed_batch)
                batch_audit_records = []
                for unit in batch.units:
                    if category == "quota":
                        detail_recorder.record(
                            "quota_blocked_units",
                            _unit_info(unit, category=category, error=str(exc))
                        )
                    else:
                        detail_recorder.record(
                            "failed_units",
                            _unit_info(unit, category=category, error=str(exc))
                        )
                    batch_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            unit=unit,
                            status="quota_blocked" if category == "quota" else "failed",
                            started_at=started_at,
                            finished_at=utc_now_iso(),
                            duration_seconds=round(perf_counter() - started_clock, 6),
                            quota_before=quota_before,
                            quota_after=quota_after,
                            quota_delta=quota_delta,
                            error_type=str(category),
                            error_message=str(exc),
                        )
                    )
                audit_writer.append(batch_audit_records)
                _write_download_checkpoint(
                    provider=provider,
                    metadata=metadata,
                    detail_recorder=detail_recorder,
                    audit_writer=audit_writer,
                    metadata_file=metadata_file,
                    run_status="running",
                )
                if category == "quota" or not continue_on_error:
                    raise
        completed = True
    finally:
        try:
            _finalize_download_run(
                provider=provider,
                metadata=metadata,
                detail_recorder=detail_recorder,
                audit_writer=audit_writer,
                metadata_file=metadata_file,
                completed=completed,
            )
        finally:
            detail_recorder.close()

    if detail_recorder.count("failed_batches") and not continue_on_error:
        raise DownloadError("Download failed before completion.")
    return metadata


def _batch_part_valid(
    part_path: Path,
    *,
    trade_date: str,
    symbols: Sequence[str],
    fields: Sequence[str],
) -> tuple[bool, list[dict[str, Any]]]:
    if not part_path.exists():
        return False, [
            {
                "status": STATUS_MISSING,
                "file_path": str(part_path),
                "reason": "missing local part",
            }
        ]
    rows = scan_raw_coverage(part_path, requested_fields=fields)
    valid_symbols = {
        str(row["order_book_id"])
        for row in rows
        if row.get("status") == VALID_STATUS and row.get("trading_date") == trade_date
    }
    return set(symbols).issubset(valid_symbols), rows


def _download_batch_tick_depth(
    *,
    provider: TickDataProvider | None,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    fields: Sequence[str],
    batch_size: int,
    resume: bool,
    continue_on_error: bool,
    dry_run: bool,
    metadata_kind: str,
    storage: dict[str, Any],
    trade_dates: Sequence[str],
    calendar_source: str,
    adjust_type: str,
    time_slice: str | None,
    retry_max_attempts: int,
    retry_backoff_seconds: float,
    retry_max_backoff_seconds: float,
    quota_guard_enabled: bool,
    quota_stop_ratio: float,
    quota_safety_multiplier: float,
    audit_output: str | Path | None,
    metadata_detail_limit: int,
) -> dict[str, Any]:
    root = Path(output_root)
    plans = build_batch_plan(
        symbols,
        start_date,
        end_date,
        root,
        batch_size,
        trade_dates=trade_dates,
    )
    metadata = _base_metadata(
        kind=metadata_kind,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        fields=fields,
        output_root=root,
        batch_size=batch_size,
        storage=storage,
        trade_dates=trade_dates,
        calendar_source=calendar_source,
        adjust_type=adjust_type,
        time_slice=time_slice,
    )
    if not dry_run and provider is None:
        raise DownloadError("A provider is required unless dry_run=True.")
    run_id = str(metadata["run_id"])
    preflight_audit_records: list[AuditRecord] = []
    audit_file = Path(audit_output) if audit_output else default_audit_path(root, metadata_kind)
    metadata["audit_path"] = str(audit_file)
    metadata["quota_guard"] = {
        "enabled": quota_guard_enabled,
        "stop_ratio": quota_stop_ratio,
        "safety_multiplier": quota_safety_multiplier,
        "available": False,
    }
    detail_recorder = DownloadMetadataRecorder(
        metadata,
        download_detail_path(root, metadata_kind, run_id),
        inline_limit=metadata_detail_limit,
    )
    for plan in plans:
        for symbol in plan.symbols:
            detail_recorder.record(
                "planned_units",
                {
                    "trade_date": plan.trade_date,
                    "order_book_id": symbol,
                    "part_path": str(plan.part_path),
                },
            )
    metadata["coverage"] = coverage_summary(scan_raw_coverage(root, requested_fields=fields))
    metadata["dry_run"] = dry_run

    plans_to_download: list[BatchPlan] = []
    for plan in plans:
        batch_info = {
            "trade_date": plan.trade_date,
            "batch_number": plan.batch_number,
            "symbols": list(plan.symbols),
            "part_path": str(plan.part_path),
        }
        if resume:
            is_valid, rows = _batch_part_valid(
                plan.part_path,
                trade_date=plan.trade_date,
                symbols=plan.symbols,
                fields=fields,
            )
            if is_valid:
                detail_recorder.record(
                    "skipped_batches",
                    {**batch_info, "validation_status": VALID_STATUS}
                )
                for symbol in plan.symbols:
                    row = next(
                        (
                            item
                            for item in rows
                            if item.get("status") == VALID_STATUS
                            and item.get("trading_date") == plan.trade_date
                            and item.get("order_book_id") == symbol
                        ),
                        {},
                    )
                    detail_recorder.record(
                        "skipped_units",
                        {
                            "trade_date": plan.trade_date,
                            "order_book_id": symbol,
                            "part_path": str(plan.part_path),
                            "validation_status": VALID_STATUS,
                            "existing_file_path": row.get("file_path", str(plan.part_path)),
                            "row_count": row.get("row_count", 0),
                        }
                    )
                    preflight_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=f"{plan.trade_date}:{plan.batch_number:04d}:resume",
                            unit=UnitPlan(plan.trade_date, symbol, plan.part_path),
                            status="skipped_existing",
                            rows=int(row.get("row_count") or 0),
                            attempts=0,
                        )
                    )
                continue
            for symbol in plan.symbols:
                invalid_row = next(
                    (
                        item
                        for item in rows
                        if item.get("order_book_id") in {symbol, None}
                        or item.get("file_path") == str(plan.part_path)
                    ),
                    {"status": STATUS_MISSING, "reason": "missing local part"},
                )
                detail_recorder.record(
                    "invalid_units",
                    {
                        "trade_date": plan.trade_date,
                        "order_book_id": symbol,
                        "part_path": str(plan.part_path),
                        "validation_status": invalid_row.get("status"),
                        "existing_file_path": invalid_row.get("file_path"),
                        "reason": invalid_row.get("reason"),
                    }
                )
        plans_to_download.append(plan)

    for plan in plans_to_download:
        detail_recorder.record(
            "planned_batches",
            {
                "trade_date": plan.trade_date,
                "batch_number": plan.batch_number,
                "symbols": list(plan.symbols),
                "part_path": str(plan.part_path),
            },
        )

    if dry_run:
        metadata["audit_status_counts"] = summarize_audit(preflight_audit_records)
        metadata["run_status"] = "dry_run"
        detail_recorder.close()
        return metadata

    assert provider is not None
    metadata["quota_before"] = _quota_snapshot(provider)
    metadata_file = metadata_path(root, metadata_kind)
    metadata["metadata_path"] = str(metadata_file)
    writer = storage["parquet"]
    successful_quota_deltas: list[int] = []
    audit_writer = IncrementalAuditWriter(audit_file)
    audit_writer.append(preflight_audit_records)
    completed = False

    try:
        _write_download_checkpoint(
            provider=provider,
            metadata=metadata,
            detail_recorder=detail_recorder,
            audit_writer=audit_writer,
            metadata_file=metadata_file,
            run_status="running",
        )
        for plan in plans_to_download:
            batch_info = {
                "trade_date": plan.trade_date,
                "batch_number": plan.batch_number,
                "symbols": list(plan.symbols),
                "part_path": str(plan.part_path),
            }
            chunk_id = f"{plan.trade_date}:{plan.batch_number:04d}"
            quota_before = _quota_snapshot(provider)
            guard = _quota_guard_decision(
                quota_before,
                successful_quota_deltas,
                enabled=quota_guard_enabled,
                stop_ratio=quota_stop_ratio,
                safety_multiplier=quota_safety_multiplier,
            )
            _mark_quota_guard_availability(metadata, guard)
            batch_info["quota_before"] = quota_before
            batch_info["quota_guard"] = guard
            if guard["blocked"]:
                batch_info["category"] = "quota_guard"
                batch_info["error"] = "quota guard blocked provider request"
                detail_recorder.record("quota_blocked_batches", batch_info)
                batch_audit_records: list[AuditRecord] = []
                for symbol in plan.symbols:
                    unit = UnitPlan(plan.trade_date, symbol, plan.part_path)
                    detail_recorder.record(
                        "quota_blocked_units",
                        _unit_info(
                            unit,
                            category="quota_guard",
                            estimated_next_delta_bytes=guard.get("estimated_next_delta_bytes"),
                        )
                    )
                    batch_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            unit=unit,
                            status="quota_blocked",
                            quota_before=quota_before,
                            error_type="quota_guard",
                            error_message="quota guard blocked provider request",
                        )
                    )
                audit_writer.append(batch_audit_records)
                _write_download_checkpoint(
                    provider=provider,
                    metadata=metadata,
                    detail_recorder=detail_recorder,
                    audit_writer=audit_writer,
                    metadata_file=metadata_file,
                    run_status="running",
                )
                continue

            started_at = utc_now_iso()
            started_clock = perf_counter()
            try:
                result = _fetch_provider_tick_frame(
                    provider=provider,
                    symbols=plan.symbols,
                    trade_date=plan.trade_date,
                    fields=fields,
                    adjust_type=adjust_type,
                    time_slice=time_slice,
                    retry_max_attempts=retry_max_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                    retry_max_backoff_seconds=retry_max_backoff_seconds,
                )
                raw = result.value
                quota_after = _quota_snapshot(provider)
                quota_delta = _quota_delta(quota_before, quota_after)
                if quota_delta:
                    successful_quota_deltas.append(quota_delta)
                normalized = normalize_tick_frame(raw, fields)
                atomic_write_parquet(normalized, plan.part_path, **writer)
                finished_at = utc_now_iso()
                duration_seconds = round(perf_counter() - started_clock, 6)
                batch_info["rows"] = int(len(normalized))
                batch_info["columns"] = list(normalized.columns)
                batch_info["attempts"] = result.attempts
                batch_info["quota_after"] = quota_after
                batch_info["quota_delta_bytes"] = quota_delta
                metadata["rows"] += int(len(normalized))
                detail_recorder.record("completed_batches", batch_info)
                batch_audit_records = []
                for symbol in plan.symbols:
                    unit = UnitPlan(plan.trade_date, symbol, plan.part_path)
                    unit_frame = _filter_unit_frame(
                        normalized,
                        unit,
                    )
                    unit_rows = int(len(unit_frame))
                    status = "written"
                    if unit_rows == 0:
                        status = "empty_remote"
                        detail_recorder.record(
                            "empty_units",
                            {
                                "trade_date": plan.trade_date,
                                "order_book_id": symbol,
                                "part_path": str(plan.part_path),
                                "reason": "provider returned no rows",
                            }
                        )
                    detail_recorder.record(
                        "completed_units",
                        {
                            "trade_date": plan.trade_date,
                            "order_book_id": symbol,
                            "part_path": str(plan.part_path),
                            "rows": unit_rows,
                            "columns": list(unit_frame.columns),
                            "attempts": result.attempts,
                            "quota_delta_bytes": quota_delta,
                        }
                    )
                    batch_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            unit=unit,
                            status=status,
                            rows=unit_rows,
                            started_at=started_at,
                            finished_at=finished_at,
                            duration_seconds=duration_seconds,
                            quota_before=quota_before,
                            quota_after=quota_after,
                            quota_delta=quota_delta,
                            attempts=result.attempts,
                        )
                    )
                audit_writer.append(batch_audit_records)
                _write_download_checkpoint(
                    provider=provider,
                    metadata=metadata,
                    detail_recorder=detail_recorder,
                    audit_writer=audit_writer,
                    metadata_file=metadata_file,
                    run_status="running",
                )
            except Exception as exc:
                category = getattr(exc, "category", "download_error")
                quota_after = _quota_snapshot(provider)
                quota_delta = _quota_delta(quota_before, quota_after)
                failed_info = {
                    **batch_info,
                    "category": category,
                    "error": str(exc),
                    "quota_after": quota_after,
                    "quota_delta_bytes": quota_delta,
                }
                detail_recorder.record("failed_batches", failed_info)
                batch_audit_records = []
                for symbol in plan.symbols:
                    unit = UnitPlan(plan.trade_date, symbol, plan.part_path)
                    if category == "quota":
                        detail_recorder.record(
                            "quota_blocked_units",
                            _unit_info(unit, category=category, error=str(exc))
                        )
                    else:
                        detail_recorder.record(
                            "failed_units",
                            _unit_info(unit, category=category, error=str(exc))
                        )
                    batch_audit_records.append(
                        _audit_record(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            unit=unit,
                            status="quota_blocked" if category == "quota" else "failed",
                            started_at=started_at,
                            finished_at=utc_now_iso(),
                            duration_seconds=round(perf_counter() - started_clock, 6),
                            quota_before=quota_before,
                            quota_after=quota_after,
                            quota_delta=quota_delta,
                            error_type=str(category),
                            error_message=str(exc),
                        )
                    )
                audit_writer.append(batch_audit_records)
                _write_download_checkpoint(
                    provider=provider,
                    metadata=metadata,
                    detail_recorder=detail_recorder,
                    audit_writer=audit_writer,
                    metadata_file=metadata_file,
                    run_status="running",
                )
                if category == "quota" or not continue_on_error:
                    raise
        completed = True
    finally:
        try:
            _finalize_download_run(
                provider=provider,
                metadata=metadata,
                detail_recorder=detail_recorder,
                audit_writer=audit_writer,
                metadata_file=metadata_file,
                completed=completed,
            )
        finally:
            detail_recorder.close()

    if detail_recorder.count("failed_batches") and not continue_on_error:
        raise DownloadError("Download failed before completion.")
    return metadata


def download_tick_depth(
    *,
    provider: TickDataProvider | None,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    output_root: str | Path,
    fields: Sequence[str] | None = None,
    batch_size: int = 5,
    resume: bool = True,
    continue_on_error: bool = False,
    dry_run: bool = False,
    metadata_kind: str = "download",
    raw_layout: str = "symbol-date",
    calendar: str = "provider",
    adjust_type: str = "none",
    time_slice: str | None = None,
    parquet_engine: str = DEFAULT_PARQUET_ENGINE,
    parquet_compression: str | None = DEFAULT_PARQUET_COMPRESSION,
    parquet_compression_level: int | None = None,
    retry_max_attempts: int = 1,
    retry_backoff_seconds: float = 0.0,
    retry_max_backoff_seconds: float = 60.0,
    quota_guard: bool = True,
    quota_stop_ratio: float = 0.95,
    quota_safety_multiplier: float = 1.2,
    audit_output: str | Path | None = None,
    metadata_detail_limit: int = 1000,
) -> dict[str, Any]:
    """Download ten-level depth snapshots into parquet parts and write run metadata."""
    config = _build_download_config(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        output_root=output_root,
        fields=fields,
        batch_size=batch_size,
        resume=resume,
        continue_on_error=continue_on_error,
        dry_run=dry_run,
        metadata_kind=metadata_kind,
        raw_layout=raw_layout,
        calendar=calendar,
        adjust_type=adjust_type,
        time_slice=time_slice,
        parquet_engine=parquet_engine,
        parquet_compression=parquet_compression,
        parquet_compression_level=parquet_compression_level,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_max_backoff_seconds=retry_max_backoff_seconds,
        quota_guard=quota_guard,
        quota_stop_ratio=quota_stop_ratio,
        quota_safety_multiplier=quota_safety_multiplier,
        audit_output=audit_output,
        metadata_detail_limit=metadata_detail_limit,
    )
    trade_dates, calendar_source = _resolve_trade_dates(
        provider=provider,
        start_date=config.start_date,
        end_date=config.end_date,
        calendar=config.calendar,
    )
    storage = _storage_settings(
        raw_layout=config.raw_layout,
        parquet_engine=config.parquet_engine,
        parquet_compression=config.parquet_compression,
        parquet_compression_level=config.parquet_compression_level,
    )
    if config.raw_layout == "batch":
        return _download_batch_tick_depth(
            provider=provider,
            symbols=config.symbols,
            start_date=config.start_date,
            end_date=config.end_date,
            output_root=config.output_root,
            fields=config.fields,
            batch_size=config.batch_size,
            resume=config.resume,
            continue_on_error=config.continue_on_error,
            dry_run=config.dry_run,
            metadata_kind=config.metadata_kind,
            storage=storage,
            trade_dates=trade_dates,
            calendar_source=calendar_source,
            adjust_type=config.adjust_type,
            time_slice=config.time_slice,
            retry_max_attempts=config.retry_max_attempts,
            retry_backoff_seconds=config.retry_backoff_seconds,
            retry_max_backoff_seconds=config.retry_max_backoff_seconds,
            quota_guard_enabled=config.quota_guard,
            quota_stop_ratio=config.quota_stop_ratio,
            quota_safety_multiplier=config.quota_safety_multiplier,
            audit_output=config.audit_output,
            metadata_detail_limit=config.metadata_detail_limit,
        )
    return _download_symbol_date_tick_depth(
        provider=provider,
        symbols=config.symbols,
        start_date=config.start_date,
        end_date=config.end_date,
        output_root=config.output_root,
        fields=config.fields,
        batch_size=config.batch_size,
        resume=config.resume,
        continue_on_error=config.continue_on_error,
        dry_run=config.dry_run,
        metadata_kind=config.metadata_kind,
        storage=storage,
        trade_dates=trade_dates,
        calendar_source=calendar_source,
        adjust_type=config.adjust_type,
        time_slice=config.time_slice,
        retry_max_attempts=config.retry_max_attempts,
        retry_backoff_seconds=config.retry_backoff_seconds,
        retry_max_backoff_seconds=config.retry_max_backoff_seconds,
        quota_guard_enabled=config.quota_guard,
        quota_stop_ratio=config.quota_stop_ratio,
        quota_safety_multiplier=config.quota_safety_multiplier,
        audit_output=config.audit_output,
        metadata_detail_limit=config.metadata_detail_limit,
    )


def probe_tick_depth(
    *,
    provider: TickDataProvider,
    symbol: str,
    trade_date: str,
    output_root: str | Path,
    fields: Sequence[str] | None = None,
    adjust_type: str = "none",
    time_slice: str | None = None,
) -> dict[str, Any]:
    """Run a one-symbol one-day probe and return a compact summary."""
    metadata = download_tick_depth(
        provider=provider,
        symbols=[symbol],
        start_date=trade_date,
        end_date=trade_date,
        output_root=output_root,
        fields=fields,
        batch_size=1,
        resume=False,
        continue_on_error=False,
        metadata_kind="probe",
        calendar="calendar",
        adjust_type=adjust_type,
        time_slice=time_slice,
    )
    completed = metadata["completed_units"][0] if metadata["completed_units"] else {}
    summary = {
        "symbol": symbol,
        "trade_date": format_date(trade_date),
        "rows": metadata["rows"],
        "columns": completed.get("columns", []),
        "parquet_path": completed.get("part_path"),
        "metadata_path": metadata.get("metadata_path"),
    }
    if not metadata["rows"]:
        summary["warning"] = (
            "provider returned zero rows; check the trade date, symbol, suspension status, "
            "and account tick-history entitlement window"
        )

    if completed.get("part_path"):
        import pandas as pd

        df = pd.read_parquet(completed["part_path"])
        if "datetime" in df.columns and not df.empty:
            timestamps = pd.to_datetime(df["datetime"], errors="coerce")
            summary["first_timestamp"] = timestamps.min().isoformat()
            summary["last_timestamp"] = timestamps.max().isoformat()
        else:
            summary["first_timestamp"] = None
            summary["last_timestamp"] = None
    return summary


def provider_error_to_exit(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, ProviderRequestError):
        return 2, str(exc)
    return 1, str(exc)
