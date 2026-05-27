from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .quality_gate import append_quality_verdict_lines


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _append_key_values(
    lines: list[str],
    payload: Mapping[str, object],
    keys: list[str],
) -> None:
    for key in keys:
        if key in payload:
            lines.append(f"{key}: {payload.get(key)}")


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


def _append_source(lines: list[str], source: Mapping[str, object]) -> None:
    if not source:
        return
    for key in ("config", "fundamentals_file", "asset_dir"):
        value = source.get(key)
        if value:
            lines.append(f"{key}: {value}")


def _append_selection(lines: list[str], selection: Mapping[str, object]) -> None:
    lines.append("")
    lines.append("Selection")
    lines.append(f"mode: {selection.get('mode')}")
    lines.append(f"feature_source: {selection.get('source')}")
    lines.append(f"selected_features: {selection.get('count')}")
    ignored_features = selection.get("ignored_features")
    if ignored_features:
        lines.append("ignored_features: " + ", ".join(str(item) for item in ignored_features))


def _append_summary(
    lines: list[str],
    summary: Mapping[str, object],
    manifest_totals: Mapping[str, object],
) -> None:
    lines.append("")
    lines.append("Summary")
    _append_key_values(
        lines,
        summary,
        [
            "rows",
            "symbols",
            "dates",
            "quarters",
            "min_trade_date",
            "max_trade_date",
            "median_symbols_per_date",
            "max_symbols_per_date",
        ],
    )
    if manifest_totals:
        lines.append("")
        lines.append("Pipeline Manifest")
        _append_key_values(
            lines,
            manifest_totals,
            [
                "input_rows",
                "output_rows",
                "symbols",
                "dropped_all_missing_fields",
                "duplicate_rows_seen",
                "duplicate_rows_dropped",
            ],
        )


def _append_complete_case(lines: list[str], complete_case: Mapping[str, object]) -> None:
    lines.append("")
    lines.append("Complete Case")
    _append_key_values(
        lines,
        complete_case,
        [
            "complete_rows",
            "complete_row_pct",
            "complete_symbols",
            "complete_quarters",
            "quarter_complete_symbols_median",
            "quarter_complete_symbols_max",
            "quarter_count_meeting_min_symbols",
        ],
    )


def _append_feature_tables(
    lines: list[str],
    payload: Mapping[str, object],
    *,
    top: int,
    quarter_limit: int,
) -> None:
    field_rows = _as_list(payload.get("field_coverage"))
    _append_dataframe_section(
        lines,
        title=f"Worst Features (top {min(top, len(field_rows))})",
        rows=field_rows[:top],
        columns=[
            "feature",
            "row_coverage_pct",
            "symbol_coverage_pct",
            "quarter_coverage_pct",
            "complete_case_row_lift_if_dropped",
        ],
    )

    quarter_rows = _as_list(payload.get("quarter_coverage"))
    _append_dataframe_section(
        lines,
        title=f"Recent Quarters (last {min(quarter_limit, len(quarter_rows))})",
        rows=quarter_rows[-quarter_limit:],
        columns=[
            "quarter",
            "symbols_in_file",
            "symbols_with_any_selected_feature",
            "symbols_with_all_selected_features",
        ],
    )


def _append_trainable_estimate(
    lines: list[str],
    trainable_estimate: Mapping[str, object],
) -> None:
    if not trainable_estimate:
        return
    lines.append("")
    lines.append("Trainable Estimate")
    _append_key_values(
        lines,
        trainable_estimate,
        [
            "feature_source",
            "pit_features_considered",
            "rebalance_frequency",
            "sample_on_rebalance_dates",
            "grid_source",
            "fundamentals_ffill",
            "fundamentals_ffill_limit",
            "missing_method",
            "missing_features_considered",
            "indicator_features_added",
            "active_rows",
            "active_symbols",
            "periods",
            "rows_with_all_selected_features_after_ffill",
            "rows_with_all_selected_features_after_missing_fill",
            "period_symbols_median_after_ffill",
            "period_symbols_max_after_ffill",
            "period_count_meeting_min_symbols_after_ffill",
            "period_symbols_median_after_missing_fill",
            "period_symbols_max_after_missing_fill",
            "period_count_meeting_min_symbols_after_missing_fill",
        ],
    )
    non_pit_features = trainable_estimate.get("non_pit_features_ignored")
    if non_pit_features:
        lines.append(
            "non_pit_features_ignored: " + ", ".join(str(item) for item in non_pit_features)
        )


def _append_fill_dependence(lines: list[str], fill_dependence: Mapping[str, object]) -> None:
    if not fill_dependence:
        return
    lines.append("")
    lines.append("Fill Dependence")
    _append_key_values(
        lines,
        fill_dependence,
        [
            "route_type",
            "status",
            "periods_after_ffill",
            "periods_after_missing_fill",
            "recovered_periods_from_missing_fill",
            "retention_ratio_after_ffill",
            "fill_dependency_ratio_from_missing_fill",
            "green_threshold",
            "yellow_threshold",
            "message",
            "next_step",
        ],
    )


