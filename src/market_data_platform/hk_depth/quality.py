"""Shared data-quality helpers for health and reconciliation workflows."""

from __future__ import annotations

from datetime import time
from typing import Any, cast

import pandas as pd

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
FAIL_ON_SEVERITIES = ("none", "info", "warning", "error")
SEVERITIES = ("error", "warning", "info")
SESSION_PHASES = (
    "pre_open",
    "morning_continuous",
    "lunch_break",
    "afternoon_continuous",
    "closing_auction",
    "post_close",
    "outside_session",
)


def numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return cast(pd.Series, pd.to_numeric(frame[column], errors="coerce"))


def normalize_fail_on_severity(value: object) -> str:
    text = str(value or "error").strip().lower()
    if not text:
        return "error"
    if text not in FAIL_ON_SEVERITIES:
        raise ValueError("fail_on_severity must be one of: none, info, warning, error.")
    return text


def sample_frame(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    sample = frame.head(limit)
    return sample.where(pd.notna(sample), None).to_dict("records")


def append_quality_check(
    checks: list[dict[str, Any]],
    *,
    check: str,
    severity: str,
    message: str,
    affected: int | None = None,
    samples: pd.DataFrame | None = None,
    sample_limit: int = 20,
    include_zero: bool = False,
    **extra: Any,
) -> None:
    if affected is not None and affected <= 0 and not include_zero:
        return
    row = {"check": check, "severity": severity, "message": message, **extra}
    if affected is not None:
        row["affected_items"] = int(affected)
    if samples is not None:
        row["sample_rows"] = sample_frame(samples, sample_limit)
    checks.append(row)


def quality_verdict(
    checks: list[dict[str, Any]],
    *,
    fail_on_severity: str,
    include_sample_failing_checks: bool = False,
) -> dict[str, Any]:
    threshold = normalize_fail_on_severity(fail_on_severity)
    severity_counts = {
        severity: sum(1 for check in checks if check.get("severity") == severity)
        for severity in SEVERITIES
    }
    overall = "none"
    for severity in SEVERITIES:
        if severity_counts[severity]:
            overall = severity
            break
    failing = 0
    if threshold != "none":
        threshold_rank = SEVERITY_RANK[threshold]
        failing = sum(
            1
            for check in checks
            if SEVERITY_RANK.get(str(check.get("severity")), 0) >= threshold_rank
        )
    verdict = {
        "overall_severity": overall,
        "issue_count": int(sum(severity_counts.values())),
        "severity_counts": severity_counts,
        "fail_on_severity": threshold,
        "gate_triggered": bool(failing),
        "gate_status": "fail" if failing else "pass",
        "failing_issue_count": int(failing),
    }
    if include_sample_failing_checks:
        verdict["sample_failing_checks"] = [
            str(check.get("check"))
            for check in checks
            if threshold != "none"
            and SEVERITY_RANK.get(str(check.get("severity")), 0) >= SEVERITY_RANK[threshold]
        ][:5]
    return verdict


def quote_ladder_flags(frame: pd.DataFrame) -> dict[str, pd.Series]:
    crossed = pd.Series(False, index=frame.index)
    zero_spread = pd.Series(False, index=frame.index)
    ask_inversion = pd.Series(False, index=frame.index)
    bid_inversion = pd.Series(False, index=frame.index)
    negative_depth_volume = pd.Series(False, index=frame.index)

    if {"a1", "b1"}.issubset(frame.columns):
        a1 = numeric(frame, "a1")
        b1 = numeric(frame, "b1")
        positive = (a1 > 0) & (b1 > 0)
        crossed = positive & (a1 < b1)
        zero_spread = positive & (a1 == b1)

    for level in range(1, 10):
        ask_left, ask_right = f"a{level}", f"a{level + 1}"
        bid_left, bid_right = f"b{level}", f"b{level + 1}"
        if {ask_left, ask_right}.issubset(frame.columns):
            left = numeric(frame, ask_left)
            right = numeric(frame, ask_right)
            valid = (left > 0) & (right > 0)
            ask_inversion |= valid & (left > right)
        if {bid_left, bid_right}.issubset(frame.columns):
            left = numeric(frame, bid_left)
            right = numeric(frame, bid_right)
            valid = (left > 0) & (right > 0)
            bid_inversion |= valid & (left < right)

    volume_columns = [
        column
        for column in frame.columns
        if (column.startswith("a") or column.startswith("b")) and column.endswith("_v")
    ]
    for column in volume_columns:
        negative_depth_volume |= numeric(frame, column) < 0

    invalid = crossed | ask_inversion | bid_inversion | negative_depth_volume
    return {
        "crossed_best_spread": crossed,
        "zero_best_spread": zero_spread,
        "ask_ladder_inversion": ask_inversion,
        "bid_ladder_inversion": bid_inversion,
        "negative_depth_volume": negative_depth_volume,
        "quote_ladder_invalid": invalid,
    }


def quote_ladder_invalid(frame: pd.DataFrame) -> pd.Series:
    return quote_ladder_flags(frame)["quote_ladder_invalid"]


def classify_hk_session_phase(timestamps: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(timestamps, errors="coerce")
    phases = pd.Series("outside_session", index=timestamps.index, dtype="object")
    valid = parsed.notna()
    if not valid.any():
        return phases

    times = parsed.dt.time
    phases.loc[valid & (times >= time(9, 0)) & (times < time(9, 30))] = "pre_open"
    phases.loc[valid & (times >= time(9, 30)) & (times < time(12, 0))] = "morning_continuous"
    phases.loc[valid & (times >= time(12, 0)) & (times < time(13, 0))] = "lunch_break"
    phases.loc[valid & (times >= time(13, 0)) & (times < time(16, 0))] = "afternoon_continuous"
    phases.loc[valid & (times >= time(16, 0)) & (times <= time(16, 10))] = "closing_auction"
    phases.loc[valid & (times > time(16, 10)) & (times <= time(16, 30))] = "post_close"
    return phases


def session_phase_counts(timestamps: pd.Series) -> dict[str, int]:
    phases = classify_hk_session_phase(timestamps)
    counts = phases.value_counts(dropna=False).to_dict()
    return {phase: int(counts.get(phase, 0)) for phase in SESSION_PHASES}
