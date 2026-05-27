from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from market_data_platform.data_providers import _to_rqdata_symbol
from .asset_io import (
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
    HK_INDUSTRY_HIERARCHY_COLUMNS,
    _build_hk_industry_catalog,
    _prepare_hk_industry_change_frame,
    _resolve_hk_industry_change_level,
    _resolve_hk_industry_source,
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


def mirror_hk_industry_changes(args, rqdatac) -> int:
    source = _resolve_hk_industry_source(args)
    level = _resolve_hk_industry_change_level(args)
    symbols, symbol_metadata = _resolve_symbols(args)
    start_date = _normalize_absolute_date(args.start_date, label="--start-date")
    end_date = _normalize_absolute_date(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    mapping_date = getattr(args, "mapping_date", None)
    mapping_date = _normalize_absolute_date(mapping_date, label="--mapping-date") if mapping_date else end_date
    catalog = _build_hk_industry_catalog(
        rqdatac,
        source=source,
        level=level,
        mapping_date=mapping_date,
    )
    industries = catalog["industry_code"].astype(str).tolist()
    resume = bool(getattr(args, "resume", False))
    skip_existing = bool(getattr(args, "skip_existing", False) or resume)
    max_attempts = max(1, int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1))
    backoff_seconds = float(getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS))
    max_backoff_seconds = float(
        getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
    )
    output_dir = _prepare_daily_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="industry_changes",
        start_date=start_date,
        end_date=end_date,
        name=getattr(args, "name", None),
        resume=resume,
    )
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "audit.csv"
    catalog_path = output_dir / "industry_catalog.parquet"

    symbol_map = {_to_rqdata_symbol("hk", symbol): symbol for symbol in symbols}
    order_book_ids = list(symbol_map.keys())
    fields = [
        "cancel_date",
        "industry_code",
        "industry_name",
        "industry_level",
        "industry_source",
        *HK_INDUSTRY_HIERARCHY_COLUMNS,
    ]
    field_metadata = {
        "count": len(fields),
        "fields_file": [],
        "source": f"industry_mapping_level_{level}",
        "base_fields": list(fields),
    }
    entries_by_symbol: dict[str, DatedMirrorEntry] = {}
    audit_by_symbol: dict[str, DatedMirrorAuditRecord] = {}
    frames_by_symbol: dict[str, list[pd.DataFrame]] = {}
    batches: list[dict[str, object]] = []
    columns: list[str] = []
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

    try:
        if resume:
            _validate_dated_resume_inputs(
                output_dir=output_dir,
                dataset_name="industry_changes",
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
                if query.get("mapping_date") not in {None, mapping_date}:
                    raise SystemExit(
                        f"Resume target query mismatch for mapping_date: expected {mapping_date!r}, got {query.get('mapping_date')!r}."
                    )
            existing_industries = _load_existing_text_list(output_dir / "industries.txt", strip=False)
            if existing_industries and list(existing_industries) != list(industries):
                raise SystemExit("Resume target industries.txt does not match the requested industry list.")

        _write_text_list(output_dir / "fields.txt", fields)
        _write_text_list(output_dir / "symbols.txt", symbols)
        _write_text_list(output_dir / "industries.txt", industries)
        catalog.to_parquet(catalog_path, index=False)

        pending_order_book_ids: list[str] = []
        for order_book_id in order_book_ids:
            symbol = symbol_map[order_book_id]
            out_path = data_dir / f"{symbol}.parquet"
            if skip_existing and out_path.exists():
                try:
                    entry, symbol_frame = _load_existing_dated_entry(
                        out_path,
                        date_column="start_date",
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

        pending_symbol_set = {symbol_map[order_book_id] for order_book_id in pending_order_book_ids}
        for industry_row in catalog.itertuples(index=False):
            if quota_blocked or not pending_symbol_set:
                break
            industry_code = str(getattr(industry_row, "industry_code"))
            industry_name = str(getattr(industry_row, "industry_name"))
            batch_started_at = _timestamp_now()
            try:
                payload, attempts = _retry_fetch(
                    f"industry change fetch failed for {industry_code}",
                    lambda: rqdatac.get_industry_change(
                        industry=industry_code,
                        source=source,
                        level=level,
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
                        "industry_code": industry_code,
                        "industry_name": industry_name,
                        "rows": 0,
                        "status": "quota_blocked",
                        "attempts": exc.attempts,
                        "error": str(exc),
                    }
                )
                break
            except MirrorFetchError as exc:
                batches.append(
                    {
                        "industry_code": industry_code,
                        "industry_name": industry_name,
                        "rows": 0,
                        "status": "failed",
                        "attempts": exc.attempts,
                        "error": str(exc),
                    }
                )
                if status == "completed":
                    status = "completed_with_failures"
                result_code = max(result_code, 1)
                continue

            prepared = _prepare_hk_industry_change_frame(
                payload,
                catalog_row=industry_row._asdict(),
                symbol_filter=pending_symbol_set,
                start_date=start_date,
                end_date=end_date,
            )
            prepared = _ensure_requested_fields(prepared, fields)
            batches.append(
                {
                    "industry_code": industry_code,
                    "industry_name": industry_name,
                    "rows": int(len(prepared)),
                    "status": "completed",
                    "attempts": attempts,
                    "started_at": batch_started_at,
                    "finished_at": _timestamp_now(),
                }
            )
            if prepared.empty:
                continue
            for symbol in prepared["symbol"].drop_duplicates().tolist():
                symbol_frame = prepared[prepared["symbol"] == symbol].reset_index(drop=True)
                if symbol_frame.empty:
                    continue
                frames_by_symbol.setdefault(symbol, []).append(symbol_frame)

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
                combined = combined.drop_duplicates(
                    subset=["start_date", "industry_code"],
                    keep="last",
                )
                combined = combined.sort_values(
                    [column for column in ("start_date", "cancel_date", "industry_code") if column in combined.columns]
                ).reset_index(drop=True)
                entry = _write_dated_symbol_frame(data_dir, combined, date_column="start_date")
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
            dataset_name="industry_changes",
            api_name="rqdatac.get_industry_change",
            output_dir=output_dir,
            fields=fields,
            field_metadata=field_metadata,
            symbol_metadata=symbol_metadata,
            symbols_requested=symbols,
            entries=[entries_by_symbol[symbol] for symbol in symbols if symbol in entries_by_symbol],
            missing_symbols=[item.symbol for item in audit_records if item.status == "missing_remote"],
            start_date=start_date,
            end_date=end_date,
            date_column="start_date",
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
            manifest_query["mapping_date"] = mapping_date
            manifest_query["industries_count"] = len(industries)
            manifest_query["industries_file"] = str(output_dir / "industries.txt")
        manifest["industry_catalog_file"] = str(catalog_path)
        _write_manifest(output_dir / "manifest.yml", manifest)

    totals = {
        "files": len(entries_by_symbol),
        "symbols": len(entries_by_symbol),
        "rows": sum(item.rows for item in entries_by_symbol.values()),
        "bytes": sum(item.total_bytes for item in entries_by_symbol.values()),
    }
    print(
        f"Wrote industry_changes mirror to {output_dir} "
        f"({totals['symbols']} symbols, {totals['files']} files, {totals['rows']} rows, {totals['bytes']} bytes, status={status})"
    )
    return result_code
