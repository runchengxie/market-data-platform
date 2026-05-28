from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .repo_paths import find_repo_root
from .repo_paths import resolve_repo_path as resolve_repo_relative_path

REPO_ROOT = find_repo_root(__file__)


@dataclass(frozen=True)
class IntradayInputGroup:
    parquet_path: Path | None
    parts_dir: Path | None
    meta_path: Path | None

    @property
    def stem(self) -> str:
        if self.parquet_path is not None:
            return self.parquet_path.stem
        if self.parts_dir is not None:
            return self.parts_dir.name[: -len(".parts")]
        raise SystemExit("Intraday input group is missing both parquet_path and parts_dir.")

    @property
    def parent_dir(self) -> Path:
        if self.parquet_path is not None:
            return self.parquet_path.parent
        if self.parts_dir is not None:
            return self.parts_dir.parent
        raise SystemExit("Intraday input group is missing both parquet_path and parts_dir.")

    @property
    def identity(self) -> str:
        return str((self.parent_dir / self.stem).resolve())


def resolve_repo_path(path_text: str | Path) -> Path:
    return resolve_repo_relative_path(path_text, repo_root=REPO_ROOT)


def _default_parts_dir(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}.parts"


def _default_meta_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}.meta.json"


def _looks_like_intraday_asset_dir(path: Path) -> bool:
    data_dir = path / "data"
    if not data_dir.exists() or not data_dir.is_dir():
        return False

    manifest_path = path / "manifest.yml"
    if not manifest_path.exists():
        return True

    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("dataset") == "intraday"


def _group_sort_key(group: IntradayInputGroup) -> str:
    return group.identity


def _with_existing(value: Path | None) -> Path | None:
    if value is None or not value.exists():
        return None
    return value


def _merge_groups(
    groups_by_identity: dict[str, IntradayInputGroup],
    *,
    parquet_path: Path | None,
    parts_dir: Path | None,
    meta_path: Path | None,
) -> None:
    candidate = IntradayInputGroup(
        parquet_path=_with_existing(parquet_path),
        parts_dir=_with_existing(parts_dir),
        meta_path=_with_existing(meta_path),
    )
    identity = candidate.identity
    existing = groups_by_identity.get(identity)
    if existing is None:
        groups_by_identity[identity] = candidate
        return

    groups_by_identity[identity] = IntradayInputGroup(
        parquet_path=existing.parquet_path or candidate.parquet_path,
        parts_dir=existing.parts_dir or candidate.parts_dir,
        meta_path=existing.meta_path or candidate.meta_path,
    )


def _collect_groups_from_root(root: Path) -> list[IntradayInputGroup]:
    groups_by_identity: dict[str, IntradayInputGroup] = {}
    for parts_dir in sorted(root.glob("*.parts")):
        stem = parts_dir.name[: -len(".parts")]
        _merge_groups(
            groups_by_identity,
            parquet_path=root / f"{stem}.parquet",
            parts_dir=parts_dir,
            meta_path=root / f"{stem}.meta.json",
        )

    for parquet_path in sorted(root.glob("*.parquet")):
        if parquet_path.name.startswith("batch_"):
            continue
        _merge_groups(
            groups_by_identity,
            parquet_path=parquet_path,
            parts_dir=_default_parts_dir(parquet_path),
            meta_path=_default_meta_path(parquet_path),
        )

    return sorted(groups_by_identity.values(), key=_group_sort_key)


def resolve_intraday_input_groups(input_specs: list[str]) -> list[IntradayInputGroup]:
    groups_by_identity: dict[str, IntradayInputGroup] = {}

    for spec in input_specs:
        input_path = resolve_repo_path(spec)
        if not input_path.exists():
            raise SystemExit(f"Input intraday path not found: {input_path}")

        if input_path.is_file():
            if input_path.suffix.lower() != ".parquet":
                raise SystemExit(f"Unsupported input file type: {input_path}")
            _merge_groups(
                groups_by_identity,
                parquet_path=input_path,
                parts_dir=_default_parts_dir(input_path),
                meta_path=_default_meta_path(input_path),
            )
            continue

        if not input_path.is_dir():
            raise SystemExit(f"Unsupported input path: {input_path}")

        if input_path.name.endswith(".parts"):
            stem = input_path.name[: -len(".parts")]
            _merge_groups(
                groups_by_identity,
                parquet_path=input_path.parent / f"{stem}.parquet",
                parts_dir=input_path,
                meta_path=input_path.parent / f"{stem}.meta.json",
            )
            continue

        scan_root = (
            input_path / "data" if _looks_like_intraday_asset_dir(input_path) else input_path
        )
        groups = _collect_groups_from_root(scan_root)
        if not groups:
            raise SystemExit(f"No intraday parquet files found under: {input_path}")
        for group in groups:
            _merge_groups(
                groups_by_identity,
                parquet_path=group.parquet_path,
                parts_dir=group.parts_dir,
                meta_path=group.meta_path,
            )

    return sorted(groups_by_identity.values(), key=_group_sort_key)


def resolve_input_parquet_paths(input_specs: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for group in resolve_intraday_input_groups(input_specs):
        if group.parts_dir is not None:
            part_files = sorted(group.parts_dir.glob("batch_*.parquet"))
            if not part_files:
                part_files = sorted(group.parts_dir.glob("*.parquet"))
            if part_files:
                resolved.extend(part_files)
                continue
        if group.parquet_path is not None and group.parquet_path.exists():
            resolved.append(group.parquet_path)
            continue
        raise SystemExit(
            f"No parquet files found for intraday input: {group.parent_dir / group.stem}"
        )

    return resolved


__all__ = [
    "IntradayInputGroup",
    "resolve_input_parquet_paths",
    "resolve_intraday_input_groups",
    "resolve_repo_path",
]
