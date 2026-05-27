"""RQData provider adapter."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Protocol

import pandas as pd

from market_data_platform.hk_depth.exceptions import ProviderRequestError
from market_data_platform.hk_depth.quota import augment_quota_payload, quota_to_payload


class TickDataProvider(Protocol):
    """Provider interface used by download workflows."""

    def get_price(
        self,
        order_book_ids: Sequence[str],
        start_date: str,
        end_date: str,
        fields: Sequence[str],
        adjust_type: str = "none",
        time_slice: str | None = None,
    ) -> pd.DataFrame:
        """Return tick data for the requested identifiers and dates."""

    def quota_snapshot(self) -> Any:
        """Return account/quota data when available."""

    def get_trading_dates(self, start_date: str, end_date: str) -> list[str]:
        """Return HK trading dates formatted as YYYYMMDD."""


def classify_provider_exception(exc: BaseException) -> str:
    """Map provider exceptions into stable categories for metadata and CLI output."""
    message = str(exc).lower()
    if any(token in message for token in ("auth", "login", "password", "token", "credential")):
        return "authentication"
    if any(
        token in message
        for token in ("permission", "entitle", "unauthor", "forbidden", "no access")
    ):
        return "entitlement"
    if any(token in message for token in ("field", "invalid", "not support", "unsupported")):
        return "invalid_field"
    if any(token in message for token in ("empty", "no data", "not found")):
        return "empty_data"
    return "provider_error"


class RQDataClient:
    """Thin wrapper around `rqdatac` for HK historical tick snapshots."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        uri: str | None = None,
        initialize: bool = True,
    ) -> None:
        self._load_dotenv()
        try:
            import rqdatac  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on optional provider package
            raise ProviderRequestError(
                "authentication",
                "import_rqdatac",
                "rqdatac is not installed; install with `uv sync --extra rqdata`.",
            ) from exc

        self._rqdatac = rqdatac
        self.username = username or os.getenv("RQDATA_USERNAME") or os.getenv("RQDATA_USER")
        self.password = password or os.getenv("RQDATA_PASSWORD")
        self.uri = uri or os.getenv("RQDATA_URI")
        if initialize:
            self._initialize()

    @staticmethod
    def _load_dotenv() -> None:
        try:
            from dotenv import load_dotenv
        except Exception:
            return
        load_dotenv()

    def _initialize(self) -> None:
        try:
            init = self._rqdatac.init
            if self.username and self.password and self.uri:
                try:
                    init(self.uri, self.username, self.password)
                except TypeError:
                    init(username=self.username, password=self.password, address=self.uri)
            elif self.username and self.password:
                try:
                    init(username=self.username, password=self.password)
                except TypeError:
                    init(self.username, self.password)
            else:
                init()
        except Exception as exc:  # pragma: no cover - provider-specific behavior
            raise ProviderRequestError(
                classify_provider_exception(exc), "initialize_rqdata", str(exc)
            ) from exc

    def get_price(
        self,
        order_book_ids: Sequence[str],
        start_date: str,
        end_date: str,
        fields: Sequence[str],
        adjust_type: str = "none",
        time_slice: str | None = None,
    ) -> pd.DataFrame:
        kwargs = {
            "start_date": start_date,
            "end_date": end_date,
            "frequency": "tick",
            "fields": list(fields),
            "adjust_type": adjust_type,
            "market": "hk",
            "expect_df": True,
        }
        if time_slice:
            kwargs["time_slice"] = time_slice
        try:
            return self._rqdatac.get_price(
                list(order_book_ids),
                **kwargs,
            )
        except Exception as exc:  # pragma: no cover - provider-specific behavior
            raise ProviderRequestError(
                classify_provider_exception(exc), "get_price", str(exc)
            ) from exc

    def quota_snapshot(self) -> Any:
        user = getattr(self._rqdatac, "user", None)
        if user is not None:
            getter = getattr(user, "get_quota", None)
            if callable(getter):
                try:
                    return augment_quota_payload(quota_to_payload(getter()))
                except Exception:
                    pass
        for name in ("get_quota", "quota", "get_account_info", "user_info"):
            attr = getattr(self._rqdatac, name, None)
            if attr is None:
                continue
            try:
                value = attr() if callable(attr) else attr
            except Exception:
                continue
            if value is None:
                continue
            payload = augment_quota_payload(quota_to_payload(value))
            return payload if isinstance(payload, dict) else {"value": payload}
        return None

    def get_trading_dates(self, start_date: str, end_date: str) -> list[str]:
        getter = getattr(self._rqdatac, "get_trading_dates", None)
        if not callable(getter):
            raise ProviderRequestError(
                "provider_error",
                "get_trading_dates",
                "rqdatac.get_trading_dates is unavailable.",
            )
        try:
            values = getter(start_date, end_date, market="hk")
        except TypeError:
            values = getter(start_date, end_date)
        except Exception as exc:  # pragma: no cover - provider-specific behavior
            raise ProviderRequestError(
                classify_provider_exception(exc), "get_trading_dates", str(exc)
            ) from exc
        return [
            pd.Timestamp(value).strftime("%Y%m%d")
            for value in values
            if pd.notna(pd.Timestamp(value))
        ]
