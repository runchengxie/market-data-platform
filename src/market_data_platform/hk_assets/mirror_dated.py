from __future__ import annotations

from pathlib import Path

import pandas as pd

from .fetch_runtime import (
    _ensure_rqdatac_hk_plugin as _ensure_rqdatac_hk_plugin_runtime,
    _fetch_hk_dividends_direct,
    _fetch_hk_ex_factors_direct,
    _fetch_hk_shares_direct,
    _retry_fetch,
)
from .manifest_ops import _validate_global_daily_resume_inputs
from .mirror_workflow import _mirror_dated_dataset
from .models import MirrorQuotaError
from .package_api import _package_attr
from .request_groups import (
    _normalize_hk_dated_payload,
    _normalize_hk_valuation_payload,
    _resolve_hk_dated_request_groups,
    _uses_hk_unique_ids,
)
from .shared import (
    DEFAULT_HK_EXCHANGE_RATE_FIELDS,
    DEFAULT_HK_SHARES_FIELDS,
    DEFAULT_HK_VALUATION_FIELDS,
    _git_metadata,
    _normalize_absolute_date,
    _normalize_frame_columns,
    _prepare_daily_output_dir,
    _resolve_default_plus_explicit_fields,
    _resolve_optional_explicit_fields,
    _split_daily_range_by_year,
    _timestamp_now,
    _write_text_list,
    _write_manifest,
)

DEFAULT_MIRROR_BACKOFF_SECONDS = _package_attr("DEFAULT_MIRROR_BACKOFF_SECONDS")
DEFAULT_MIRROR_MAX_ATTEMPTS = _package_attr("DEFAULT_MIRROR_MAX_ATTEMPTS")
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = _package_attr(
    "DEFAULT_MIRROR_MAX_BACKOFF_SECONDS"
)
DEFAULT_OUT_ROOT = _package_attr("DEFAULT_OUT_ROOT")


def _ensure_rqdatac_hk_plugin() -> None:
    ensure_plugin = _package_attr(
        "_ensure_rqdatac_hk_plugin",
        default=_ensure_rqdatac_hk_plugin_runtime,
    )
    ensure_plugin()


def mirror_hk_valuation(args, rqdatac) -> int:
    fields, field_metadata = _resolve_default_plus_explicit_fields(
        args,
        default_fields=DEFAULT_HK_VALUATION_FIELDS,
        source_label="default_plus_explicit",
    )
    return _mirror_dated_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="valuation",
        api_name="rqdatac.get_factor",
        date_column="trade_date",
        fields=fields,
        field_metadata=field_metadata,
        resolve_request_groups=lambda symbols, start_date, end_date, args: _resolve_hk_dated_request_groups(
            symbols,
            start_date=start_date,
            end_date=end_date,
            out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        ),
        normalize_payload=_normalize_hk_valuation_payload,
        fetch_batch=lambda order_book_ids, selected_fields, start_date, end_date: rqdatac.get_factor(
            list(order_book_ids),
            list(selected_fields),
            start_date,
            end_date,
            market="hk",
        ),
    )


def mirror_hk_ex_factors(args, rqdatac) -> int:
    return _mirror_dated_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="ex_factors",
        api_name="rqdatac.get_ex_factor",
        date_column="ex_date",
        fields=[],
        field_metadata={
            "count": 0,
            "fields_file": [],
            "source": "api_payload",
            "base_fields": [],
        },
        sort_columns=("announcement_date", "ex_end_date"),
        resolve_request_groups=lambda symbols, start_date, end_date, args: _resolve_hk_dated_request_groups(
            symbols,
            start_date=start_date,
            end_date=end_date,
            out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        ),
        normalize_payload=_normalize_hk_dated_payload,
        fetch_batch=lambda order_book_ids, fields, start_date, end_date: (
            _fetch_hk_ex_factors_direct(
                order_book_ids,
                start_date=start_date,
                end_date=end_date,
            )
            if _uses_hk_unique_ids(order_book_ids)
            else rqdatac.get_ex_factor(
                order_book_ids,
                start_date=start_date,
                end_date=end_date,
                market="hk",
            )
        ),
    )


