import pandas as pd

from cstree import data_providers


def _daily_frame(symbol: str, start: str, end: str, *, close_offset: float = 0.0) -> pd.DataFrame:
    dates = pd.date_range(pd.to_datetime(start), pd.to_datetime(end), freq="D")
    rows = []
    for idx, trade_date in enumerate(dates):
        rows.append(
            {
                "trade_date": trade_date.strftime("%Y%m%d"),
                "symbol": symbol,
                "close": close_offset + float(idx + 1),
                "vol": 1000.0 + idx,
                "amount": 10000.0 + idx,
            }
        )
    return pd.DataFrame(rows)


def test_fetch_daily_symbol_cache_refresh_window_merges_monotonic(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    symbol = "AAA"
    cache_file = cache_dir / "hk_rqdata_daily_AAA.parquet"

    cached = _daily_frame(symbol, "20200101", "20200105", close_offset=0.0)
    cached.to_parquet(cache_file)

    fetch_ranges = []

    def fake_fetch(provider, market, symbol_value, start_date, end_date, client, data_cfg):
        fetch_ranges.append((start_date, end_date))
        return _daily_frame(symbol_value, start_date, end_date, close_offset=100.0)

    monkeypatch.setattr(data_providers, "_fetch_daily_from_provider", fake_fetch)

    data_cfg = {
        "provider": "rqdata",
        "cache_mode": "symbol",
        "cache_refresh_days": 2,
        "cache_refresh_on_hit": False,
    }
    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200102",
        "20200107",
        cache_dir,
        client=None,
        data_cfg=data_cfg,
    )

    assert fetch_ranges == [("20200104", "20200107")]
    assert result["trade_date"].tolist() == [
        "20200102",
        "20200103",
        "20200104",
        "20200105",
        "20200106",
        "20200107",
    ]
    assert result["trade_date"].is_monotonic_increasing
    assert result["trade_date"].nunique() == len(result)

    merged = pd.read_parquet(cache_file).sort_values("trade_date").reset_index(drop=True)
    refreshed_close = float(merged.loc[merged["trade_date"] == "20200104", "close"].iloc[0])
    assert refreshed_close > 100.0


def test_fetch_daily_symbol_cache_refresh_on_hit_triggers_tail_refresh(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    symbol = "AAA"
    cache_file = cache_dir / "hk_rqdata_daily_AAA.parquet"

    cached = _daily_frame(symbol, "20200101", "20200105", close_offset=0.0)
    cached.to_parquet(cache_file)

    fetch_ranges = []

    def fake_fetch(provider, market, symbol_value, start_date, end_date, client, data_cfg):
        fetch_ranges.append((start_date, end_date))
        return _daily_frame(symbol_value, start_date, end_date, close_offset=200.0)

    monkeypatch.setattr(data_providers, "_fetch_daily_from_provider", fake_fetch)

    data_cfg = {
        "provider": "rqdata",
        "cache_mode": "symbol",
        "cache_refresh_days": 2,
        "cache_refresh_on_hit": True,
    }
    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200102",
        "20200105",
        cache_dir,
        client=None,
        data_cfg=data_cfg,
    )

    assert fetch_ranges == [("20200104", "20200105")]
    assert result["trade_date"].tolist() == [
        "20200102",
        "20200103",
        "20200104",
        "20200105",
    ]


def test_fetch_daily_symbol_cache_skips_small_leading_calendar_gap(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    symbol = "AAA"
    cache_file = cache_dir / "hk_rqdata_daily_AAA.parquet"

    cached = _daily_frame(symbol, "20200102", "20200105", close_offset=0.0)
    cached.to_parquet(cache_file)

    fetch_ranges = []

    def fake_fetch(provider, market, symbol_value, start_date, end_date, client, data_cfg):
        fetch_ranges.append((start_date, end_date))
        return _daily_frame(symbol_value, start_date, end_date, close_offset=300.0)

    monkeypatch.setattr(data_providers, "_fetch_daily_from_provider", fake_fetch)

    data_cfg = {
        "provider": "rqdata",
        "cache_mode": "symbol",
        "cache_refresh_days": 0,
        "cache_refresh_on_hit": False,
    }
    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200101",
        "20200105",
        cache_dir,
        client=None,
        data_cfg=data_cfg,
    )

    assert fetch_ranges == []
    assert result["trade_date"].tolist() == [
        "20200102",
        "20200103",
        "20200104",
        "20200105",
    ]


