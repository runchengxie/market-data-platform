from __future__ import annotations

import calendar
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from market_data_platform.paths import candidate_asset_paths, resolve_artifacts_root
from market_data_platform.providers.tushare_a_share import (
    TRADE_DATE_APIS,
    _normalize_ts_code,
    _pandas,
    _validate_date,
    _write_manifest,
    mirror_a_share_trade_date_dataset,
)

BACKFILL_DATASETS = ("daily", "adj_factor", "daily_basic", "limit_status")
BACKFILL_SEGMENTS = ("month", "year", "all")

_SNAPSHOT_NAMES = {
    "daily": "a_share_all_{start}_{end}_daily",
    "adj_factor": "a_share_all_{start}_{end}_adj_factor",
    "daily_basic": "a_share_all_{start}_{end}_daily_basic",
    "limit_status": "a_share_limit_status_{start}_{end}",
}


def _parse_date(value: str) -> date:
    text = _validate_date(value)
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _format_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _normalize_datasets(datasets: Iterable[str] | None) -> tuple[str, ...]:
    selected = tuple(str(dataset).strip() for dataset in datasets or BACKFILL_DATASETS)
    invalid = sorted({dataset for dataset in selected if dataset not in BACKFILL_DATASETS})
    if invalid:
        available = ", ".join(BACKFILL_DATASETS)
        raise ValueError(
            f"Unsupported TuShare backfill dataset(s): {invalid}. Available: {available}."
        )
    normalized: list[str] = []
    for dataset in selected:
        if dataset and dataset not in normalized:
            normalized.append(dataset)
    return tuple(normalized)


def _normalize_segment(value: str) -> str:
    segment = str(value or "month").strip().lower()
    if segment not in BACKFILL_SEGMENTS:
        available = ", ".join(BACKFILL_SEGMENTS)
        raise ValueError(
            f"Unsupported TuShare backfill segment: {segment}. Available: {available}."
        )
    return segment


