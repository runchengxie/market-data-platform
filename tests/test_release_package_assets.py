from __future__ import annotations

from pathlib import Path

import pytest

from market_data_platform.release_tools import package_assets


def test_release_presets_load_from_yaml_configs():
    presets = package_assets.load_release_presets()

    assert sorted(presets) == ["hk_connect", "hk_current", "hk_etf", "hk_full"]
    assert presets["hk_full"]["daily_snapshot"] == "hk_all_2000_20260327_daily_final_latest"
    assert presets["hk_etf"]["default_parts"] == ("daily", "instruments")
    assert "announcement" not in presets["hk_current"]["default_parts"]


def test_release_preset_loader_rejects_unknown_part(tmp_path: Path):
    preset_path = tmp_path / "bad.yml"
    preset_path.write_text(
        "\n".join(
            [
                "default_parts:",
                "  - daily",
                "  - not_a_part",
                "daily_snapshot: hk_daily_latest",
                "instruments_file: hk_instruments_latest.parquet",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="unsupported default_parts"):
        package_assets.load_release_presets(tmp_path)


def test_build_part_specs_groups_reference_and_universe_meta(tmp_path: Path):
    daily_dir = tmp_path / "hk_all_daily"
    instruments_path = tmp_path / "hk_all_instruments.parquet"
    ex_factors_dir = tmp_path / "hk_all_ex_factors"
    dividends_dir = tmp_path / "hk_all_dividends"
    shares_dir = tmp_path / "hk_all_shares"
    universe_by_date_path = tmp_path / "hk_all_by_date.csv"
    universe_symbols_path = tmp_path / "hk_all_symbols.txt"
    universe_meta_path = tmp_path / "hk_all_by_date.meta.yml"
    for path in (daily_dir, ex_factors_dir, dividends_dir, shares_dir):
        path.mkdir()
    for path in (
        instruments_path,
        universe_by_date_path,
        universe_symbols_path,
        universe_meta_path,
    ):
        path.write_text("demo\n", encoding="utf-8")

    specs = package_assets._build_part_specs(
        {
            "daily_dir": daily_dir,
            "intraday_dir": None,
            "etf_daily_dir": None,
            "etf_instruments_path": None,
            "valuation_dir": None,
            "instruments_path": instruments_path,
            "pit_dir": None,
            "ex_factors_dir": ex_factors_dir,
            "dividends_dir": dividends_dir,
            "shares_dir": shares_dir,
            "exchange_rate_dir": None,
            "southbound_dir": None,
            "financial_details_dir": None,
            "announcement_dir": None,
            "industry_changes_dir": None,
            "universe_by_date_path": universe_by_date_path,
            "universe_symbols_path": universe_symbols_path,
            "universe_meta_path": universe_meta_path,
        }
    )

    assert [entry["label"] for entry in specs["reference"]["entries"]] == [
        "ex_factors",
        "dividends",
        "shares",
    ]
    assert specs["reference"]["summary"] == {
        "ex_factors_snapshot": "hk_all_ex_factors",
        "dividends_snapshot": "hk_all_dividends",
        "shares_snapshot": "hk_all_shares",
    }
    assert specs["universe"]["summary"]["meta"] == "hk_all_by_date.meta.yml"
    assert specs["universe"]["latest_links"][-1] == {
        "link": "universe/latest_meta.yml",
        "target": "universe/hk_all_by_date.meta.yml",
    }
