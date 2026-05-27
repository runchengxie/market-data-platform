from __future__ import annotations

import logging

from dotenv import load_dotenv

from .config_utils import resolve_pipeline_config
from .rqdata_runtime import (
    init_rqdatac as _init_rqdatac_runtime,
    patch_rqdatac_adjust_price_readonly as _patch_rqdatac_adjust_price_readonly,
)


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024


def render_pct_bar(pct: float, width: int = 20) -> str:
    if pct <= 0:
        filled = 0
    elif pct >= 100:
        filled = width
    else:
        filled = int(round(width * pct / 100))
    return f"[{'#' * filled}{'-' * (width - filled)}] {pct:.2f}%"


def coerce_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def augment_quota_entry(entry: dict) -> dict:
    bytes_used = coerce_float(entry.get("bytes_used"))
    bytes_limit = coerce_float(entry.get("bytes_limit"))
    if bytes_used is None or bytes_limit is None:
        return entry
    if bytes_limit <= 0:
        return entry
    used_pct = min(bytes_used / bytes_limit * 100.0, 100.0)
    remaining_pct = max(0.0, 100.0 - used_pct)
    bytes_remaining = max(bytes_limit - bytes_used, 0.0)
    entry["bytes_remaining"] = bytes_remaining
    entry["used_pct"] = round(used_pct, 2)
    entry["remaining_pct"] = round(remaining_pct, 2)
    return entry


def augment_quota_payload(payload):
    if isinstance(payload, dict):
        return augment_quota_entry(payload)
    if isinstance(payload, list):
        updated = []
        for entry in payload:
            if isinstance(entry, dict):
                updated.append(augment_quota_entry(entry))
            else:
                updated.append(entry)
        return updated
    return payload


def format_quota_entry(entry: dict, label: str | None = None) -> str:
    lines: list[str] = []
    if label:
        lines.append(label)
    if "license_type" in entry:
        lines.append(f"license_type: {entry.get('license_type')}")
    if "remaining_days" in entry:
        lines.append(f"remaining_days: {entry.get('remaining_days')}")

    bytes_used = entry.get("bytes_used")
    bytes_limit = entry.get("bytes_limit")
    bytes_remaining = entry.get("bytes_remaining")
    used_pct = entry.get("used_pct")
    remaining_pct = entry.get("remaining_pct")

    bytes_used_val = coerce_float(bytes_used)
    bytes_limit_val = coerce_float(bytes_limit)
    bytes_remaining_val = coerce_float(bytes_remaining)
    used_pct_val = coerce_float(used_pct)
    remaining_pct_val = coerce_float(remaining_pct)

    if bytes_used_val is not None:
        lines.append(
            f"bytes_used: {format_bytes(bytes_used_val)} ({int(bytes_used_val)} B)"
        )
    elif bytes_used is not None:
        lines.append(f"bytes_used: {bytes_used}")

    if bytes_limit_val is not None:
        lines.append(
            f"bytes_limit: {format_bytes(bytes_limit_val)} ({int(bytes_limit_val)} B)"
        )
    elif bytes_limit is not None:
        lines.append(f"bytes_limit: {bytes_limit}")

    if bytes_remaining_val is not None:
        lines.append(
            f"bytes_remaining: {format_bytes(bytes_remaining_val)} ({int(bytes_remaining_val)} B)"
        )
    elif bytes_remaining is not None:
        lines.append(f"bytes_remaining: {bytes_remaining}")

    if used_pct_val is not None:
        lines.append(f"used_pct: {used_pct_val:.2f}%")
    elif used_pct is not None:
        lines.append(f"used_pct: {used_pct}")

    if remaining_pct_val is not None:
        lines.append(f"remaining_pct: {remaining_pct_val:.2f}%")
    elif remaining_pct is not None:
        lines.append(f"remaining_pct: {remaining_pct}")

    if used_pct_val is not None:
        lines.append(f"usage: {render_pct_bar(used_pct_val)} used")
    return "\n".join(lines)


def format_quota_pretty(payload) -> str:
    if isinstance(payload, dict):
        return format_quota_entry(payload, label="Quota usage")
    if isinstance(payload, list):
        blocks: list[str] = []
        for idx, entry in enumerate(payload, start=1):
            if isinstance(entry, dict):
                blocks.append(format_quota_entry(entry, label=f"Quota usage #{idx}"))
            else:
                blocks.append(f"Quota usage #{idx}\n{entry}")
        return "\n\n".join(blocks)
    return str(payload)


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    resolved = resolve_pipeline_config(path)
    return resolved.data


def append_arg(argv: list[str], flag: str, value, *, formatter=str) -> None:
    if value is None:
        return
    if isinstance(value, str) and value == "":
        return
    argv.extend([flag, formatter(value)])


def append_repeat_args(argv: list[str], flag: str, values) -> None:
    if not values:
        return
    for entry in values:
        argv.extend([flag, str(entry)])


def append_bool_switch(
    argv: list[str],
    value: bool | None,
    *,
    true_flag: str,
    false_flag: str | None = None,
) -> None:
    if value is True:
        argv.append(true_flag)
    elif value is False and false_flag is not None:
        argv.append(false_flag)


def append_passthrough(argv: list[str], values) -> None:
    if values:
        items = list(values)
        if items and items[0] == "--":
            items = items[1:]
        argv.extend(items)


def init_rqdatac(args) -> object:
    load_dotenv()
    cfg = load_config(args.config) if getattr(args, "config", None) else {}
    data_cfg = cfg.get("data") if isinstance(cfg, dict) else None
    return _init_rqdatac_runtime(
        data_cfg=data_cfg,
        username=getattr(args, "username", None),
        password=getattr(args, "password", None),
        logger=logging.getLogger("market_data_platform.hk_assets.cli"),
        load_env=False,
        error_cls=SystemExit,
        import_error_message="rqdatac is not installed. Install with: pip install 'market-data-platform[rqdata]'",
        patch_fn=_patch_rqdatac_adjust_price_readonly,
    )