def iter_backfill_segments(
    *,
    start_date: str,
    end_date: str,
    segment: str = "month",
) -> list[dict[str, str]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        raise ValueError(f"start_date must be <= end_date: {start_date} > {end_date}")
    segment = _normalize_segment(segment)
    current = start
    periods: list[dict[str, str]] = []
    while current <= end:
        if segment == "all":
            period_end = end
        elif segment == "year":
            period_end = min(end, date(current.year, 12, 31))
        else:
            last_day = calendar.monthrange(current.year, current.month)[1]
            period_end = min(end, date(current.year, current.month, last_day))
        periods.append({"start_date": _format_date(current), "end_date": _format_date(period_end)})
        current = period_end + timedelta(days=1)
    return periods


def _snapshot_name(dataset: str, *, start_date: str, end_date: str) -> str:
    return _SNAPSHOT_NAMES[dataset].format(start=start_date, end=end_date)


def _dataset_output_dir(root: Path, dataset: str, *, start_date: str, end_date: str) -> Path:
    aliases = candidate_asset_paths(root, market="a_share", provider="tushare")
    return aliases[dataset].parent / _snapshot_name(
        dataset,
        start_date=start_date,
        end_date=end_date,
    )


def build_a_share_backfill_plan(
    *,
    artifacts_root: str | Path | None = None,
    start_date: str,
    end_date: str,
    datasets: Iterable[str] | None = None,
    segment: str = "month",
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    start = _validate_date(start_date)
    end = _validate_date(end_date)
    selected_datasets = _normalize_datasets(datasets)
    segments = iter_backfill_segments(start_date=start, end_date=end, segment=segment)
    aliases = candidate_asset_paths(root, market="a_share", provider="tushare")
    return {
        "provider": "tushare",
        "market": "a_share",
        "status": "planned",
        "artifacts_root": str(root),
        "query": {
            "start_date": start,
            "end_date": end,
            "segment": _normalize_segment(segment),
            "partition_by": "trade_date",
        },
        "datasets": [
            {
                "dataset": dataset,
                "api": TRADE_DATE_APIS[dataset],
                "output_dir": str(
                    _dataset_output_dir(root, dataset, start_date=start, end_date=end)
                ),
                "latest_alias": str(aliases[dataset]),
                "segments": segments,
            }
            for dataset in selected_datasets
        ],
        "totals": {
            "datasets": len(selected_datasets),
            "segments_per_dataset": len(segments),
            "dataset_segments": len(selected_datasets) * len(segments),
        },
    }


def _trade_date_from_part(path: Path) -> str | None:
    parent = path.parent.name
    if not parent.startswith("trade_date="):
        return None
    value = parent.split("=", 1)[1]
    return value if value.isdigit() and len(value) == 8 else None


def _summarize_trade_date_output(output_dir: str | Path) -> dict[str, Any]:
    pd = _pandas()
    root = Path(output_dir).expanduser().resolve()
    files = sorted((root / "data").glob("trade_date=*/part.parquet"))
    rows = 0
    symbols: set[str] = set()
    trade_dates: set[str] = set()
    for path in files:
        trade_date = _trade_date_from_part(path)
        if trade_date:
            trade_dates.add(trade_date)
        try:
            frame = pd.read_parquet(path, columns=["symbol"])
        except Exception:
            frame = pd.read_parquet(path, columns=["ts_code"])
            frame["symbol"] = frame["ts_code"].map(_normalize_ts_code)
        rows += int(len(frame))
        symbols.update(frame["symbol"].dropna().astype(str).tolist())
    return {
        "rows": rows,
        "symbols": len(symbols),
        "files": len(files),
        "trade_dates_present": sorted(trade_dates),
    }


def _segment_totals(segment_results: Sequence[dict[str, Any]], key: str) -> int:
    total = 0
    for result in segment_results:
        totals = result.get("totals")
        if isinstance(totals, dict):
            total += int(totals.get(key) or 0)
    return total


def _write_dataset_backfill_manifest(
    *,
    dataset: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    segment: str,
    segment_results: Sequence[dict[str, Any]],
    failures: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    audit = _summarize_trade_date_output(output_dir)
    status = "completed" if not failures else "failed"
    manifest = {
        "schema_version": f"tushare.{TRADE_DATE_APIS[dataset]}.v1",
        "dataset": dataset,
        "market": "a_share",
        "provider": "tushare",
        "status": status,
        "output_dir": str(output_dir),
        "query": {
            "api": TRADE_DATE_APIS[dataset],
            "start_date": start_date,
            "end_date": end_date,
            "segment": segment,
            "partition_by": "trade_date",
        },
        "totals": {
            "rows": audit["rows"],
            "symbols": audit["symbols"],
            "files": audit["files"],
            "trade_dates_present": len(audit["trade_dates_present"]),
            "trade_dates_requested": _segment_totals(
                segment_results, "trade_dates_requested"
            ),
            "trade_dates_written_this_run": _segment_totals(
                segment_results, "trade_dates_written"
            ),
            "trade_dates_skipped_this_run": _segment_totals(
                segment_results, "trade_dates_skipped"
            ),
            "trade_dates_empty_this_run": _segment_totals(segment_results, "trade_dates_empty"),
            "segments_completed": len(segment_results),
            "segments_failed": len(failures),
        },
        "trade_dates_present": audit["trade_dates_present"],
        "segments": list(segment_results),
        "failures": list(failures),
    }
    _write_manifest(output_dir / "manifest.yml", manifest)
    return manifest


def _sync_latest_alias(*, alias_path: str | Path, target_dir: str | Path) -> dict[str, str]:
    alias = Path(alias_path).expanduser()
    if not alias.is_absolute():
        alias = alias.absolute()
    target = Path(target_dir).expanduser().resolve()
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.exists() or alias.is_symlink():
        if not alias.is_symlink():
            raise RuntimeError(f"Refusing to replace non-symlink latest alias: {alias}")
        alias.unlink()
    alias.symlink_to(target.name)
    return {"alias_path": str(alias), "target": str(target)}


def _run_dataset_backfill(
    *,
    dataset_plan: dict[str, Any],
    start_date: str,
    end_date: str,
    segment: str,
    skip_existing: bool,
    continue_on_error: bool,
    sync_latest: bool,
    token_env: str,
    client: Any | None,
) -> dict[str, Any]:
    dataset = str(dataset_plan["dataset"])
    output_dir = Path(str(dataset_plan["output_dir"])).expanduser().resolve()
    segment_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for period in dataset_plan["segments"]:
        period_start = str(period["start_date"])
        period_end = str(period["end_date"])
        try:
            manifest = mirror_a_share_trade_date_dataset(
                dataset=dataset,
                out_dir=output_dir,
                start_date=period_start,
                end_date=period_end,
                skip_existing=skip_existing,
                token_env=token_env,
                client=client,
            )
        except Exception as exc:
            failure = {
                "dataset": dataset,
                "start_date": period_start,
                "end_date": period_end,
                "error": str(exc) or exc.__class__.__name__,
            }
            failures.append(failure)
            if not continue_on_error:
                break
            continue
        segment_results.append(
            {
                "start_date": period_start,
                "end_date": period_end,
                "status": manifest.get("status"),
                "totals": manifest.get("totals", {}),
                "written_trade_dates": manifest.get("written_trade_dates", []),
                "skipped_trade_dates": manifest.get("skipped_trade_dates", []),
                "empty_trade_dates": manifest.get("empty_trade_dates", []),
            }
        )

    final_manifest = _write_dataset_backfill_manifest(
        dataset=dataset,
        output_dir=output_dir,
        start_date=start_date,
        end_date=end_date,
        segment=segment,
        segment_results=segment_results,
        failures=failures,
    )
    result: dict[str, Any] = {
        "dataset": dataset,
        "status": final_manifest["status"],
        "output_dir": str(output_dir),
        "manifest_path": str(output_dir / "manifest.yml"),
        "totals": final_manifest["totals"],
        "failures": failures,
    }
    if sync_latest and not failures:
        result["latest_alias"] = _sync_latest_alias(
            alias_path=dataset_plan["latest_alias"], target_dir=output_dir
        )
    return result


def run_a_share_history_backfill(
    *,
    artifacts_root: str | Path | None = None,
    start_date: str,
    end_date: str,
    datasets: Iterable[str] | None = None,
    segment: str = "month",
    skip_existing: bool = True,
    sync_latest: bool = False,
    dry_run: bool = False,
    continue_on_error: bool = False,
    token_env: str = "TUSHARE_TOKEN",
    client: Any | None = None,
) -> dict[str, Any]:
    plan = build_a_share_backfill_plan(
        artifacts_root=artifacts_root,
        start_date=start_date,
        end_date=end_date,
        datasets=datasets,
        segment=segment,
    )
    if dry_run:
        return {**plan, "dry_run": True}

    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for dataset_plan in plan["datasets"]:
        result = _run_dataset_backfill(
            dataset_plan=dataset_plan,
            start_date=plan["query"]["start_date"],
            end_date=plan["query"]["end_date"],
            segment=plan["query"]["segment"],
            skip_existing=skip_existing,
            continue_on_error=continue_on_error,
            sync_latest=sync_latest,
            token_env=token_env,
            client=client,
        )
        completed.append(result)
        failures.extend(result.get("failures", []))
        if failures and not continue_on_error:
            break

    status = "completed" if not failures else "failed"
    return {
        "provider": "tushare",
        "market": "a_share",
        "status": status,
        "dry_run": False,
        "artifacts_root": plan["artifacts_root"],
        "query": plan["query"],
        "datasets": completed,
        "failures": failures,
        "totals": {
            "datasets_planned": len(plan["datasets"]),
            "datasets_attempted": len(completed),
            "datasets_completed": sum(1 for item in completed if item["status"] == "completed"),
            "datasets_failed": sum(1 for item in completed if item["status"] != "completed"),
            "failures": len(failures),
        },
    }
