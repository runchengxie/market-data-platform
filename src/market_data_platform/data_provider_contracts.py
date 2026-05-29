"""Provider and market boundary helpers for supported RQData workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

SUPPORTED_MARKETS = {"hk", "cn"}


@dataclass(frozen=True)
class MarketSpec:
    market: str
    provider_market: str
    canonical_suffixes: tuple[str, ...]
    rqdata_suffixes: tuple[str, ...]


MARKET_SPECS = {
    "hk": MarketSpec(
        market="hk",
        provider_market="hk",
        canonical_suffixes=(".HK",),
        rqdata_suffixes=(".XHKG",),
    ),
    "cn": MarketSpec(
        market="cn",
        provider_market="cn",
        canonical_suffixes=(".SH", ".SZ"),
        rqdata_suffixes=(".XSHG", ".XSHE"),
    ),
}


def normalize_market(market: str | None, *, default: str | None = "hk") -> str | None:
    fallback = None if default is None else str(default).strip().lower() or None
    value = str(market).strip().lower() if market is not None else None
    return value or fallback


def resolve_provider(
    data_cfg: Mapping | None, *, default: str | None = "rqdata"
) -> str | None:
    if not isinstance(data_cfg, Mapping):
        return default
    raw = data_cfg.get("provider", default)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in {"rqdatac", "rqdata"}:
        return "rqdata"
    return value or default


def fundamentals_provider_supported(provider: str, market: str) -> bool:
    normalized_provider = resolve_provider({"provider": provider}, default="rqdata")
    normalized_market = normalize_market(market)
    return normalized_provider == "rqdata" and normalized_market == "hk"


def require_supported_market(market: str) -> str:
    normalized_market = normalize_market(market)
    if normalized_market not in SUPPORTED_MARKETS:
        supported = ", ".join(sorted(SUPPORTED_MARKETS))
        raise ValueError(
            f"Unsupported market '{normalized_market}'. Supported markets: {supported}."
        )
    return normalized_market


def hk_to_rqdata_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return text
    if text.endswith(".XHKG"):
        return text
    if text.endswith(".HK"):
        text = text[:-3]
    if text.isdigit():
        text = text.zfill(5)
    return f"{text}.XHKG"


def _infer_cn_suffix(code: str) -> str | None:
    if code.startswith(("5", "6", "9")):
        return ".XSHG"
    if code.startswith(("0", "2", "3")):
        return ".XSHE"
    return None


def cn_to_rqdata_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return text
    if text.endswith((".XSHG", ".XSHE")):
        return text
    if text.endswith(".SH"):
        return f"{text[:-3].zfill(6)}.XSHG"
    if text.endswith(".SZ"):
        return f"{text[:-3].zfill(6)}.XSHE"
    if text.isdigit():
        suffix = _infer_cn_suffix(text.zfill(6))
        if suffix is not None:
            return f"{text.zfill(6)}{suffix}"
    return text


def to_rqdata_symbol(market: str, symbol: str) -> str:
    market = require_supported_market(market)
    if market == "hk":
        return hk_to_rqdata_symbol(symbol)
    if market == "cn":
        return cn_to_rqdata_symbol(symbol)
    return str(symbol or "").strip()
