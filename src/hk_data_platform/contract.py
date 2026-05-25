from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from hk_data_platform.manifest import load_manifest_summary
from hk_data_platform.paths import (
    candidate_asset_paths,
    current_contract_path,
    normalize_market,
    resolve_artifacts_root,
)


def infer_manifest_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidates: list[Path] = []
    if path.is_dir():
        candidates.append(path / "manifest.yml")
    else:
        candidates.append(path.with_name(f"{path.stem}.manifest.yml"))
        candidates.append(path.parent / "manifest.yml")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _detect_as_of(value: object | None) -> str | None:
    text = str(value or "")
    digits = "".join(char for char in text if char.isdigit())
    return digits[:8] if len(digits) >= 8 else None


def _path_kind(path: Path) -> str:
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "missing"


def describe_current_path(path: Path) -> dict[str, Any]:
    alias_path = path.expanduser()
    if not alias_path.is_absolute():
        alias_path = alias_path.absolute()
    resolved_path = alias_path.resolve(strict=False)
    manifest_path = infer_manifest_path(alias_path)
    manifest = load_manifest_summary(manifest_path) if manifest_path is not None else None
    as_of = None
    if isinstance(manifest, Mapping):
        as_of = str(manifest.get("query_end_date") or "").strip() or None
    if not as_of:
        as_of = _detect_as_of(resolved_path.name)
    return {
        "alias_path": str(alias_path),
        "exists": alias_path.exists(),
        "is_symlink": alias_path.is_symlink(),
        "path_kind": _path_kind(alias_path),
        "resolved_path": str(resolved_path),
        "resolved_name": resolved_path.name,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "manifest": manifest,
        "as_of": as_of,
    }


def build_current_contract(
    artifacts_root: str | Path | None = None,
    *,
    market: str | None = None,
    generated_by: str | None = None,
    target_date: str | None = None,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    market = normalize_market(market)
    contract_path = current_contract_path(root, market=market)
    contract_name = f"{market}_current"
    return {
        "contract": {
            "name": contract_name,
            "market": market,
            "version": 1,
            "artifacts_root": str(root),
            "contract_path": str(contract_path),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "generated_by": generated_by,
            "target_date": target_date,
        },
        "assets": {
            asset_key: describe_current_path(path)
            for asset_key, path in candidate_asset_paths(root, market=market).items()
        },
    }


def write_current_contract(path: str | Path, payload: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
