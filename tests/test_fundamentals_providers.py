import sys
import types

import pandas as pd
import pytest

from cstree import data_providers


class _FakeRQDataClient:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls: list[tuple] = []

    def get_factor(self, order_book_id, factors, start_date, end_date, **kwargs):
        self.calls.append((order_book_id, tuple(factors), start_date, end_date, kwargs))
        return self.frame.copy()


def _hk_factor_frame() -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [["00005.XHKG"], pd.to_datetime(["2025-01-02", "2025-01-03"])],
        names=["order_book_id", "date"],
    )
    return pd.DataFrame(
        {
            "hk_total_market_val": [1000.0, 1010.0],
            "pe_ratio_ttm": [8.0, 8.1],
            "pb_ratio_ttm": [1.1, 1.2],
        },
        index=index,
    )


class _FakeRQDataModule(types.ModuleType):
    def __init__(self, frame: pd.DataFrame):
        super().__init__("rqdatac")
        self.frame = frame
        self.init_calls: list[dict[str, object]] = []
        self.factor_calls: list[tuple] = []

    def init(self, **kwargs):
        self.init_calls.append(dict(kwargs))

    def get_factor(self, order_book_id, factors, start_date, end_date, **kwargs):
        self.factor_calls.append((order_book_id, tuple(factors), start_date, end_date, kwargs))
        if not self.init_calls:
            raise RuntimeError("rqdatac is not initialized.")
        return self.frame.copy()


def test_fetch_fundamentals_rqdata_hk_provider_standardizes_and_caches(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = _FakeRQDataClient(_hk_factor_frame())
    data_cfg = {
        "provider": "rqdata",
        "rqdata": {"market": "hk"},
    }
    fundamentals_cfg = {
        "endpoint": "get_factor",
        "fields": ["hk_total_market_val", "pe_ratio_ttm", "pb_ratio_ttm"],
        "column_map": {
            "trade_date": "trade_date",
            "symbol": "symbol",
            "market_cap": "hk_total_market_val",
            "pe_ttm": "pe_ratio_ttm",
            "pb": "pb_ratio_ttm",
        },
    }

    first = data_providers.fetch_fundamentals(
        "hk",
        "00005.HK",
        "20250102",
        "20250103",
        cache_dir,
        client,
        data_cfg,
        fundamentals_cfg,
    )
    second = data_providers.fetch_fundamentals(
        "hk",
        "00005.HK",
        "20250102",
        "20250103",
        cache_dir,
        client,
        data_cfg,
        fundamentals_cfg,
    )

    assert client.calls == [
        (
            "00005.XHKG",
            ("hk_total_market_val", "pe_ratio_ttm", "pb_ratio_ttm"),
            "20250102",
            "20250103",
            {"market": "hk"},
        )
    ]
    assert first.equals(second)
    assert first["trade_date"].tolist() == ["20250102", "20250103"]
    assert first["symbol"].tolist() == ["00005.HK", "00005.HK"]
    assert "ts_code" not in first.columns
    assert {"market_cap", "pe_ttm", "pb"}.issubset(first.columns)
    assert len(list(cache_dir.glob("hk_rqdata_fundamentals_*.parquet"))) == 1


def test_fetch_fundamentals_rqdata_hk_provider_lazy_inits_when_client_missing(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fake_rqdatac = _FakeRQDataModule(_hk_factor_frame())
    monkeypatch.setitem(sys.modules, "rqdatac", fake_rqdatac)
    data_cfg = {
        "provider": "rqdata",
        "rqdata": {
            "market": "hk",
            "init": {"username": "demo_user", "password": "demo_pass"},
        },
    }
    fundamentals_cfg = {
        "endpoint": "get_factor",
        "fields": ["hk_total_market_val", "pe_ratio_ttm", "pb_ratio_ttm"],
        "column_map": {
            "trade_date": "trade_date",
            "symbol": "symbol",
            "market_cap": "hk_total_market_val",
            "pe_ttm": "pe_ratio_ttm",
            "pb": "pb_ratio_ttm",
        },
    }

    result = data_providers.fetch_fundamentals(
        "hk",
        "00005.HK",
        "20250102",
        "20250103",
        cache_dir,
        None,
        data_cfg,
        fundamentals_cfg,
    )

    assert fake_rqdatac.init_calls == [{"username": "demo_user", "password": "demo_pass"}]
    assert len(fake_rqdatac.factor_calls) == 2
    assert result["trade_date"].tolist() == ["20250102", "20250103"]
    assert result["symbol"].tolist() == ["00005.HK", "00005.HK"]
    assert {"market_cap", "pe_ttm", "pb"}.issubset(result.columns)


def test_fetch_fundamentals_cache_key_tracks_field_config(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = _FakeRQDataClient(_hk_factor_frame())
    data_cfg = {
        "provider": "rqdata",
        "rqdata": {"market": "hk"},
    }
    base_cfg = {
        "endpoint": "get_factor",
        "column_map": {
            "trade_date": "trade_date",
            "symbol": "symbol",
            "market_cap": "hk_total_market_val",
        },
    }

    data_providers.fetch_fundamentals(
        "hk",
        "00005.HK",
        "20250102",
        "20250103",
        cache_dir,
        client,
        data_cfg,
        {**base_cfg, "fields": ["hk_total_market_val"]},
    )
    data_providers.fetch_fundamentals(
        "hk",
        "00005.HK",
        "20250102",
        "20250103",
        cache_dir,
        client,
        data_cfg,
        {**base_cfg, "fields": ["hk_total_market_val", "pe_ratio_ttm"]},
    )

    assert len(client.calls) == 2
    assert len(list(cache_dir.glob("hk_rqdata_fundamentals_*.parquet"))) == 2


def test_fetch_fundamentals_rqdata_non_hk_market_rejected(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = _FakeRQDataClient(_hk_factor_frame())

    with pytest.raises(ValueError, match="market='hk'"):
        data_providers.fetch_fundamentals(
            "a_share",
            "000001.SZ",
            "20250102",
            "20250103",
            cache_dir,
            client,
            {"provider": "rqdata"},
            {"endpoint": "get_factor"},
        )