def mirror_hk_dividends(args, rqdatac) -> int:
    return _mirror_dated_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="dividends",
        api_name="rqdatac.get_dividend",
        date_column="declaration_announcement_date",
        fields=[],
        field_metadata={
            "count": 0,
            "fields_file": [],
            "source": "api_payload",
            "base_fields": [],
        },
        sort_columns=("ex_dividend_date", "payable_date"),
        resolve_request_groups=lambda symbols, start_date, end_date, args: _resolve_hk_dated_request_groups(
            symbols,
            start_date=start_date,
            end_date=end_date,
            out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        ),
        normalize_payload=_normalize_hk_dated_payload,
        fetch_batch=lambda order_book_ids, fields, start_date, end_date: (
            _fetch_hk_dividends_direct(
                order_book_ids,
                start_date=start_date,
                end_date=end_date,
            )
            if _uses_hk_unique_ids(order_book_ids)
            else rqdatac.get_dividend(
                order_book_ids,
                start_date=start_date,
                end_date=end_date,
                market="hk",
            )
        ),
    )


def mirror_hk_shares(args, rqdatac) -> int:
    fields, field_metadata = _resolve_default_plus_explicit_fields(
        args,
        default_fields=DEFAULT_HK_SHARES_FIELDS,
        source_label="default_plus_explicit",
    )
    return _mirror_dated_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="shares",
        api_name="rqdatac.get_shares",
        date_column="date",
        fields=fields,
        field_metadata=field_metadata,
        resolve_request_groups=lambda symbols, start_date, end_date, args: _resolve_hk_dated_request_groups(
            symbols,
            start_date=start_date,
            end_date=end_date,
            out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        ),
        normalize_payload=_normalize_hk_dated_payload,
        fetch_batch=lambda order_book_ids, selected_fields, start_date, end_date: (
            _fetch_hk_shares_direct(
                order_book_ids,
                fields=list(selected_fields),
                start_date=start_date,
                end_date=end_date,
            )
            if _uses_hk_unique_ids(order_book_ids)
            else rqdatac.get_shares(
                order_book_ids,
                start_date=start_date,
                end_date=end_date,
                fields=list(selected_fields),
                market="hk",
            )
        ),
    )