def test_fetch_daily_symbol_cache_fetches_large_leading_gap(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    symbol = "AAA"
    cache_file = cache_dir / "hk_rqdata_daily_AAA.parquet"

    cached = _daily_frame(symbol, "20200110", "20200112", close_offset=0.0)
    cached.to_parquet(cache_file)

    fetch_ranges = []

    def fake_fetch(provider, market, symbol_value, start_date, end_date, client, data_cfg):
        fetch_ranges.append((start_date, end_date))
        return _daily_frame(symbol_value, start_date, end_date, close_offset=400.0)

    monkeypatch.setattr(data_providers, "_fetch_daily_from_provider", fake_fetch)

    data_cfg = {
        "provider": "rqdata",
        "cache_mode": "symbol",
        "cache_refresh_days": 0,
        "cache_refresh_on_hit": False,
    }
    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200101",
        "20200112",
        cache_dir,
        client=None,
        data_cfg=data_cfg,
    )

    assert fetch_ranges == [("20200101", "20200110")]
    assert result["trade_date"].tolist() == [
        "20200101",
        "20200102",
        "20200103",
        "20200104",
        "20200105",
        "20200106",
        "20200107",
        "20200108",
        "20200109",
        "20200110",
        "20200111",
        "20200112",
    ]


def test_fetch_daily_reads_from_local_asset_dir_without_remote_fetch(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = tmp_path / "daily_assets"
    data_dir = asset_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    symbol = "AAA"

    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103"],
            "symbol": [symbol, symbol, symbol],
            "close": [10.0, 11.0, 12.0],
            "volume": [100.0, 110.0, 120.0],
            "total_turnover": [1000.0, 1100.0, 1200.0],
        }
    ).to_parquet(data_dir / f"{symbol}.parquet")

    def fake_fetch(*args, **kwargs):
        raise AssertionError("remote provider should not be called when local asset is configured")

    monkeypatch.setattr(data_providers, "_fetch_daily_rqdata", fake_fetch)

    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200102",
        "20200103",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "symbol",
            "cache_refresh_days": 0,
            "cache_refresh_on_hit": False,
            "column_map": {
                "trade_date": "trade_date",
                "close": "close",
                "vol": "volume",
                "amount": "total_turnover",
            },
            "rqdata": {
                "daily_asset_dir": str(asset_dir),
            },
        },
    )

    assert result["trade_date"].tolist() == ["20200102", "20200103"]
    assert result["close"].tolist() == [11.0, 12.0]


def test_fetch_daily_local_asset_prefers_ts_code_over_order_book_id(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = tmp_path / "daily_assets"
    data_dir = asset_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    symbol = "00001.HK"

    pd.DataFrame(
        {
            "trade_date": ["20200102", "20200103"],
            "ts_code": [symbol, symbol],
            "order_book_id": ["00001.XHKG", "00001.XHKG"],
            "open": [10.0, 10.5],
            "close": [10.2, 10.7],
            "volume": [100.0, 120.0],
            "total_turnover": [1000.0, 1284.0],
        }
    ).to_parquet(data_dir / f"{symbol}.parquet")

    def fake_fetch(*args, **kwargs):
        raise AssertionError("remote provider should not be called when local asset is configured")

    monkeypatch.setattr(data_providers, "_fetch_daily_rqdata", fake_fetch)

    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200102",
        "20200103",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "symbol",
            "cache_refresh_days": 0,
            "cache_refresh_on_hit": False,
            "column_map": {
                "trade_date": "trade_date",
                "close": "close",
                "vol": "volume",
                "amount": "total_turnover",
            },
            "rqdata": {
                "daily_asset_dir": str(asset_dir),
            },
        },
    )

    assert result["symbol"].tolist() == [symbol, symbol]
    assert "ts_code" not in result.columns


def test_fetch_daily_derives_tr_close_from_local_ex_factors(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = tmp_path / "daily_assets"
    ex_dir = tmp_path / "ex_factors"
    (asset_dir / "data").mkdir(parents=True, exist_ok=True)
    (ex_dir / "data").mkdir(parents=True, exist_ok=True)
    symbol = "AAA"

    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103", "20200106"],
            "symbol": [symbol, symbol, symbol, symbol],
            "close": [10.0, 10.0, 8.0, 9.0],
            "volume": [100.0, 100.0, 100.0, 100.0],
            "total_turnover": [1000.0, 1000.0, 800.0, 900.0],
        }
    ).to_parquet(asset_dir / "data" / f"{symbol}.parquet")
    pd.DataFrame(
        {
            "ex_date": [pd.Timestamp("2020-01-03")],
            "ex_cum_factor": [1.25],
        }
    ).to_parquet(ex_dir / "data" / f"{symbol}.parquet")

    def fake_fetch(*args, **kwargs):
        raise AssertionError("remote provider should not be called when local asset is configured")

    monkeypatch.setattr(data_providers, "_fetch_daily_rqdata", fake_fetch)

    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200101",
        "20200106",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "symbol",
            "cache_refresh_days": 0,
            "cache_refresh_on_hit": False,
            "column_map": {
                "trade_date": "trade_date",
                "close": "close",
                "vol": "volume",
                "amount": "total_turnover",
            },
            "rqdata": {
                "daily_asset_dir": str(asset_dir),
                "ex_factors_dir": str(ex_dir),
            },
        },
    )

    assert result["tr_close"].round(4).tolist() == [10.0, 10.0, 10.0, 11.25]
    assert result.attrs["tr_close_meta"]["source"] == "local_ex_factors"


