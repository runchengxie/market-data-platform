from __future__ import annotations

import importlib
import sys

import pytest

from market_data_platform.cli import main
from market_data_platform.contract import build_current_contract, write_current_contract
from market_data_platform.paths import (
    candidate_asset_paths,
    current_contract_path,
    dataset_registry_path,
    resolve_artifacts_root,
)
from market_data_platform.registry import (
    build_combined_dataset_registry_rows,
    build_dataset_registry_rows,
    render_combined_dataset_registry_csv,
    render_dataset_registry_csv,
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


def test_cn_paths_and_contract_use_market_specific_layout(tmp_path):
    root = tmp_path / "market-data"

    assert (
        current_contract_path(root, market="cn")
        == root.resolve() / "metadata" / "current_assets" / "cn_current.json"
    )

    assets = candidate_asset_paths(root, market="cn")
    assert (
        assets["daily_clean"]
        == root.resolve() / "assets" / "rqdata" / "cn" / "daily" / "cn_all_daily_clean_latest"
    )
    assert (
        assets["instruments"]
        == root.resolve()
        / "assets"
        / "rqdata"
        / "cn"
        / "instruments"
        / "cn_all_instruments_latest.parquet"
    )
    assert assets["st_flags"].name == "cn_st_flags_latest"
    assert assets["limit_status"].name == "cn_limit_status_latest"

    contract = build_current_contract(root, market="cn", target_date="20260522")
    assert contract["contract"]["name"] == "cn_current"
    assert contract["contract"]["market"] == "cn"
    assert contract["contract"]["contract_path"].endswith("metadata/current_assets/cn_current.json")
    assert "daily_clean" in contract["assets"]


def test_tushare_cn_paths_and_registry_source_are_provider_specific(tmp_path):
    root = tmp_path / "market-data"
    assets = candidate_asset_paths(root, market="cn", provider="tushare")
    assert (
        assets["daily"]
        == root.resolve() / "assets" / "tushare" / "cn" / "daily" / "cn_all_daily_latest"
    )
    assert assets["trade_cal"].name == "cn_trade_cal_latest.parquet"
    assert "daily_basic" in assets

    snapshot = root / "assets" / "tushare" / "cn" / "daily" / "cn_all_20260522_daily"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.yml").write_text(
        "\n".join(
            [
                "dataset: daily",
                "provider: tushare",
                "status: completed",
                "query:",
                "  start_date: '20260521'",
                "  end_date: '20260522'",
                "totals:",
                "  rows: 10",
                "  symbols: 5",
            ]
        ),
        encoding="utf-8",
    )
    assets["daily"].parent.mkdir(parents=True, exist_ok=True)
    assets["daily"].symlink_to(snapshot.name)

    contract = build_current_contract(
        root,
        market="cn",
        provider="tushare",
        target_date="20260522",
    )
    daily = next(
        row
        for row in build_dataset_registry_rows(contract)
        if row["dataset_name"] == "cn_daily"
    )
    rows_by_name = {row["dataset_name"]: row for row in build_dataset_registry_rows(contract)}
    assert contract["contract"]["provider"] == "tushare"
    assert daily["source"] == "tushare"
    assert rows_by_name["cn_instruments"]["source"] == "tushare"
    assert rows_by_name["cn_limit_status"]["source"] == "tushare"


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


def test_cn_dataset_registry_uses_contract_market(tmp_path):
    root = tmp_path / "market-data"
    snapshot = root / "assets" / "rqdata" / "cn" / "daily" / "cn_all_20260522_daily"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.yml").write_text(
        "\n".join(
            [
                "dataset: daily",
                "status: completed",
                "query:",
                "  start_date: '20240101'",
                "  end_date: '20260522'",
                "totals:",
                "  rows: 12",
                "  symbols_written: 3",
            ]
        ),
        encoding="utf-8",
    )
    alias = candidate_asset_paths(root, market="cn")["daily"]
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(snapshot.name)

    contract = build_current_contract(root, market="cn", target_date="20260522")
    rows = build_dataset_registry_rows(contract)
    csv_text = render_dataset_registry_csv(contract)

    daily = next(row for row in rows if row["dataset_name"] == "cn_daily")
    assert daily["market"] == "cn"
    assert daily["version"] == "20260522"
    assert daily["records"] == "12"
    assert "cn_current_contract" in csv_text


def test_combined_dataset_registry_includes_hk_and_cn_contracts(tmp_path):
    root = tmp_path / "market-data"
    hk_contract = build_current_contract(root, market="hk", target_date="20260522")
    cn_contract = build_current_contract(root, market="cn", target_date="20260522")

    rows = build_combined_dataset_registry_rows([hk_contract, cn_contract])
    csv_text = render_combined_dataset_registry_csv([hk_contract, cn_contract])

    dataset_names = {row["dataset_name"] for row in rows}
    assert "hk_current_contract" in dataset_names
    assert "cn_current_contract" in dataset_names
    assert "hk_daily" in dataset_names
    assert "cn_daily" in dataset_names
    assert "# Dataset Registry for current HK/CN research data assets." in csv_text


def test_hk_data_platform_imports_remain_compatible(tmp_path):
    sys.modules.pop("hk_data_platform.paths", None)
    sys.modules.pop("hk_data_platform", None)
    with pytest.warns(DeprecationWarning, match="hk_data_platform"):
        legacy_paths = importlib.import_module("hk_data_platform.paths")

    root = tmp_path / "market-data"

    assert legacy_paths.current_contract_path(root, market="cn") == current_contract_path(
        root,
        market="cn",
    )


def test_cli_registry_build_combines_existing_market_contracts(tmp_path):
    root = tmp_path / "market-data"
    hk_contract = build_current_contract(root, market="hk", target_date="20260522")
    cn_contract = build_current_contract(root, market="cn", target_date="20260522")
    write_current_contract(current_contract_path(root, market="hk"), hk_contract)
    write_current_contract(current_contract_path(root, market="cn"), cn_contract)

    assert main(["registry", "build", "--artifacts-root", str(root)]) == 0

    registry_text = dataset_registry_path(root).read_text(encoding="utf-8")
    assert "hk_current_contract" in registry_text
    assert "cn_current_contract" in registry_text
