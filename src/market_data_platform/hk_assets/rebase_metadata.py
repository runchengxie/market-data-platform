from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from market_data_platform.current_assets import (
    build_hk_current_contract,
    default_dataset_registry_path,
    default_hk_current_contract_path,
    load_current_contract,
    write_current_contract,
    write_dataset_registry,
)


_TEXT_SUFFIXES = {".csv", ".json", ".txt", ".yaml", ".yml"}
_METADATA_ROOT_NAMES = ("assets", "metadata")


def _normalize_prefix(value: str | Path, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SystemExit(f"{label} must not be empty.")
    return os.path.abspath(os.path.normpath(os.path.expanduser(text))).rstrip("/")


def _candidate_metadata_files(artifacts_root: Path) -> list[Path]:
    files: list[Path] = []
    for root_name in _METADATA_ROOT_NAMES:
        root = artifacts_root / root_name
        if not root.exists():
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in _TEXT_SUFFIXES
        )
    return sorted(files)


def _write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.rebase.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def build_hk_asset_metadata_rebase_report(args: Any) -> dict[str, Any]:
    artifacts_root = Path(args.artifacts_root).expanduser().resolve()
    from_prefix = _normalize_prefix(args.from_prefix, label="--from-prefix")
    to_prefix = _normalize_prefix(
        getattr(args, "to_prefix", None) or artifacts_root.parent,
        label="--to-prefix",
    )
    if from_prefix == to_prefix:
        raise SystemExit("--from-prefix and --to-prefix resolve to the same path.")

    execute = bool(getattr(args, "execute", False))
    max_file_bytes = int(getattr(args, "max_file_bytes", 5_000_000))
    if max_file_bytes <= 0:
        raise SystemExit("--max-file-bytes must be > 0.")
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    files_scanned = 0

    for path in _candidate_metadata_files(artifacts_root):
        size = path.stat().st_size
        if size > max_file_bytes:
            skipped.append(
                {
                    "path": str(path),
                    "reason": "larger_than_max_file_bytes",
                    "bytes": size,
                }
            )
            continue
        files_scanned += 1
        content = path.read_text(encoding="utf-8", errors="strict")
        replacement_count = content.count(from_prefix)
        if not replacement_count:
            continue
        changes.append(
            {
                "path": str(path),
                "replacements": replacement_count,
                "status": "updated" if execute else "dry-run",
            }
        )
        if execute:
            _write_text_atomic(path, content.replace(from_prefix, to_prefix))

    contract_rebuilt = False
    if execute:
        contract_path = default_hk_current_contract_path(artifacts_root)
        previous_contract = load_current_contract(contract_path)
        if previous_contract is not None:
            contract_meta = previous_contract.get("contract")
            target_date = (
                str(contract_meta.get("target_date") or "").strip()
                if isinstance(contract_meta, dict)
                else ""
            )
            rebuilt_contract = build_hk_current_contract(
                artifacts_root,
                generated_by="rebase-hk-asset-metadata",
                target_date=target_date or None,
            )
            write_current_contract(contract_path, rebuilt_contract)
            write_dataset_registry(default_dataset_registry_path(artifacts_root), rebuilt_contract)
            contract_rebuilt = True

    return {
        "summary": {
            "artifacts_root": str(artifacts_root),
            "from_prefix": from_prefix,
            "to_prefix": to_prefix,
            "execute": execute,
            "files_scanned": files_scanned,
            "files_changed": len(changes),
            "replacements": sum(int(item["replacements"]) for item in changes),
            "skipped_large_files": len(skipped),
            "current_contract_rebuilt": contract_rebuilt,
        },
        "changes": changes,
        "skipped": skipped,
    }


def _render_text(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "HK asset metadata rebase",
        f"from_prefix: {summary['from_prefix']}",
        f"to_prefix: {summary['to_prefix']}",
        f"mode: {'execute' if summary['execute'] else 'dry-run'}",
        f"files_scanned: {summary['files_scanned']}",
        f"files_changed: {summary['files_changed']}",
        f"replacements: {summary['replacements']}",
        f"current_contract_rebuilt: {summary['current_contract_rebuilt']}",
    ]
    for item in payload["changes"]:
        lines.append(f"{item['status']}: {item['path']} ({item['replacements']} replacement(s))")
    return "\n".join(lines)


def rebase_hk_asset_metadata(args: Any) -> int:
    payload = build_hk_asset_metadata_rebase_report(args)
    output = (
        json.dumps(payload, ensure_ascii=False, indent=2)
        if getattr(args, "format", "text") == "json"
        else _render_text(payload)
    )
    out_path = getattr(args, "out", None)
    if out_path:
        path = Path(out_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


__all__ = ["build_hk_asset_metadata_rebase_report", "rebase_hk_asset_metadata"]