def _append_trainable_periods(
    lines: list[str],
    payload: Mapping[str, object],
    *,
    quarter_limit: int,
) -> None:
    trainable_rows = _as_list(payload.get("trainable_period_coverage"))
    _append_dataframe_section(
        lines,
        title=f"Estimated Trainable Periods (last {min(quarter_limit, len(trainable_rows))})",
        rows=trainable_rows[-quarter_limit:],
        columns=[
            "period",
            "active_symbols",
            "symbols_with_any_selected_features_after_ffill",
            "symbols_with_all_selected_features_after_ffill",
            "symbols_with_all_selected_features_after_missing_fill",
        ],
    )


def _append_health_summary(
    lines: list[str],
    health_source: Mapping[str, object],
    health_summary: Mapping[str, object],
) -> None:
    lines.append("")
    lines.append("Health")
    _append_key_values(
        lines,
        health_source,
        ["target_date", "target_date_source", "symbol_filter_source"],
    )
    _append_key_values(
        lines,
        health_summary,
        [
            "symbols_scanned",
            "symbols_available_in_fundamentals",
            "symbols_missing_in_fundamentals",
            "symbols_with_any_row_before_target_date",
            "symbols_without_any_row_before_target_date",
            "symbols_with_any_selected_features_asof_target_date",
            "symbols_with_all_selected_features_asof_target_date",
            "all_selected_features_coverage_pct",
            "latest_report_age_days_max",
            "latest_report_age_gt_90d_symbols",
            "latest_report_age_gt_180d_symbols",
            "complete_symbol_oldest_feature_age_days_max",
            "complete_symbol_oldest_feature_age_gt_90d_symbols",
            "complete_symbol_oldest_feature_age_gt_180d_symbols",
            "rows_last_30d",
            "symbols_updated_last_30d",
            "rows_last_90d",
            "symbols_updated_last_90d",
            "rows_last_180d",
            "symbols_updated_last_180d",
        ],
    )


def _append_optional_symbol_list(lines: list[str], label: str, values: object) -> None:
    if values:
        lines.append(f"{label}: " + ", ".join(str(item) for item in values))


def _append_health_tables(
    lines: list[str],
    health: Mapping[str, object],
    *,
    top: int,
) -> None:
    feature_health = _as_list(health.get("feature_health"))
    _append_dataframe_section(
        lines,
        title=f"Health Features (top {min(top, len(feature_health))})",
        rows=feature_health[:top],
        columns=[
            "feature",
            "coverage_pct",
            "missing_symbols_asof_target_date",
            "age_days_p90",
            "age_days_max",
            "age_gt_180d_symbols",
        ],
    )
    recent_disclosures = _as_list(health.get("recent_disclosures"))
    _append_dataframe_section(
        lines,
        title=f"Recent Disclosures (last {len(recent_disclosures)})",
        rows=recent_disclosures,
    )
    _append_dataframe_section(
        lines,
        title="Health Checks",
        rows=_as_list(health.get("quality_checks")),
        columns=["check", "field", "severity", "affected_symbols", "affected_pct"],
    )


def _append_health(
    lines: list[str],
    health: Mapping[str, object],
    *,
    top: int,
) -> None:
    if not health:
        return
    _append_health_summary(
        lines,
        _as_mapping(health.get("source")),
        _as_mapping(health.get("summary")),
    )
    _append_optional_symbol_list(
        lines,
        "sample_symbols_without_rows",
        health.get("sample_symbols_without_rows"),
    )
    _append_optional_symbol_list(
        lines,
        "sample_missing_asset_symbols",
        health.get("sample_missing_asset_symbols"),
    )
    health_verdict = _as_mapping(health.get("quality_verdict")) or None
    append_quality_verdict_lines(lines, health_verdict, heading="Health Verdict")
    _append_health_tables(lines, health, top=top)


def render_hk_pit_coverage_text(
    payload: Mapping[str, object],
    *,
    top: int,
    quarter_limit: int,
) -> str:
    lines = ["HK PIT Coverage"]

    _append_source(lines, _as_mapping(payload.get("source")))
    _append_selection(lines, _as_mapping(payload.get("selection")))
    _append_summary(
        lines,
        _as_mapping(payload.get("summary")),
        _as_mapping(payload.get("pipeline_manifest_totals")),
    )
    _append_complete_case(lines, _as_mapping(payload.get("complete_case")))
    _append_feature_tables(lines, payload, top=top, quarter_limit=quarter_limit)
    _append_trainable_estimate(lines, _as_mapping(payload.get("trainable_estimate")))
    _append_fill_dependence(lines, _as_mapping(payload.get("fill_dependence_assessment")))
    _append_trainable_periods(lines, payload, quarter_limit=quarter_limit)
    _append_health(lines, _as_mapping(payload.get("health")), top=top)

    return "\n".join(lines).strip() + "\n"
