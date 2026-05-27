"""Provider retry and quota-error helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from market_data_platform.hk_depth.exceptions import ProviderRequestError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    """Value returned by a provider call plus the attempt count."""

    value: T
    attempts: int


def looks_like_quota_error(exc: BaseException) -> bool:
    """Return whether an exception message looks like a quota exhaustion error."""
    category = getattr(exc, "category", None)
    if str(category).lower() == "quota":
        return True
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
    return any(term in text for term in quota_terms) and any(
        term in text for term in exhaustion_terms
    )


def retry_provider_call(
    label: str,
    action: Callable[[], T],
    *,
    max_attempts: int = 1,
    backoff_seconds: float = 0.0,
    max_backoff_seconds: float = 60.0,
) -> RetryResult[T]:
    """Run a provider action with retry/backoff for non-quota failures."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")
    if backoff_seconds < 0:
        raise ValueError("backoff_seconds must be non-negative.")
    if max_backoff_seconds < 0:
        raise ValueError("max_backoff_seconds must be non-negative.")

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return RetryResult(value=action(), attempts=attempt)
        except Exception as exc:
            last_exc = exc
            if looks_like_quota_error(exc):
                raise ProviderRequestError("quota", label, str(exc)) from exc
            if attempt < max_attempts:
                sleep_for = min(backoff_seconds * (2 ** (attempt - 1)), max_backoff_seconds)
                if sleep_for > 0:
                    time.sleep(sleep_for)

    assert last_exc is not None
    category = getattr(last_exc, "category", "provider_error")
    raise ProviderRequestError(str(category), label, str(last_exc)) from last_exc
