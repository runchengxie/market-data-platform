from __future__ import annotations

from pathlib import Path


def find_repo_root(start: str | Path | None = None) -> Path:
    candidate = Path(start or Path.cwd()).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "pyproject.toml").exists():
            return directory
    return Path.cwd().resolve()


def resolve_repo_path(path_text: str | Path, *, repo_root: Path | None = None) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = repo_root.resolve() if repo_root is not None else Path.cwd().resolve()
    return (base / path).resolve()
