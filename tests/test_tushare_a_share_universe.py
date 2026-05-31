from __future__ import annotations

import pytest
import yaml

from market_data_platform.cli import build_parser
from market_data_platform.providers.tushare_a_share_universe import (
    build_a_share_universe,
    build_a_share_universe_frame,
    validate_a_share_universe,
)


def _write_daily_clean_symbol(pd, root, symbol, amounts):
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103", "20200104", "20200105"],
            "symbol": [symbol] * 5,
            "amount": amounts,
        }
    ).to_parquet(data_dir / f"{symbol}.parquet", index=False)


def _write_daily_clean_manifest(root):
    (root / "manifest.yml").write_text(
        "\n".join(
            [
                "schema_version: tushare.a_share.daily_clean.v1",
                "dataset: daily_clean",
                "market: a_share",
                "provider: tushare",
                "status: completed",
                "query:",
                "  start_date: '20200101'",
                "  end_date: '20200105'",
            ]
        ),
        encoding="utf-8",
    )


def test_build_a_share_universe_frame_uses_lagged_amount_liquidity(tmp_path):
    pd = pytest.importorskip("pandas")
    asset_dir = tmp_path / "daily_clean"
    _write_daily_clean_symbol(pd, asset_dir, "600519.SH", [10.0, 20.0, 30.0, 40.0, 50.0])
    _write_daily_clean_symbol(pd, asset_dir, "000001.SZ", [50.0, 40.0, 30.0, 20.0, 10.0])
    _write_daily_clean_manifest(asset_dir)

    universe, summary = build_a_share_universe_frame(
        asset_dir,
        start_date="20200101",
        end_date="20200131",
        rebalance_frequency="M",
        lookback_days=2,
        min_window_days=2,
    )

    assert summary["latest_symbols"] == 2
    assert summary["rebalance_dates_requested"] == 1
    assert summary["rebalance_dates"] == 1
    assert universe.columns.tolist() == ["trade_date", "symbol", "liq_metric", "selected"]
    assert universe["trade_date"].tolist() == ["20200105", "20200105"]
    assert universe["symbol"].tolist() == ["600519.SH", "000001.SZ"]
    assert universe["liq_metric"].tolist() == [35.0, 25.0]


def test_build_and_validate_a_share_universe_writes_canonical_outputs(tmp_path):
    pd = pytest.importorskip("pandas")
    asset_dir = tmp_path / "daily_clean"
    _write_daily_clean_symbol(pd, asset_dir, "600519.SH", [10.0, 20.0, 30.0, 40.0, 50.0])
    _write_daily_clean_symbol(pd, asset_dir, "000001.SZ", [50.0, 40.0, 30.0, 20.0, 10.0])
    _write_daily_clean_manifest(asset_dir)

    summary = build_a_share_universe(
        artifacts_root=tmp_path,
        daily_clean_dir=asset_dir,
        start_date="20200101",
        end_date="20200131",
        lookback_days=2,
        min_window_days=2,
        min_rows=2,
        min_symbols=2,
    )

    universe_dir = tmp_path / "assets" / "universe"
    by_date = universe_dir / "a_share_all_full_by_date.csv"
    symbols = universe_dir / "a_share_all_full_symbols.txt"
    meta = universe_dir / "a_share_all_full_by_date.meta.yml"
    assert summary["status"] == "completed"
    assert symbols.read_text(encoding="utf-8") == "600519.SH\n000001.SZ\n"
    meta_payload = yaml.safe_load(meta.read_text(encoding="utf-8"))
    assert meta_payload["build"]["last_rebalance_date"] == "20200105"
    manifest = yaml.safe_load(
        (universe_dir / "a_share_all_full_by_date.manifest.yml").read_text(encoding="utf-8")
    )
    assert manifest["dataset"] == "universe_by_date"
    assert manifest["query"]["end_date"] == "20200105"

    validation = validate_a_share_universe(
        by_date_file=by_date,
        latest_symbols_file=symbols,
        meta_file=meta,
        expected_as_of="20200105",
        min_rows=2,
        min_symbols=2,
    )
    assert validation["status"] == "passed"
    assert validation["errors"] == []


def test_build_a_share_universe_refuses_to_overwrite_outputs_without_force(tmp_path):
    pd = pytest.importorskip("pandas")
    asset_dir = tmp_path / "daily_clean"
    _write_daily_clean_symbol(pd, asset_dir, "600519.SH", [10.0, 20.0, 30.0, 40.0, 50.0])
    _write_daily_clean_manifest(asset_dir)
    universe_dir = tmp_path / "assets" / "universe"
    universe_dir.mkdir(parents=True)
    (universe_dir / "a_share_all_full_by_date.csv").write_text("old\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        build_a_share_universe(
            artifacts_root=tmp_path,
            daily_clean_dir=asset_dir,
            start_date="20200101",
            end_date="20200131",
            lookback_days=2,
            min_window_days=2,
        )


def test_a_share_universe_commands_are_exposed():
    parser = build_parser()

    build = parser.parse_args(
        [
            "tushare",
            "build-a-share-universe",
            "--start-date",
            "20200101",
            "--end-date",
            "20200131",
        ]
    )
    validate = parser.parse_args(
        [
            "tushare",
            "validate-a-share-universe",
            "--by-date-file",
            "universe.csv",
            "--latest-symbols-file",
            "symbols.txt",
            "--meta-file",
            "meta.yml",
        ]
    )

    assert build.tushare_command == "build-a-share-universe"
    assert validate.tushare_command == "validate-a-share-universe"
