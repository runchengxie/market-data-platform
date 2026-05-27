from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from .shared import _load_text_list, _normalize_absolute_date, _normalize_hk_symbol


def parse_compact_date(value: object, *, label: str) -> pd.Timestamp:
    normalized = _normalize_absolute_date(value, label=label)
    return pd.to_datetime(normalized, format="%Y%m%d").normalize()


def format_date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.normalize().strftime("%Y-%m-%d")


def normalize_symbol_list(values: Sequence[object]) -> list[str]:
    normalized = [_normalize_hk_symbol(value) for value in values]
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in normalized:
        text = str(symbol or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def load_symbols_from_text(path_text: str | Path) -> list[str]:
    return normalize_symbol_list(_load_text_list(path_text, label="Symbols file"))
