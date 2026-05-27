from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MirrorEntry:
    symbol: str
    order_book_id: str
    path: Path
    rows: int
    total_bytes: int
    min_quarter: str | None
    max_quarter: str | None
    min_info_date: str | None
    max_info_date: str | None


@dataclass(frozen=True)
class MirrorAuditRecord:
    symbol: str
    order_book_id: str
    status: str
    attempts: int
    rows: int
    total_bytes: int
    min_quarter: str | None
    max_quarter: str | None
    min_info_date: str | None
    max_info_date: str | None
    started_at: str | None
    finished_at: str | None
    file_mtime: str | None
    dropped_fields: str | None
    error: str | None


@dataclass(frozen=True)
class DailyMirrorEntry:
    symbol: str
    order_book_id: str
    path: Path
    rows: int
    total_bytes: int
    min_trade_date: str | None
    max_trade_date: str | None


@dataclass(frozen=True)
class DailyMirrorAuditRecord:
    symbol: str
    order_book_id: str
    status: str
    attempts: int
    rows: int
    total_bytes: int
    min_trade_date: str | None
    max_trade_date: str | None
    started_at: str | None
    finished_at: str | None
    file_mtime: str | None
    error: str | None


@dataclass(frozen=True)
class DatedMirrorEntry:
    symbol: str
    order_book_id: str
    path: Path
    rows: int
    total_bytes: int
    min_date: str | None
    max_date: str | None


@dataclass(frozen=True)
class DatedMirrorAuditRecord:
    symbol: str
    order_book_id: str
    status: str
    attempts: int
    rows: int
    total_bytes: int
    min_date: str | None
    max_date: str | None
    started_at: str | None
    finished_at: str | None
    file_mtime: str | None
    dropped_fields: str | None
    error: str | None


@dataclass(frozen=True)
class DatedRequestGroup:
    symbol: str
    request_ids: tuple[str, ...]
    order_book_ids: tuple[str, ...]


class MirrorFetchError(RuntimeError):
    def __init__(self, message: str, *, attempts: int):
        super().__init__(message)
        self.attempts = attempts


class MirrorQuotaError(MirrorFetchError):
    pass
