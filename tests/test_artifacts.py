from pathlib import Path

import pytest

from market_data_platform.artifacts import (
    resolve_artifacts_root,
    resolve_configured_artifacts_root,
    resolve_data_input_path,
    resolve_hk_data_platform_root,
    resolve_metadata_db_path,
    resolve_repo_path,
    resolve_warehouse_db_path,
)


def test_resolve_repo_path_handles_relative_and_absolute_inputs(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)

    relative = resolve_repo_path("artifacts/runs")
    assert relative == (repo_root / "artifacts" / "runs").resolve()

    absolute_input = repo_root / "artifacts" / "cache"
    absolute = resolve_repo_path(absolute_input)
    assert absolute == absolute_input.resolve()


def test_resolve_artifacts_root_uses_cstree_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSTREE_ARTIFACTS_ROOT", "legacy-artifacts")

    assert resolve_artifacts_root() == (tmp_path / "legacy-artifacts").resolve()


def test_resolve_configured_artifacts_root_prefers_cstree_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSTREE_ARTIFACTS_ROOT", "preferred-artifacts")

    resolved = resolve_configured_artifacts_root(
        {"paths": {"artifacts_root": "config-artifacts"}}
    )

    assert resolved == (tmp_path / "preferred-artifacts").resolve()


def test_resolve_hk_data_platform_root_uses_neutral_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HK_DATA_PLATFORM_ROOT", "hk-data-platform-artifacts")

    assert resolve_hk_data_platform_root() == (
        tmp_path / "hk-data-platform-artifacts"
    ).resolve()


def test_resolve_data_input_path_rebases_shared_data_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HK_DATA_PLATFORM_ROOT", "hk-data-platform-artifacts")

    assert resolve_data_input_path("artifacts/assets/universe/demo.csv") == (
        tmp_path / "hk-data-platform-artifacts" / "assets" / "universe" / "demo.csv"
    ).resolve()
    assert resolve_data_input_path("artifacts/metadata/current_assets/hk_current.json") == (
        tmp_path
        / "hk-data-platform-artifacts"
        / "metadata"
        / "current_assets"
        / "hk_current.json"
    ).resolve()
    assert resolve_data_input_path("artifacts/runs/demo") == (
        tmp_path / "artifacts" / "runs" / "demo"
    ).resolve()


def test_hk_data_platform_root_does_not_override_run_artifacts_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HK_DATA_PLATFORM_ROOT", "hk-data-platform-artifacts")

    assert resolve_artifacts_root() == (tmp_path / "artifacts").resolve()


@pytest.mark.parametrize(
    ("resolver", "env_name", "expected"),
    [
        (
            resolve_artifacts_root,
            "CSTREE_ARTIFACTS_ROOT",
            Path("preferred-artifacts"),
        ),
        (
            resolve_metadata_db_path,
            "CSTREE_METADATA_DB_PATH",
            Path("preferred") / "catalog.sqlite",
        ),
        (
            resolve_warehouse_db_path,
            "CSTREE_WAREHOUSE_DB_PATH",
            Path("preferred") / "warehouse.duckdb",
        ),
    ],
)
def test_cstree_env_resolvers_use_cstree_env(
    resolver,
    env_name,
    expected,
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(env_name, expected.as_posix())

    assert resolver() == (tmp_path / expected).resolve()
