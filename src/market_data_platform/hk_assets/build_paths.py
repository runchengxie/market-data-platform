from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .shared import DEFAULT_PIPELINE_FUNDAMENTALS_NAME, _resolve_path


def default_pipeline_fundamentals_path(asset_dir: Path) -> Path:
    return asset_dir / DEFAULT_PIPELINE_FUNDAMENTALS_NAME


def resolve_pipeline_fundamentals_out_path(args, asset_dir: Path) -> Path:
    out = getattr(args, "out", None)
    if out:
        return _resolve_path(out)
    return default_pipeline_fundamentals_path(asset_dir)


def pipeline_fundamentals_manifest_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}.manifest.yml")


def write_symbol_list(path: Path, symbols: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(str(symbol).strip() for symbol in symbols if str(symbol).strip())
    if content:
        content += "\n"
    path.write_text(content, encoding="utf-8")