def test_load_basic_from_local_asset_accepts_name_fallback_columns(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    instruments_file = tmp_path / "hk_instruments.parquet"

    pd.DataFrame(
        {
            "ts_code": ["00001.HK", "00002.HK"],
            "symbol": ["长和", "中电控股"],
            "listed_date": ["1972-11-01", "1980-01-02"],
            "eng_symbol": ["CKH HOLDINGS", "CLP HOLDINGS"],
        }
    ).to_parquet(instruments_file)

    result = data_providers.load_basic(
        "hk",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "rqdata": {
                "instruments_file": str(instruments_file),
            },
        },
        symbols=["00001.HK"],
    )

    assert result["symbol"].tolist() == ["00001.HK"]
    assert result["name"].tolist() == ["长和"]
    assert result["list_date"].tolist() == ["19721101"]


def test_load_basic_from_local_asset_normalizes_hk_symbol_when_order_book_id_present(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    instruments_file = tmp_path / "hk_instruments.parquet"

    pd.DataFrame(
        {
            "symbol": ["00001.HK", "00002.HK"],
            "order_book_id": ["00001.XHKG", "00002.XHKG"],
            "name": ["长和", "中电控股"],
            "listed_date": ["1972-11-01", "1980-01-02"],
        }
    ).to_parquet(instruments_file)

    result = data_providers.load_basic(
        "hk",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "rqdata": {
                "instruments_file": str(instruments_file),
            },
        },
        symbols=["00001.HK"],
    )

    assert result["symbol"].tolist() == ["00001.HK"]
    assert result["name"].tolist() == ["长和"]
    assert result["list_date"].tolist() == ["19721101"]


def test_load_basic_from_local_asset_normalizes_a_share_order_book_id(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    instruments_file = tmp_path / "a_share_instruments.parquet"

    pd.DataFrame(
        {
            "order_book_id": ["600000.XSHG", "000001.XSHE"],
            "name": ["PF Bank", "Ping An Bank"],
            "listed_date": ["1999-11-10", "1991-04-03"],
        }
    ).to_parquet(instruments_file)

    result = data_providers.load_basic(
        "a_share",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "rqdata": {
                "instruments_file": str(instruments_file),
            },
        },
        symbols=["600000.SH"],
    )

    assert result["symbol"].tolist() == ["600000.SH"]
    assert result["name"].tolist() == ["PF Bank"]
    assert result["list_date"].tolist() == ["19991110"]


def test_fetch_daily_backfills_tr_close_for_existing_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ex_dir = tmp_path / "ex_factors"
    (ex_dir / "data").mkdir(parents=True, exist_ok=True)
    symbol = "AAA"
    cache_file = cache_dir / "hk_rqdata_daily_AAA.parquet"

    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103"],
            "ts_code": [symbol, symbol, symbol],
            "symbol": [symbol, symbol, symbol],
            "close": [10.0, 10.0, 8.0],
            "vol": [100.0, 100.0, 100.0],
            "amount": [1000.0, 1000.0, 800.0],
        }
    ).to_parquet(cache_file)
    pd.DataFrame(
        {
            "ex_date": [pd.Timestamp("2020-01-03")],
            "ex_cum_factor": [1.25],
        }
    ).to_parquet(ex_dir / "data" / f"{symbol}.parquet")

    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200101",
        "20200103",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "symbol",
            "cache_refresh_days": 0,
            "cache_refresh_on_hit": False,
            "rqdata": {
                "ex_factors_dir": str(ex_dir),
            },
        },
    )

    assert result["tr_close"].round(4).tolist() == [10.0, 10.0, 10.0]
    assert result.attrs["tr_close_meta"]["source"] == "local_ex_factors"
    cached = pd.read_parquet(cache_file)
    assert "tr_close" in cached.columns
    assert cached["tr_close"].round(4).tolist() == [10.0, 10.0, 10.0]


