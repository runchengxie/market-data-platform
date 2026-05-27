"""RQData quota formatting helpers."""

from __future__ import annotations

from typing import Any, cast


def quota_to_payload(value: Any) -> Any:
    """Convert provider quota objects into JSON-friendly Python values."""
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    if isinstance(value, dict):
        return {str(key): quota_to_payload(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [quota_to_payload(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def coerce_float(value: object) -> float | None:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def format_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def render_pct_bar(pct: float, width: int = 20) -> str:
    if pct <= 0:
        filled = 0
    elif pct >= 100:
        filled = width
    else:
        filled = int(round(width * pct / 100))
    return f"[{'#' * filled}{'-' * (width - filled)}] {pct:.2f}%"


def augment_quota_entry(entry: dict[str, Any]) -> dict[str, Any]:
    bytes_used = coerce_float(entry.get("bytes_used"))
    bytes_limit = coerce_float(entry.get("bytes_limit"))
    if bytes_used is None or bytes_limit is None or bytes_limit <= 0:
        return entry
    used_pct = min(bytes_used / bytes_limit * 100.0, 100.0)
    entry["bytes_remaining"] = max(bytes_limit - bytes_used, 0.0)
    entry["used_pct"] = round(used_pct, 2)
    entry["remaining_pct"] = round(max(0.0, 100.0 - used_pct), 2)
    return entry


def augment_quota_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return augment_quota_entry(dict(payload))
    if isinstance(payload, list):
        return [
            augment_quota_entry(dict(entry)) if isinstance(entry, dict) else entry
            for entry in payload
        ]
    return payload


def format_quota_entry(entry: dict[str, Any], label: str | None = None) -> str:
    lines: list[str] = []
    if label:
        lines.append(label)
    for key in ("license_type", "remaining_days"):
        if key in entry:
            lines.append(f"{key}: {entry.get(key)}")

    for key in ("bytes_used", "bytes_limit", "bytes_remaining"):
        raw_value = entry.get(key)
        value = coerce_float(raw_value)
        if value is None:
            if raw_value is not None:
                lines.append(f"{key}: {raw_value}")
            continue
        lines.append(f"{key}: {format_bytes(value)} ({int(value)} B)")

    used_pct = coerce_float(entry.get("used_pct"))
    remaining_pct = coerce_float(entry.get("remaining_pct"))
    if used_pct is not None:
        lines.append(f"used_pct: {used_pct:.2f}%")
        lines.append(f"usage: {render_pct_bar(used_pct)} used")
    if remaining_pct is not None:
        lines.append(f"remaining_pct: {remaining_pct:.2f}%")
    return "\n".join(lines)


def format_quota_pretty(payload: Any) -> str:
    if isinstance(payload, dict):
        return format_quota_entry(payload, label="Quota usage")
    if isinstance(payload, list):
        blocks: list[str] = []
        for index, entry in enumerate(payload, start=1):
            if isinstance(entry, dict):
                blocks.append(format_quota_entry(entry, label=f"Quota usage #{index}"))
            else:
                blocks.append(f"Quota usage #{index}\n{entry}")
        return "\n\n".join(blocks)
    return str(payload)
