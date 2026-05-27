from __future__ import annotations

from collections.abc import Sequence
import re
import time

import pandas as pd

from .models import MirrorFetchError, MirrorQuotaError


def _fetch_hk_ex_factors_direct(
    request_ids: Sequence[str],
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    from rqdatac.client import get_client

    payload = get_client().execute(
        "get_ex_factor",
        list(request_ids),
        int(start_date),
        int(end_date),
        market="hk",
    )
    return pd.DataFrame(payload or [])


def _fetch_hk_dividends_direct(
    request_ids: Sequence[str],
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    from rqdatac.client import get_client

    payload = get_client().execute(
        "get_dividend",
        list(request_ids),
        int(start_date),
        int(end_date),
        market="hk",
    )
    return pd.DataFrame(payload or [])


def _fetch_hk_shares_direct(
    request_ids: Sequence[str],
    *,
    fields: Sequence[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    from rqdatac.client import get_client

    payload = get_client().execute(
        "get_shares_v2",
        list(request_ids),
        list(fields),
        start_date=int(start_date),
        end_date=int(end_date),
        market="hk",
    )
    return pd.DataFrame(payload or [])


def _looks_like_quota_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    quota_terms = (
        "quota",
        "bytes_limit",
        "bytes_used",
        "traffic",
        "流量",
        "配额",
        "限额",
    )
    exhaustion_terms = (
        "exceed",
        "exceeded",
        "used up",
        "用完",
        "超出",
        "达到",
        "不足",
        "limit",
    )
    return any(term in text for term in quota_terms) and any(term in text for term in exhaustion_terms)


def _looks_like_provider_permission_error(exc: Exception | str) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    permission_terms = (
        "permission",
        "no permission",
        "not authorized",
        "unauthorized",
        "access denied",
        "无权限",
    )
    provider_terms = (
        "ricequant",
        "rqdata",
        "instrument",
        "instruments",
        "day bar",
        "access",
        "权限",
    )
    return any(term in text for term in permission_terms) and any(term in text for term in provider_terms)


def _extract_invalid_field_name(error_text: str) -> str | None:
    if not error_text:
        return None
    match = re.search(r"got invalided value ([^,\s]+)", str(error_text), flags=re.IGNORECASE)
    if not match:
        return None
    field = str(match.group(1)).strip()
    return field or None


def _retry_fetch(
    label: str,
    action,
    *,
    max_attempts: int,
    backoff_seconds: float,
    max_backoff_seconds: float,
):
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return action(), attempt
        except Exception as exc:
            last_exc = exc
            if _looks_like_quota_error(exc):
                raise MirrorQuotaError(str(exc), attempts=attempt) from exc
            if attempt < max_attempts:
                sleep_for = min(backoff_seconds * (2 ** (attempt - 1)), max_backoff_seconds)
                if sleep_for > 0:
                    time.sleep(sleep_for)
    if last_exc is not None:
        raise MirrorFetchError(f"{label}: {last_exc}", attempts=max_attempts) from last_exc
    raise MirrorFetchError(f"{label}: unknown error", attempts=max_attempts)


def _ensure_rqdatac_hk_plugin() -> None:
    try:
        import rqdatac_hk  # noqa: F401
    except ImportError as exc:
        raise SystemExit("rqdatac-hk is not installed. Install with: pip install '.[rqdata]'") from exc
