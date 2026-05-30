from __future__ import annotations

import yaml

from market_data_platform.providers.tushare_a_share_clean import (
    build_a_share_daily_clean,
    validate_a_share_daily_clean,
)


def _write_part(frame, root, trade_date):
    path = root / "data" / f"trade_date={trade_date}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def test_build_a_share_daily_clean_merges_adjustment_valuation_and_limit_status(tmp_path):
    pd = __import__("pandas")
    daily_dir = tmp_path / "raw_daily"
    adj_dir = tmp_path / "adj_factor"
    daily_basic_dir = tmp_path / "daily_basic"
    limit_dir = tmp_path / "limit_status"
    instruments = tmp_path / "instruments.parquet"
    out_dir = tmp_path / "daily_clean"

    _write_part(
        pd.DataFrame(
            {
                "ts_code": ["600519.SH", "000001.SZ"],
                "trade_date": ["20260522", "20260522"],
                "open": [100.0, 10.0],
                "high": [110.0, 10.0],
                "low": [99.0, 9.5],
                "close": [110.0, 9.5],
                "pre_close": [100.0, 10.0],
                "vol": [1000.0, 0.0],
                "amount": [110000.0, 0.0],
            }
        ),
        daily_dir,
        "20260522",
    )
    _write_part(
        pd.DataFrame(
            {
                "ts_code": ["600519.SH", "000001.SZ"],
                "trade_date": ["20260522", "20260522"],
                "adj_factor": [2.0, 1.0],
            }
        ),
        adj_dir,
        "20260522",
    )
    _write_part(
        pd.DataFrame(
            {
                "ts_code": ["600519.SH", "000001.SZ"],
                "trade_date": ["20260522", "20260522"],
                "turnover_rate": [1.2, 0.5],
                "pe_ttm": [25.0, 6.0],
                "pb": [9.0, 0.8],
                "total_mv": [2000000.0, 300000.0],
            }
        ),
        daily_basic_dir,
        "20260522",
    )
    _write_part(
        pd.DataFrame(
            {
                "ts_code": ["600519.SH", "000001.SZ"],
                "trade_date": ["20260522", "20260522"],
                "up_limit": [110.0, 11.0],
                "down_limit": [90.0, 9.5],
            }
        ),
        limit_dir,
        "20260522",
    )
    pd.DataFrame(
        {
            "ts_code": ["600519.SH", "000001.SZ"],
            "name": ["贵州茅台", "平安银行"],
            "list_date": ["20010827", "19910403"],
        }
    ).to_parquet(instruments, index=False)

    manifest = build_a_share_daily_clean(
        daily_dir=daily_dir,
        adj_factor_dir=adj_dir,
        daily_basic_dir=daily_basic_dir,
        limit_status_dir=limit_dir,
        instruments_file=instruments,
        out_dir=out_dir,
        min_rows=2,
        min_symbols=2,
    )

    assert manifest["dataset"] == "daily_clean"
    assert manifest["quality"]["limit_up_rows"] == 1
    assert manifest["quality"]["limit_down_rows"] == 1
    assert manifest["quality"]["suspended_rows"] == 1
    output = pd.read_parquet(out_dir / "data" / "600519.SH.parquet")
    assert output.loc[0, "tr_close"] == 110.0
    assert output.loc[0, "pe_ttm"] == 25.0
    assert bool(output.loc[0, "is_limit_up"]) is True
    assert output.loc[0, "board"] == "MAIN"
    manifest_payload = yaml.safe_load((out_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest_payload["schema_version"] == "tushare.a_share.daily_clean.v1"


def test_validate_a_share_daily_clean_reports_required_overlay_columns(tmp_path):
    pd = __import__("pandas")
    root = tmp_path / "daily_clean"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "symbol": ["600519.SH"],
            "trade_date": ["20260522"],
            "close": [100.0],
            "tr_close": [100.0],
            "is_st": [False],
            "is_suspended": [False],
            "is_limit_up": [False],
            "is_limit_down": [False],
        }
    ).to_parquet(data_dir / "600519.SH.parquet", index=False)

    summary = validate_a_share_daily_clean(
        daily_clean_dir=root,
        min_rows=1,
        min_symbols=1,
        require_valuation=True,
        require_limit_status=True,
    )

    assert summary["status"] == "failed"
    assert "pe_ttm" in summary["errors"][0]
    assert "up_limit" in summary["errors"][0]