def test_fetch_daily_preserves_input_tr_close_when_ex_factors_missing(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = tmp_path / "daily_assets"
    ex_dir = tmp_path / "ex_factors"
    (asset_dir / "data").mkdir(parents=True, exist_ok=True)
    (ex_dir / "data").mkdir(parents=True, exist_ok=True)
    symbol = "AAA"

    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103"],
            "symbol": [symbol, symbol, symbol],
            "close": [10.0, 10.0, 8.0],
            "tr_close": [10.0, 10.0, 10.0],
            "volume": [100.0, 100.0, 100.0],
            "total_turnover": [1000.0, 1000.0, 800.0],
        }
    ).to_parquet(asset_dir / "data" / f"{symbol}.parquet")

    result = data_providers.fetch_daily(
        "hk",
        symbol,
        "20200101",
        "20200103",
        cache_dir,
        client=None,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "symbol",
            "cache_refresh_days": 0,
            "cache_refresh_on_hit": False,
            "column_map": {
                "trade_date": "trade_date",
                "close": "close",
                "vol": "volume",
                "amount": "total_turnover",
            },
            "rqdata": {
                "daily_asset_dir": str(asset_dir),
                "ex_factors_dir": str(ex_dir),
            },
        },
    )

    assert result["tr_close"].round(4).tolist() == [10.0, 10.0, 10.0]
    assert result.attrs["tr_close_meta"]["source"] == "input_frame_missing_ex_factors"


class _FakeRQInstrument:
    def __init__(self, listed_date: str):
        self.listed_date = listed_date


class _FakeRQDailyClient:
    def __init__(self, listed_date: str):
        self.listed_date = listed_date
        self.price_calls: list[tuple[str, str, str, str, dict]] = []

    def instruments(self, order_book_id, market=None):
        return _FakeRQInstrument(self.listed_date)

    def get_price(self, order_book_id, start_date, end_date, frequency, **kwargs):
        self.price_calls.append((order_book_id, start_date, end_date, frequency, kwargs))
        return pd.DataFrame(
            {
                "close": [10.0, 11.0],
                "volume": [100.0, 110.0],
                "total_turnover": [1000.0, 1100.0],
            },
            index=pd.to_datetime(["2015-03-20", "2015-03-23"]),
        )


def test_fetch_daily_rqdata_clamps_start_date_to_listing_date(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_providers._RQDATA_LISTED_DATE_CACHE.clear()
    client = _FakeRQDailyClient("2015-03-20")

    result = data_providers.fetch_daily(
        "hk",
        "01468.HK",
        "20150101",
        "20151231",
        cache_dir,
        client=client,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "range",
            "rqdata": {"market": "hk", "skip_suspended": True},
        },
    )

    assert client.price_calls == [
        (
            "01468.XHKG",
            "20150320",
            "20151231",
            "1d",
            {
                "fields": ["close", "volume", "total_turnover"],
                "skip_suspended": True,
                "market": "hk",
            },
        )
    ]
    assert result["trade_date"].tolist() == ["20150320", "20150323"]


def test_fetch_daily_rqdata_returns_empty_when_symbol_lists_after_requested_range(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_providers._RQDATA_LISTED_DATE_CACHE.clear()
    client = _FakeRQDailyClient("2016-01-05")

    result = data_providers.fetch_daily(
        "hk",
        "01468.HK",
        "20150101",
        "20151231",
        cache_dir,
        client=client,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "range",
            "rqdata": {"market": "hk", "skip_suspended": True},
        },
    )

    assert client.price_calls == []
    assert result.empty


def test_fetch_daily_rqdata_maps_a_share_symbol_to_provider_id(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_providers._RQDATA_LISTED_DATE_CACHE.clear()
    client = _FakeRQDailyClient("1999-11-10")

    result = data_providers.fetch_daily(
        "a_share",
        "600000.SH",
        "20200101",
        "20200131",
        cache_dir,
        client=client,
        data_cfg={
            "provider": "rqdata",
            "cache_mode": "range",
            "rqdata": {"market": "a_share", "skip_suspended": False},
        },
    )

    assert client.price_calls == [
        (
            "600000.XSHG",
            "20200101",
            "20200131",
            "1d",
            {
                "fields": ["close", "volume", "total_turnover"],
                "skip_suspended": False,
                "market": "a_share",
            },
        )
    ]
    assert result["symbol"].tolist() == ["600000.SH", "600000.SH"]
