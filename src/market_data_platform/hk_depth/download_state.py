"""Bounded metadata state for long-running downloads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

DOWNLOAD_DETAIL_COLLECTIONS = (
    "planned_batches",
    "completed_batches",
    "skipped_batches",
    "failed_batches",
    "planned_units",
    "completed_units",
    "skipped_units",
    "invalid_units",
    "empty_units",
    "failed_units",
    "quota_blocked_batches",
    "quota_blocked_units",
)


def download_detail_path(dataset_root: str | Path, kind: str, run_id: str) -> Path:
    return Path(dataset_root) / "meta" / f"{kind}_details_{run_id[:12]}.jsonl"


class DownloadMetadataRecorder:
    """Stream full download details and retain bounded inline metadata samples."""

    def __init__(
        self,
        metadata: dict[str, Any],
        path: str | Path,
        *,
        inline_limit: int,
    ) -> None:
        if inline_limit < 0:
            raise ValueError("metadata_detail_limit must be non-negative.")
        self.metadata = metadata
        self.path = Path(path)
        self.inline_limit = inline_limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream: TextIO = self.path.open("w", encoding="utf-8")
        self.metadata["detail_records_path"] = str(self.path)
        self.metadata["detail_inline_limit"] = inline_limit
        self.metadata["detail_counts"] = dict.fromkeys(DOWNLOAD_DETAIL_COLLECTIONS, 0)
        self.metadata["detail_lists_truncated"] = []

    def record(self, collection: str, value: Mapping[str, Any]) -> None:
        if collection not in DOWNLOAD_DETAIL_COLLECTIONS:
            raise ValueError(f"Unsupported metadata detail collection: {collection}")
        row = dict(value)
        self._stream.write(
            json.dumps({"collection": collection, **row}, default=str, sort_keys=True) + "\n"
        )
        counts = self.metadata["detail_counts"]
        counts[collection] = int(counts[collection]) + 1
        inline_rows = self.metadata[collection]
        if len(inline_rows) < self.inline_limit:
            inline_rows.append(row)
        elif collection not in self.metadata["detail_lists_truncated"]:
            self.metadata["detail_lists_truncated"].append(collection)

    def count(self, collection: str) -> int:
        return int(self.metadata["detail_counts"][collection])

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.flush()
        self._stream.close()