def mirror_hk_exchange_rate(args, rqdatac) -> int:
    start_date = _normalize_absolute_date(args.start_date, label="--start-date")
    end_date = _normalize_absolute_date(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")

    fields, field_metadata = _resolve_default_plus_explicit_fields(
        args,
        default_fields=DEFAULT_HK_EXCHANGE_RATE_FIELDS,
        source_label="default_plus_explicit",
    )
    resume = bool(getattr(args, "resume", False))
    max_attempts = max(
        1,
        int(getattr(args, "max_attempts", DEFAULT_MIRROR_MAX_ATTEMPTS) or 1),
    )
    backoff_seconds = float(
        getattr(args, "backoff_seconds", DEFAULT_MIRROR_BACKOFF_SECONDS)
    )
    max_backoff_seconds = float(
        getattr(args, "max_backoff_seconds", DEFAULT_MIRROR_MAX_BACKOFF_SECONDS)
    )
    output_dir = _prepare_daily_output_dir(
        out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        dataset_name="exchange_rate",
        start_date=start_date,
        end_date=end_date,
        name=getattr(args, "name", None),
        resume=resume,
    )
    if resume:
        _validate_global_daily_resume_inputs(
            output_dir=output_dir,
            dataset_name="exchange_rate",
            fields=fields,
            start_date=start_date,
            end_date=end_date,
        )

    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    fields_path = output_dir / "fields.txt"
    data_path = data_dir / "exchange_rate.parquet"
    currency_pairs_path = output_dir / "currency_pairs.txt"
    dates_path = output_dir / "dates.txt"

    started_at = _timestamp_now()
    finished_at: str | None = None
    status = "completed"
    error: str | None = None
    result_code = 0
    total_attempts = 0
    fetch_chunks = _split_daily_range_by_year(start_date, end_date)
    frame = pd.DataFrame()

    try:
        chunk_frames: list[pd.DataFrame] = []
        for chunk_index, (chunk_start, chunk_end) in enumerate(fetch_chunks, start=1):
            print(
                f"Fetching exchange_rate chunk {chunk_index}/{len(fetch_chunks)}: "
                f"{chunk_start} -> {chunk_end}"
            )
            payload, attempts = _retry_fetch(
                f"exchange_rate fetch failed for {chunk_start}->{chunk_end}",
                lambda chunk_start=chunk_start, chunk_end=chunk_end: rqdatac.get_exchange_rate(
                    start_date=chunk_start,
                    end_date=chunk_end,
                    fields=list(fields),
                ),
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                max_backoff_seconds=max_backoff_seconds,
            )
            total_attempts += attempts
            if isinstance(payload, pd.Series):
                chunk_frame = payload.to_frame().reset_index()
            elif isinstance(payload, pd.DataFrame):
                chunk_frame = payload.reset_index()
            else:
                chunk_frame = pd.DataFrame(payload)
            chunk_frames.append(_normalize_frame_columns(chunk_frame))

        if chunk_frames:
            frame = pd.concat(chunk_frames, ignore_index=True)
        else:
            frame = pd.DataFrame()
        if "date" not in frame.columns and "index" in frame.columns:
            frame = frame.rename(columns={"index": "date"})
        if "date" not in frame.columns:
            raise SystemExit("exchange_rate payload is missing date.")
        if "currency_pair" not in frame.columns:
            raise SystemExit("exchange_rate payload is missing currency_pair.")

        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime(
            "%Y%m%d"
        )
        frame = frame[frame["date"].notna()].copy()
        frame["currency_pair"] = frame["currency_pair"].astype(str).str.strip()
        frame = frame[frame["currency_pair"] != ""].copy()
        frame.sort_values(["date", "currency_pair"], kind="mergesort", inplace=True)
        frame.reset_index(drop=True, inplace=True)

        _write_text_list(fields_path, fields)
        _write_text_list(
            currency_pairs_path,
            frame["currency_pair"].drop_duplicates().tolist(),
        )
        _write_text_list(dates_path, frame["date"].drop_duplicates().tolist())
        frame.to_parquet(data_path, index=False)
    except MirrorQuotaError as exc:
        status = "quota_exhausted"
        error = str(exc)
        result_code = 2
        finished_at = _timestamp_now()
    except Exception as exc:
        status = "failed"
        error = str(exc)
        result_code = 1
        finished_at = _timestamp_now()
    else:
        finished_at = _timestamp_now()
    finally:
        totals = {
            "rows": int(len(frame)),
            "dates": int(frame["date"].nunique()) if "date" in frame.columns else 0,
            "currency_pairs": int(frame["currency_pair"].nunique())
            if "currency_pair" in frame.columns
            else 0,
            "bytes": int(data_path.stat().st_size) if data_path.exists() else 0,
        }
        manifest = {
            "name": output_dir.name,
            "created_at": started_at,
            "dataset": "exchange_rate",
            "api": "rqdatac.get_exchange_rate",
            "market": "hk",
            "config_ref": getattr(args, "config", None),
            "output_dir": str(output_dir),
            "data_file": str(data_path),
            "fields_file": str(fields_path),
            "currency_pairs_file": str(currency_pairs_path),
            "dates_file": str(dates_path),
            "query": {
                "start_date": start_date,
                "end_date": end_date,
                "fields": list(fields),
            },
            "field_metadata": field_metadata,
            "columns": frame.columns.tolist(),
            "totals": totals,
            "currency_pairs": frame["currency_pair"].drop_duplicates().tolist()
            if "currency_pair" in frame.columns
            else [],
            "status": status,
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
            "attempts": total_attempts,
            "fetch_chunks": len(fetch_chunks),
            "git": _git_metadata(Path.cwd().resolve()),
        }
        _write_manifest(output_dir / "manifest.yml", manifest)

    print(
        f"Wrote exchange_rate mirror to {output_dir} "
        f"({len(frame)} rows, {int(frame['date'].nunique()) if 'date' in frame.columns else 0} dates, "
        f"{int(frame['currency_pair'].nunique()) if 'currency_pair' in frame.columns else 0} currency pairs, "
        f"status={status})"
    )
    return result_code


def mirror_hk_announcement(args, rqdatac) -> int:
    _ensure_rqdatac_hk_plugin()
    hk_api = getattr(rqdatac, "hk", None)
    if hk_api is None or not hasattr(hk_api, "get_announcement"):
        raise SystemExit(
            "rqdatac.hk.get_announcement is unavailable. Check rqdatac-hk installation."
        )

    fields, field_metadata = _resolve_optional_explicit_fields(args)
    return _mirror_dated_dataset(
        args=args,
        rqdatac=rqdatac,
        dataset_name="announcement",
        api_name="rqdatac.hk.get_announcement",
        date_column="info_date",
        fields=fields,
        field_metadata=field_metadata,
        sort_columns=(
            "rice_create_tm",
            "first_category",
            "second_category",
            "third_category",
            "title",
        ),
        resolve_request_groups=lambda symbols, start_date, end_date, args: _resolve_hk_dated_request_groups(
            symbols,
            start_date=start_date,
            end_date=end_date,
            out_root=getattr(args, "out_root", DEFAULT_OUT_ROOT),
        ),
        normalize_payload=_normalize_hk_dated_payload,
        fetch_batch=lambda order_book_ids, selected_fields, start_date, end_date: hk_api.get_announcement(
            order_book_ids=list(order_book_ids),
            start_date=start_date,
            end_date=end_date,
            fields=list(selected_fields) if selected_fields else None,
            market="hk",
        ),
    )
