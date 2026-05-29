from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ARTIFACTS_ROOT = Path("artifacts")
CACHE_DIR = ARTIFACTS_ROOT / "cache"
ASSETS_DIR = ARTIFACTS_ROOT / "assets"
METADATA_DIR = ARTIFACTS_ROOT / "metadata"
STANDARDIZED_DIR = ARTIFACTS_ROOT / "standardized"
FUNDAMENTALS_DIR = ASSETS_DIR / "fundamentals"
RQDATA_ASSETS_DIR = ASSETS_DIR / "rqdata"
UNIVERSE_DIR = ASSETS_DIR / "universe"
UNIVERSE_BY_DATE_FILE = UNIVERSE_DIR / "universe_by_date.csv"
HK_CONNECT_SYMBOLS_FILE = UNIVERSE_DIR / "hk_connect_symbols.txt"
UNIVERSE_META_FILE = UNIVERSE_DIR / "universe_by_date.meta.yml"
HK_ALL_FULL_BY_DATE_FILE = UNIVERSE_DIR / "hk_all_full_by_date.csv"
HK_ALL_FULL_SYMBOLS_FILE = UNIVERSE_DIR / "hk_all_full_symbols.txt"
HK_ALL_FULL_META_FILE = UNIVERSE_DIR / "hk_all_full_by_date.meta.yml"
RUNS_DIR = ARTIFACTS_ROOT / "runs"
LIVE_RUNS_DIR = ARTIFACTS_ROOT / "live_runs"
SWEEPS_DIR = ARTIFACTS_ROOT / "sweeps"
SNAPSHOTS_DIR = ARTIFACTS_ROOT / "snapshots"
ENV_ARTIFACTS_ROOT = "CSTREE_ARTIFACTS_ROOT"
ENV_DATA_PLATFORM_ROOT = "DATA_PLATFORM_ROOT"
ENV_HK_DATA_PLATFORM_ROOT = "HK_DATA_PLATFORM_ROOT"
ENV_METADATA_DB_PATH = "CSTREE_METADATA_DB_PATH"
ENV_WAREHOUSE_DB_PATH = "CSTREE_WAREHOUSE_DB_PATH"
DATA_PLATFORM_PATH_PREFIXES = {
    ("artifacts", "assets"),
    ("artifacts", "metadata"),
    ("artifacts", "standardized"),
}


def default_path_text(path: Path) -> str:
    return path.as_posix()


def resolve_repo_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def _normalize_path_text(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_env_path(env_name: str) -> str | None:
    return _normalize_path_text(os.getenv(env_name))


def resolve_artifacts_root(path_text: str | Path | None = None) -> Path:
    configured = (
        _normalize_path_text(path_text)
        or _resolve_env_path(ENV_DATA_PLATFORM_ROOT)
        or _resolve_env_path(ENV_ARTIFACTS_ROOT)
    )
    return resolve_repo_path(configured or ARTIFACTS_ROOT)


def resolve_hk_data_platform_root(path_text: str | Path | None = None) -> Path | None:
    configured = (
        _normalize_path_text(path_text)
        or _resolve_env_path(ENV_DATA_PLATFORM_ROOT)
        or _resolve_env_path(ENV_HK_DATA_PLATFORM_ROOT)
    )
    return resolve_repo_path(configured) if configured is not None else None


def _data_platform_relative_path(path: Path) -> Path | None:
    if path.is_absolute():
        return None
    parts = path.parts
    if len(parts) < 2:
        return None
    if (parts[0], parts[1]) not in DATA_PLATFORM_PATH_PREFIXES:
        return None
    return Path(*parts[1:])


def configured_data_input_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.absolute()
    data_root = resolve_hk_data_platform_root()
    relative = _data_platform_relative_path(path)
    if data_root is not None and relative is not None:
        return (data_root / relative).absolute()
    return (Path.cwd() / path).absolute()


def resolve_data_input_path(path_text: str | Path) -> Path:
    return configured_data_input_path(path_text).resolve()


def resolve_configured_artifacts_root(
    config: Mapping[str, object] | None,
    *,
    override: str | Path | None = None,
) -> Path:
    if override is not None:
        return resolve_artifacts_root(override)
    configured_env = _resolve_env_path(ENV_DATA_PLATFORM_ROOT) or _resolve_env_path(
        ENV_ARTIFACTS_ROOT
    )
    if configured_env is not None:
        return resolve_artifacts_root(configured_env)
    cfg = config if isinstance(config, Mapping) else {}
    paths_cfg = cfg.get("paths")
    if isinstance(paths_cfg, Mapping):
        return resolve_artifacts_root(paths_cfg.get("artifacts_root"))
    return resolve_artifacts_root()


def cache_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "cache"


def assets_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "assets"


def metadata_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "metadata"


def standardized_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "standardized"


def runs_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "runs"


def live_runs_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "live_runs"


def sweeps_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "sweeps"


def snapshots_dir_for(artifacts_root: str | Path | None = None) -> Path:
    return resolve_artifacts_root(artifacts_root) / "snapshots"


def resolve_metadata_db_path(
    path_text: str | Path | None = None,
    *,
    artifacts_root: str | Path | None = None,
) -> Path:
    configured = _normalize_path_text(path_text) or _resolve_env_path(ENV_METADATA_DB_PATH)
    if configured is not None:
        return resolve_repo_path(configured)
    return metadata_dir_for(artifacts_root) / "catalog.sqlite"


def resolve_warehouse_db_path(
    path_text: str | Path | None = None,
    *,
    artifacts_root: str | Path | None = None,
) -> Path:
    configured = _normalize_path_text(path_text) or _resolve_env_path(ENV_WAREHOUSE_DB_PATH)
    if configured is not None:
        return resolve_repo_path(configured)
    return metadata_dir_for(artifacts_root) / "warehouse.duckdb"
