from __future__ import annotations

import pandas as pd
import pytest

from market_data_platform.hk_depth.cli import build_parser, main
from market_data_platform.hk_depth.storage import atomic_write_parquet


@pytest.mark.parametrize(
    "command",
    [
        "probe",
        "download",
        "health",
        "aggregate-daily",
        "recompress-raw",
        "compact-raw",
        "emit-asset",
        "package-assets",
        "release-assets",
        "quota",
        "reconcile-daily",
    ],
)
def test_hk_depth_cli_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])

    assert exc.value.code == 0
    assert command in capsys.readouterr().out


def test_hk_depth_parser_exposes_migrated_commands() -> None:
    parser = build_parser()
    subparser_action = next(action for action in parser._actions if action.dest == "command")

    assert {
        "download",
        "health",
        "aggregate-daily",
        "reconcile-daily",
        "emit-asset",
        "package-assets",
    }.issubset(subparser_action.choices)


def test_hk_depth_offline_pipeline_uses_platform_package(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    daily_path = tmp_path / "daily" / "data.parquet"
    raw_asset = tmp_path / "asset_raw"
    daily_asset = tmp_path / "asset_daily"
    health_units = tmp_path / "reports" / "health_units.csv"
    daily_ref = tmp_path / "daily_ref"
    reconcile_report = tmp_path / "reports" / "reconcile.json"

    assert (
        main(
            [
                "download",
                "--symbols",
                "00001.XHKG",
                "--start-date",
                "20250303",
                "--end-date",
                "20250303",
                "--out",
                str(raw_root),
                "--fields",
                "last volume total_turnover a1 a1_v b1 b1_v",
                "--fake-provider",
            ]
        )
        == 0
    )
    assert main(["health", "--input", str(raw_root), "--out-units", str(health_units)]) == 0
    assert main(["aggregate-daily", "--input", str(raw_root), "--output", str(daily_path)]) == 0
    assert (
        main(["emit-asset", "--kind", "raw", "--source", str(raw_root), "--output", str(raw_asset)])
        == 0
    )
    assert (
        main(
            [
                "emit-asset",
                "--kind",
                "daily",
                "--source",
                str(daily_path),
                "--output",
                str(daily_asset),
            ]
        )
        == 0
    )

    atomic_write_parquet(
        pd.DataFrame(
            {
                "trade_date": ["20250303"],
                "symbol": ["00001.HK"],
                "open": [100.05],
                "high": [100.20],
                "low": [100.05],
                "close": [100.20],
                "volume": [10000.0],
                "total_turnover": [1001500.0],
            }
        ),
        daily_ref / "data" / "00001.HK.parquet",
    )
    assert (
        main(
            [
                "reconcile-daily",
                "--tick-input",
                str(raw_root),
                "--daily-asset-dir",
                str(daily_ref),
                "--out",
                str(reconcile_report),
                "--fail-on-severity",
                "none",
            ]
        )
        == 0
    )

    assert health_units.exists()
    assert daily_path.exists()
    assert (raw_asset / "manifest.yml").exists()
    assert (daily_asset / "manifest.yml").exists()
    assert reconcile_report.exists()
