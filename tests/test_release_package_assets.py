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
