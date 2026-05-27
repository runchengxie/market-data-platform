from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

_SEVERITY_RANK = {
    "info": 0,
    "warning": 1,
    "error": 2,
}
_FAIL_ON_SEVERITIES = ("none", "info", "warning", "error")


def _normalize_quality_severity(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in _SEVERITY_RANK:
        return text
    return None


def normalize_fail_on_severity(value: object) -> str:
    text = str(value or "none").strip().lower()
    if not text:
        return "none"
    if text not in _FAIL_ON_SEVERITIES:
        raise SystemExit("fail_on_severity must be one of: none, info, warning, error.")
    return text


def _coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _summarize_quality_state(
    *,
    issue_count: int,
    overall_severity: str,
    fail_on_severity: str,
    severity_counts: Mapping[str, object] | None = None,
) -> tuple[str, int, bool, str]:
    threshold_rank = _SEVERITY_RANK.get(fail_on_severity, 99)
    counts = severity_counts if isinstance(severity_counts, Mapping) else {}

    failing_issue_count = 0
    if fail_on_severity != "none":
        failing_issue_count = int(
            sum(
                _coerce_int(counts.get(severity, 0))
                for severity in _SEVERITY_RANK
                if _SEVERITY_RANK[severity] >= threshold_rank
            )
        )
    gate_triggered = bool(failing_issue_count > 0)

    if issue_count <= 0:
        color = "green"
        message = "No quality issues detected."
    elif overall_severity == "error":
        color = "red"
        message = f"{issue_count} quality issue(s) detected, including at least one error."
    else:
        color = "yellow"
        message = f"{issue_count} quality issue(s) detected; max_severity={overall_severity}."

    if fail_on_severity != "none":
        if gate_triggered:
            message = (
                f"{failing_issue_count} quality issue(s) met fail_on_severity={fail_on_severity}; "
                "the inspection gate was triggered."
            )
        else:
            message = (
                f"{issue_count} quality issue(s) detected; none met fail_on_severity={fail_on_severity}."
            )

    return color, failing_issue_count, gate_triggered, message


def format_quality_check_label(row: Mapping[str, object]) -> str:
    check = str(row.get("check") or "").strip() or "unknown_check"
    field = str(row.get("field") or "").strip()
    if field:
        return f"{check} [{field}]"
    return check


def summarize_quality_checks(
    quality_checks: Sequence[Mapping[str, object]] | None,
    *,
    fail_on_severity: object = "none",
) -> dict[str, object]:
    threshold = normalize_fail_on_severity(fail_on_severity)
    severity_counts: Counter[str] = Counter()
    failing_labels: list[str] = []
    max_rank = -1
    max_severity = "none"

    for row in quality_checks or []:
        if not isinstance(row, Mapping):
            continue
        severity = _normalize_quality_severity(row.get("severity")) or "info"
        severity_counts[severity] += 1
        severity_rank = _SEVERITY_RANK[severity]
        if severity_rank > max_rank:
            max_rank = severity_rank
            max_severity = severity
        if threshold != "none" and severity_rank >= _SEVERITY_RANK.get(threshold, 99):
            label = format_quality_check_label(row)
            if label not in failing_labels and len(failing_labels) < 5:
                failing_labels.append(label)

    issue_count = int(sum(severity_counts.values()))
    severity_counts_dict = {
        "error": int(severity_counts.get("error", 0)),
        "warning": int(severity_counts.get("warning", 0)),
        "info": int(severity_counts.get("info", 0)),
    }
    color, failing_issue_count, gate_triggered, message = _summarize_quality_state(
        issue_count=issue_count,
        overall_severity=max_severity if issue_count > 0 else "none",
        fail_on_severity=threshold,
        severity_counts=severity_counts_dict,
    )

    return {
        "color": color,
        "overall_severity": max_severity if issue_count > 0 else "none",
        "issue_count": issue_count,
        "severity_counts": severity_counts_dict,
        "fail_on_severity": threshold,
        "gate_triggered": gate_triggered,
        "gate_status": "fail" if gate_triggered else "pass",
        "failing_issue_count": failing_issue_count,
        "sample_failing_checks": failing_labels,
        "message": message,
    }


def rethreshold_quality_verdict(
    quality_verdict: Mapping[str, object] | None,
    *,
    fail_on_severity: object | None = None,
) -> dict[str, object] | None:
    if not isinstance(quality_verdict, Mapping):
        return None

    threshold = normalize_fail_on_severity(
        fail_on_severity if fail_on_severity is not None else quality_verdict.get("fail_on_severity", "none")
    )
    issue_count = _coerce_int(quality_verdict.get("issue_count"), default=0)
    severity_counts_raw = quality_verdict.get("severity_counts")
    severity_counts = {
        "error": _coerce_int(
            severity_counts_raw.get("error") if isinstance(severity_counts_raw, Mapping) else 0,
            default=0,
        ),
        "warning": _coerce_int(
            severity_counts_raw.get("warning") if isinstance(severity_counts_raw, Mapping) else 0,
            default=0,
        ),
        "info": _coerce_int(
            severity_counts_raw.get("info") if isinstance(severity_counts_raw, Mapping) else 0,
            default=0,
        ),
    }
    if issue_count <= 0:
        issue_count = int(sum(severity_counts.values()))
    overall_severity = _normalize_quality_severity(quality_verdict.get("overall_severity"))
    if overall_severity is None:
        if severity_counts["error"] > 0:
            overall_severity = "error"
        elif severity_counts["warning"] > 0:
            overall_severity = "warning"
        elif severity_counts["info"] > 0:
            overall_severity = "info"
        else:
            overall_severity = "none"

    color, failing_issue_count, gate_triggered, message = _summarize_quality_state(
        issue_count=issue_count,
        overall_severity=overall_severity if issue_count > 0 else "none",
        fail_on_severity=threshold,
        severity_counts=severity_counts,
    )

    updated = dict(quality_verdict)
    updated["color"] = color
    updated["overall_severity"] = overall_severity if issue_count > 0 else "none"
    updated["issue_count"] = issue_count
    updated["severity_counts"] = severity_counts
    updated["fail_on_severity"] = threshold
    updated["gate_triggered"] = gate_triggered
    updated["gate_status"] = "fail" if gate_triggered else "pass"
    updated["failing_issue_count"] = failing_issue_count
    updated["message"] = message
    return updated


def quality_gate_exit_code(quality_verdict: Mapping[str, object] | None) -> int:
    if isinstance(quality_verdict, Mapping) and bool(quality_verdict.get("gate_triggered")):
        return 2
    return 0


def append_quality_verdict_lines(
    lines: list[str],
    quality_verdict: Mapping[str, object] | None,
    *,
    heading: str = "Quality Verdict",
) -> None:
    if not isinstance(quality_verdict, Mapping):
        return
    lines.append("")
    lines.append(heading)
    for key in ("color", "overall_severity", "issue_count", "gate_status", "fail_on_severity"):
        lines.append(f"{key}: {quality_verdict.get(key)}")
    severity_counts = quality_verdict.get("severity_counts")
    if isinstance(severity_counts, Mapping):
        lines.append(
            "severity_counts: "
            f"error={int(severity_counts.get('error', 0))}, "
            f"warning={int(severity_counts.get('warning', 0))}, "
            f"info={int(severity_counts.get('info', 0))}"
        )
    lines.append(f"gate_triggered: {bool(quality_verdict.get('gate_triggered'))}")
    message = quality_verdict.get("message")
    if message:
        lines.append(f"message: {message}")
    sample_failing_checks = quality_verdict.get("sample_failing_checks")
    if isinstance(sample_failing_checks, list) and sample_failing_checks:
        lines.append("sample_failing_checks: " + ", ".join(str(item) for item in sample_failing_checks))
