from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from .artifacts import (
    CACHE_DIR as DEFAULT_CACHE_DIR,
)
from .artifacts import (
    SNAPSHOTS_DIR as DEFAULT_SNAPSHOTS_DIR,
)
from .artifacts import (
    UNIVERSE_DIR as DEFAULT_UNIVERSE_DIR,
)
from .artifacts import (
    default_path_text,
)
from .current_assets import default_hk_current_contract_path, load_current_contract


@dataclass(frozen=True)
class SnapshotEntry:
    source: Path
    target: Path
    kind: str
    file_count: int
    total_bytes: int


def _default_name() -> str:
    return datetime.now().strftime("snapshot_%Y%m%d_%H%M%S")


def _resolve_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def _describe_path(path: Path) -> tuple[str, int, int]:
    if path.is_file():
        return "file", 1, int(path.stat().st_size)
    if path.is_dir():
        file_count = 0
        total_bytes = 0
        for child in path.rglob("*"):
            if child.is_file():
                file_count += 1
                total_bytes += int(child.stat().st_size)
        return "directory", file_count, total_bytes
    raise SystemExit(f"Unsupported path type: {path}")


def _relative_target_path(source: Path, repo_root: Path) -> Path:
    try:
        return source.relative_to(repo_root)
    except ValueError:
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
        return Path("external") / f"{source.name}_{digest}"


def _copy_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _prune_nested_paths(paths: Iterable[Path]) -> list[Path]:
    items = list(paths)
    directory_paths = [
        path
        for path in items
        if path.exists() and path.is_dir()
    ]
    pruned: list[Path] = []
    for path in items:
        if any(parent != path and path.is_relative_to(parent) for parent in directory_paths):
            continue
        pruned.append(path)
    return pruned


def _current_contract_backup_paths(
    *,
    repo_root: Path,
    preset: str | None,
) -> tuple[list[Path], dict[str, object] | None]:
    if preset != "hk_current":
        return [], None
    contract_path = default_hk_current_contract_path(repo_root / "artifacts")
    contract = load_current_contract(contract_path)
    if not isinstance(contract, dict):
        raise SystemExit(f"Current contract not found or invalid: {contract_path}")

    assets = contract.get("assets")
    if not isinstance(assets, dict):
        raise SystemExit(f"Current contract is missing assets: {contract_path}")

    selected_paths: list[Path] = [contract_path]
    selected_asset_keys: list[str] = []
    for asset_key, entry in assets.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("exists") is not True:
            continue
        resolved_path_text = str(entry.get("resolved_path") or "").strip()
        if not resolved_path_text:
            continue
        resolved_path = _resolve_path(resolved_path_text)
        selected_paths.append(resolved_path)
        selected_asset_keys.append(str(asset_key))

        manifest_path_text = str(entry.get("manifest_path") or "").strip()
        if not manifest_path_text:
            continue
        manifest_path = _resolve_path(manifest_path_text)
        if not manifest_path.exists():
            continue
        if resolved_path.is_dir():
            try:
                manifest_path.relative_to(resolved_path)
                continue
            except ValueError:
                pass
        if manifest_path != resolved_path:
            selected_paths.append(manifest_path)

    return selected_paths, {
        "preset": "hk_current",
        "current_contract_path": str(contract_path),
        "current_asset_keys": selected_asset_keys,
    }


def _build_manifest(
    *,
    name: str,
    repo_root: Path,
    output_dir: Path,
    entries: Iterable[SnapshotEntry],
    git: dict | None = None,
    selection: dict[str, object] | None = None,
) -> dict:
    entries_list = list(entries)
    manifest = {
        "name": name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "entries": [
            {
                "source": str(item.source),
                "target": str(item.target),
                "kind": item.kind,
                "file_count": item.file_count,
                "total_bytes": item.total_bytes,
            }
            for item in entries_list
        ],
        "totals": {
            "paths": len(entries_list),
            "files": sum(item.file_count for item in entries_list),
            "bytes": sum(item.total_bytes for item in entries_list),
        },
    }
    if git:
        manifest["git"] = git
    if selection:
        manifest["selection"] = selection
    return manifest


