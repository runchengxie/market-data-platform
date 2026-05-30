import pandas as pd

from market_data_platform import data_providers


def test_fetch_daily_reads_tushare_local_asset_without_online_provider(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = tmp_path / "tushare_daily_clean"
    (asset_dir / "data").mkdir(parents=True, exist_ok=True)
    symbol = "600519.SH"

    pd.DataFrame(
        {
            "trade_date": ["20200102", "20200103"],
            "ts_code": [symbol, symbol],
            "close": [100.0, 101.0],
            "vol": [2000.0, 2100.0],
            "amount": [30000.0, 33000.0],
        }
    ).to_parquet(asset_dir / "data" / f"{symbol}.parquet")

    def fake_fetch(*args, **kwargs):
        raise AssertionError("TuShare online provider should not be called in platform asset mode")

    monkeypatch.setattr(data_providers, "_fetch_daily_rqdata", fake_fetch)

    result = data_providers.fetch_daily(
        "a_share",
        symbol,
        "20200102",
        "20200103",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "tushare",
            "source_mode": "platform_assets",
            "tushare": {"daily_asset_dir": str(asset_dir)},
        },
    )

    assert result["symbol"].tolist() == [symbol, symbol]
    assert result["close"].tolist() == [100.0, 101.0]


def test_load_basic_reads_tushare_local_instruments_file(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    instruments_file = tmp_path / "a_share_tushare_instruments.parquet"

    pd.DataFrame(
        {
            "ts_code": ["600519.SH", "000858.SZ"],
            "name": ["贵州茅台", "五粮液"],
            "list_date": ["20010827", "19980427"],
            "exchange": ["SSE", "SZSE"],
        }
    ).to_parquet(instruments_file)

    result = data_providers.load_basic(
        "a_share",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "tushare",
            "source_mode": "platform_assets",
            "tushare": {"instruments_file": str(instruments_file)},
        },
        symbols=["600519.SH"],
    )

    assert result["symbol"].tolist() == ["600519.SH"]
    assert result["name"].tolist() == ["贵州茅台"]
    assert result["list_date"].tolist() == ["20010827"]
