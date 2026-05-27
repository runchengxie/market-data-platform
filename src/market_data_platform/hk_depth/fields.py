"""Field definitions for RQData Hong Kong ten-level depth snapshots."""

from __future__ import annotations

BASE_TICK_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "last",
    "prev_close",
    "volume",
    "total_turnover",
    "num_trades",
    "limit_up",
    "limit_down",
    "change_rate",
)
ASK_PRICE_FIELDS: tuple[str, ...] = tuple(f"a{i}" for i in range(1, 11))
ASK_VOLUME_FIELDS: tuple[str, ...] = tuple(f"a{i}_v" for i in range(1, 11))
BID_PRICE_FIELDS: tuple[str, ...] = tuple(f"b{i}" for i in range(1, 11))
BID_VOLUME_FIELDS: tuple[str, ...] = tuple(f"b{i}_v" for i in range(1, 11))

DEFAULT_TICK_DEPTH_FIELDS: tuple[str, ...] = (
    *BASE_TICK_FIELDS,
    *ASK_PRICE_FIELDS,
    *ASK_VOLUME_FIELDS,
    *BID_PRICE_FIELDS,
    *BID_VOLUME_FIELDS,
)


def parse_fields(value: str | None) -> list[str]:
    """Parse a comma or whitespace separated field list."""
    if value is None or not value.strip():
        return list(DEFAULT_TICK_DEPTH_FIELDS)
    fields: list[str] = []
    for chunk in value.replace(",", " ").split():
        field = chunk.strip()
        if field and field not in fields:
            fields.append(field)
    return fields
