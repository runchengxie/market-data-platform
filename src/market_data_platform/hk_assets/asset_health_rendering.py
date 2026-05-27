from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .quality_gate import append_quality_verdict_lines


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _append_header(lines: list[str], summary: Mapping[str, object]) -> None:
    for key in (
        "asset_dir",
        "dataset",
        "target_date",
        "target_date_source",
        "date_column",
        "selection_source",
        "daily_reference_asset_dir",
        "symbol_filter_source",
    ):
        value = summary.get(key)
        if value:
            lines.append(f"{key}: {value}")

    selected_fields = summary.get("selected_fields")
    if isinstance(selected_fields, list) and selected_fields:
        lines.append(f"selected_fields: {', '.join(str(item) for item in selected_fields)}")
    manifest_query_date = summary.get("manifest_query_date")
    if manifest_query_date:
        lines.append(f"manifest_query_date: {manifest_query_date}")


def _append_summary(lines: list[str], summary: Mapping[str, object]) -> None:
    lines.append("")
    lines.append("Summary")
    for key in (
        "symbols_scanned",
        "symbols_available_in_asset_dir",
        "symbols_missing_asset_file",
        "symbols_with_target_date_row",
        "symbols_without_target_date_row",
        "target_date_coverage_pct",
        "symbols_with_duplicate_dates",
        "duplicate_date_groups",
        "duplicate_date_rows",
        "latest_date_min",
        "latest_date_max",
    ):
        lines.append(f"{key}: {summary.get(key)}")

    status_counts = summary.get("audit_status_counts")
    if isinstance(status_counts, Mapping) and status_counts:
        lines.append("")
        lines.append("Audit Status")
        for status, count in sorted(status_counts.items()):
            lines.append(f"{status}: {count}")


def _append_missing_files(lines: list[str], rows: list[object]) -> None:
    if not rows:
        return
    lines.append("")
    lines.append("Sample Missing Asset Files")
    for row in rows:
        if not isinstance(row, Mapping):
            lines.append(str(row))
            continue
        symbol = row.get("symbol")
        suffix_parts = [str(item) for item in (row.get("status"), row.get("error")) if item]
        if suffix_parts:
            lines.append(f"{symbol} ({'; '.join(suffix_parts)})")
        else:
            lines.append(str(symbol))


def _append_dataframe_section(
    lines: list[str],
    *,
    title: str,
    rows: list[object],
    columns: list[str] | None = None,
) -> None:
    if not rows:
        return
    lines.append("")
    lines.append(title)
    frame = pd.DataFrame(rows)
    if columns is not None:
        frame = frame[columns]
    lines.append(frame.to_string(index=False))


def _append_audit_issues(lines: list[str], rows: list[object]) -> None:
    if not rows:
        return
    lines.append("")
    lines.append("Audit Issues")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "{status}: {issue_category} -> {affected_symbols} symbol(s)".format(
                status=row.get("status"),
                issue_category=row.get("issue_category"),
                affected_symbols=row.get("affected_symbols"),
            )
        )
        if row.get("error"):
            lines.append(f"  error: {row.get('error')}")
        sample_symbols = row.get("sample_symbols")
        if isinstance(sample_symbols, list) and sample_symbols:
            lines.append(f"  samples: {', '.join(str(item) for item in sample_symbols)}")


def _append_stale_symbols(lines: list[str], rows: list[object]) -> None:
    if not rows:
        return
    lines.append("")
    lines.append("Sample Stale Symbols")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = row.get("symbol")
        latest_date = row.get("latest_date")
        status = row.get("status")
        if status:
            lines.append(f"{symbol} @ {latest_date} ({status})")
        else:
            lines.append(f"{symbol} @ {latest_date}")


def _append_field_coverage(lines: list[str], rows: list[object]) -> None:
    _append_dataframe_section(
        lines,
        title="Field Coverage",
        rows=rows,
        columns=[
            "field",
            "clean_nonmissing_on_target_date",
            "missing_on_target_date",
            "placeholder_on_target_date",
            "nonfinite_on_target_date",
            "zero_on_target_date",
            "unique_clean_values_on_target_date",
            "ffill_age_days_p90",
            "ffill_age_days_max",
        ],
    )


def _append_quality_checks(lines: list[str], rows: list[object]) -> None:
    if not rows:
        return
    lines.append("")
    lines.append("Quality Checks")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("check") or "")
        field = row.get("field")
        if field:
            label = f"{label} [{field}]"
        lines.append(
            f"{row.get('severity')}: {label} -> "
            f"{row.get('affected_symbols')} symbol(s), {row.get('affected_pct')}%"
        )
        sample_symbols = row.get("sample_symbols")
        if isinstance(sample_symbols, list) and sample_symbols:
            lines.append(f"  samples: {', '.join(str(item) for item in sample_symbols)}")


def _append_history(lines: list[str], summary: Mapping[str, object], issues: list[object]) -> None:
    if not summary:
        return
    lines.append("")
    lines.append("History")
    for key in ("symbols_scanned", "rows_scanned", "date_min", "date_max", "issue_count"):
        lines.append(f"{key}: {summary.get(key)}")
    for row in issues:
        if not isinstance(row, Mapping):
            continue
        label = f"{row.get('check')} [{row.get('field')}]" if row.get("field") else row.get("check")
        lines.append(
            (
                "{severity}: {label} -> "
                "{affected_rows} row(s), {affected_symbols} symbol(s)"
            ).format(
                severity=row.get("severity"),
                label=label,
                affected_rows=row.get("affected_rows"),
                affected_symbols=row.get("affected_symbols"),
            )
        )
        sample_rows = row.get("sample_rows")
        if isinstance(sample_rows, list) and sample_rows:
            sample_df = pd.DataFrame(sample_rows)
            lines.append(sample_df.to_string(index=False))


def render_asset_health_text(payload: Mapping[str, object]) -> str:
    summary = _as_mapping(payload.get("summary"))
    history_payload = _as_mapping(payload.get("history"))
    missing_file_rows = _as_list(
        payload.get("sample_missing_asset_file_details")
        if isinstance(payload.get("sample_missing_asset_file_details"), list)
        else payload.get("sample_missing_asset_file_symbols")
    )
    lines = ["HK Asset Health"]

    _append_header(lines, summary)
    _append_summary(lines, summary)
    _append_missing_files(lines, missing_file_rows)
    _append_dataframe_section(
        lines,
        title=f"Latest Dates (top {len(_as_list(payload.get('latest_date_distribution')))})",
        rows=_as_list(payload.get("latest_date_distribution")),
    )
    append_quality_verdict_lines(lines, _as_mapping(payload.get("quality_verdict")) or None)
    _append_audit_issues(lines, _as_list(payload.get("audit_issue_groups")))
    _append_stale_symbols(lines, _as_list(payload.get("sample_stale_symbols")))
    _append_field_coverage(lines, _as_list(payload.get("field_coverage")))
    _append_quality_checks(lines, _as_list(payload.get("quality_checks")))
    _append_history(
        lines,
        _as_mapping(history_payload.get("summary")),
        _as_list(history_payload.get("issues")),
    )

    return "\n".join(lines).strip() + "\n"
