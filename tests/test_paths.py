from __future__ import annotations

from hk_data_platform.contract import build_current_contract
from hk_data_platform.paths import (
    candidate_asset_paths,
    current_contract_path,
    dataset_registry_path,
    resolve_artifacts_root,
)


def test_shared_paths_resolve_from_explicit_root(tmp_path):
    root = tmp_path / "hk-data"

    assert resolve_artifacts_root(root) == root.resolve()
    assert (
        current_contract_path(root)
        == root.resolve() / "metadata" / "current_assets" / "hk_current.json"
    )
    assert dataset_registry_path(root) == root.resolve() / "metadata" / "dataset_registry.csv"

    assets = candidate_asset_paths(root)
    assert (
        assets["daily_clean"]
        == root.resolve() / "assets" / "rqdata" / "hk" / "daily" / "hk_all_daily_clean_latest"
    )
    assert (
        assets["tick_depth_daily"]
        == root.resolve()
        / "assets"
        / "rqdata"
        / "hk"
        / "tick_depth_daily"
        / "hk_tick_depth_daily_latest"
    )


def test_build_current_contract_reads_tick_depth_manifest(tmp_path):
    root = tmp_path / "hk-data"
    snapshot = (
        root
        / "assets"
        / "rqdata"
        / "hk"
        / "tick_depth_daily"
        / "hk_tick_depth_daily_core_20250401_20260409"
    )
    (snapshot / "data").mkdir(parents=True)
    (snapshot / "data" / "data.parquet").write_text("demo", encoding="utf-8")
    (snapshot / "manifest.yml").write_text(
        "\n".join(
            [
                "schema_version: tick_depth_daily.v1",
                "row_count: 10",
                "symbol_count: 2",
                "date_range:",
                "  start: '20250401'",
                "  end: '20260409'",
            ]
        ),
        encoding="utf-8",
    )
    alias = candidate_asset_paths(root)["tick_depth_daily"]
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(snapshot.name)

    contract = build_current_contract(root, target_date="20260409")

    entry = contract["assets"]["tick_depth_daily"]
    assert entry["exists"] is True
    assert entry["as_of"] == "20260409"
    assert entry["manifest"]["dataset"] == "tick_depth_daily"
    assert entry["manifest"]["totals"]["rows"] == 10
