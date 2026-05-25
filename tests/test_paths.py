from __future__ import annotations

from hk_data_platform.contract import build_current_contract
from hk_data_platform.paths import (
    candidate_asset_paths,
    current_contract_path,
    dataset_registry_path,
    resolve_artifacts_root,
)
from hk_data_platform.registry import build_dataset_registry_rows, render_dataset_registry_csv


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
    assert (
        assets["financial_details"]
        == root.resolve()
        / "assets"
        / "rqdata"
        / "hk"
        / "financial_details"
        / "hk_financial_details_latest"
    )
    assert len(assets) == 22


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


def test_dataset_registry_is_derived_from_current_contract(tmp_path):
    root = tmp_path / "hk-data"
    snapshot = root / "assets" / "rqdata" / "hk" / "daily" / "hk_all_20260409_daily"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.yml").write_text(
        "\n".join(
            [
                "dataset: daily",
                "status: completed",
                "query:",
                "  start_date: '20240101'",
                "  end_date: '20260409'",
                "totals:",
                "  rows: 12",
                "  symbols_written: 3",
            ]
        ),
        encoding="utf-8",
    )
    alias = candidate_asset_paths(root)["daily"]
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(snapshot.name)

    contract = build_current_contract(root, target_date="20260409")
    rows = build_dataset_registry_rows(contract)
    csv_text = render_dataset_registry_csv(contract)

    daily = next(row for row in rows if row["dataset_name"] == "hk_daily")
    assert daily["version"] == "20260409"
    assert daily["records"] == "12"
    assert daily["symbols"] == "3"
    assert daily["date_range"] == "2024-01-01 to 2026-04-09"
    assert "hk_current_contract" in csv_text


def test_current_contract_uses_query_date_as_as_of(tmp_path):
    root = tmp_path / "hk-data"
    snapshot = (
        root
        / "assets"
        / "rqdata"
        / "hk"
        / "financial_details"
        / "hk_financial_details_hk_all3195_superset_2000_2026_20260522"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.yml").write_text(
        "\n".join(
            [
                "dataset: financial_details",
                "status: completed",
                "query:",
                "  start_quarter: 2000q1",
                "  end_quarter: 2026q1",
                "  date: '20260522'",
                "totals:",
                "  rows: 7",
                "  symbols_written: 2",
            ]
        ),
        encoding="utf-8",
    )
    alias = candidate_asset_paths(root)["financial_details"]
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(snapshot.name)

    contract = build_current_contract(root, target_date="20260522")
    rows = build_dataset_registry_rows(contract)

    entry = contract["assets"]["financial_details"]
    financial_details = next(row for row in rows if row["dataset_name"] == "hk_financial_details")
    assert entry["as_of"] == "20260522"
    assert financial_details["version"] == "20260522"
    assert financial_details["date_range"] == "as of 2026-05-22"