def _read_git_value(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def _git_metadata(repo_root: Path) -> dict | None:
    commit = _read_git_value(repo_root, "rev-parse", "HEAD")
    if not commit:
        return None
    short_commit = _read_git_value(repo_root, "rev-parse", "--short", "HEAD")
    branch = _read_git_value(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _read_git_value(repo_root, "status", "--short")
    return {
        "commit": commit,
        "short_commit": short_commit,
        "branch": branch,
        "is_dirty": bool(status),
    }


def add_backup_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--preset",
        choices=["hk_current"],
        default=None,
        help=(
            "Optional backup selection preset. "
            "`hk_current` freezes the current HK asset set declared by hk_current.json."
        ),
    )
    parser.add_argument(
        "--out-root",
        default=default_path_text(DEFAULT_SNAPSHOTS_DIR),
        help=(
            "Snapshot root directory. "
            f"Default: {default_path_text(DEFAULT_SNAPSHOTS_DIR)}"
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Snapshot folder name. Default: snapshot_<timestamp>",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="Config file to include. Repeatable.",
    )
    parser.add_argument(
        "--include-path",
        action="append",
        default=[],
        help="Extra file or directory to include. Repeatable.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=f"Do not include {default_path_text(DEFAULT_CACHE_DIR)}/.",
    )
    parser.add_argument(
        "--no-universe",
        action="store_true",
        help=f"Do not include {default_path_text(DEFAULT_UNIVERSE_DIR)}/.",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="Skip missing paths instead of failing.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="marketdata backup-data",
        description="Create a private local snapshot of caches, universe files, and configs.",
    )
    add_backup_data_args(parser)
    args = parser.parse_args(argv)

    repo_root = Path.cwd().resolve()
    name = args.name or _default_name()
    out_root = _resolve_path(args.out_root)
    output_dir = out_root / name
    if output_dir.exists():
        raise SystemExit(f"Refusing to overwrite existing snapshot: {output_dir}")

    requested: list[Path] = []
    preset_paths, selection = _current_contract_backup_paths(
        repo_root=repo_root,
        preset=getattr(args, "preset", None),
    )
    requested.extend(preset_paths)
    if not args.no_cache:
        requested.append(_resolve_path(DEFAULT_CACHE_DIR))
    if not args.no_universe:
        requested.append(_resolve_path(DEFAULT_UNIVERSE_DIR))
    requested.extend(_resolve_path(item) for item in (args.config or []))
    requested.extend(_resolve_path(item) for item in (args.include_path or []))

    deduped: list[Path] = []
    seen: set[Path] = set()
    for item in requested:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    deduped = _prune_nested_paths(deduped)

    if not deduped:
        raise SystemExit("No paths selected for backup.")

    output_dir.mkdir(parents=True, exist_ok=False)
    copied: list[SnapshotEntry] = []
    try:
        for source in deduped:
            if not source.exists():
                if args.skip_missing:
                    continue
                raise SystemExit(f"Path not found: {source}")
            kind, file_count, total_bytes = _describe_path(source)
            rel_target = _relative_target_path(source, repo_root)
            target = output_dir / rel_target
            _copy_path(source, target)
            copied.append(
                SnapshotEntry(
                    source=source,
                    target=target,
                    kind=kind,
                    file_count=file_count,
                    total_bytes=total_bytes,
                )
            )

        manifest = _build_manifest(
            name=name,
            repo_root=repo_root,
            output_dir=output_dir,
            entries=copied,
            git=_git_metadata(repo_root),
            selection=selection,
        )
        manifest_path = output_dir / "manifest.yml"
        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        raise

    totals = manifest["totals"]
    print(
        f"Snapshot written to {output_dir} "
        f"({totals['paths']} paths, {totals['files']} files, {totals['bytes']} bytes)"
    )
    return 0
