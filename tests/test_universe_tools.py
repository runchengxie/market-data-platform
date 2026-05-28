import pandas as pd
import pytest

from market_data_platform.hk_assets import build_hk_connect_universe as hk_universe
from market_data_platform.hk_assets import build_hk_daily_asset_universe as hk_daily_assets


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("today", "today"),
        ("t", "today"),
        ("t-1", "t-1"),
        ("yesterday", "t-1"),
        ("last_trading_day", "last_trading_day"),
        ("last_completed_trading_day", "last_completed_trading_day"),
        ("20260131", "20260131"),
    ],
)
def test_normalize_date_token(token, expected):
    assert hk_universe.normalize_date_token(token, "end-date") == expected


def test_normalize_date_token_rejects_invalid_date():
    with pytest.raises(SystemExit, match="end-date must be in YYYYMMDD format."):
        hk_universe.normalize_date_token("2026-01-31", "end-date")


def test_format_output_path_appends_date_tag():
    out = hk_universe.format_output_path(
        "artifacts/assets/universe/universe_by_date.csv", "20260131", append_date=True
    )
    assert str(out) == "artifacts/assets/universe/universe_by_date_20260131.csv"


def test_format_output_path_supports_template():
    out = hk_universe.format_output_path(
        "artifacts/assets/universe/{as_of}/symbols.txt", "20260131", append_date=True
    )
    assert str(out) == "artifacts/assets/universe/20260131/symbols.txt"


def test_extract_universe_config_normalizes_nested_keys():
    cfg = {
        "hk_connect_universe": {
            "start-date": "20250101",
            "rqdata": {"username": "u", "password": "p"},
        }
    }
    normalized = hk_universe.extract_universe_config(cfg)
    assert normalized["start_date"] == "20250101"
    assert normalized["rqdata_user"] == "u"
    assert normalized["rqdata_pass"] == "p"


def test_resolve_as_of_date_respects_last_trading_variants(monkeypatch):
    calls = []

    def fake_resolve_last_trading_date(rqdatac, as_of, market, include_today):
        calls.append(include_today)
        return pd.Timestamp("2026-01-31")

    monkeypatch.setattr(hk_universe, "resolve_last_trading_date", fake_resolve_last_trading_date)

    hk_universe.resolve_as_of_date(object(), "last_trading_day", "hk")
    hk_universe.resolve_as_of_date(object(), "last_completed_trading_day", "hk")

    assert calls == [True, False]


def test_select_liquid_symbols_keeps_all_when_top_quantile_zero():
    liq = pd.Series(
        [10.0, 30.0, 20.0],
        index=["00005.XHKG", "00700.XHKG", "00001.XHKG"],
    )

    selected = hk_universe.select_liquid_symbols(liq, 0.0)

    assert selected.index.tolist() == ["00700.XHKG", "00001.XHKG", "00005.XHKG"]
    assert selected.tolist() == [30.0, 20.0, 10.0]


def test_build_hk_daily_asset_universe_outputs_symbol_only(tmp_path):
    asset_dir = tmp_path / "daily_assets"
    data_dir = asset_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103", "20200104", "20200105"],
            "symbol": ["AAA.HK"] * 5,
            "total_turnover": [10.0, 20.0, 30.0, 40.0, 50.0],
        }
    ).to_parquet(data_dir / "AAA.HK.parquet")
    pd.DataFrame(
        {
            "trade_date": ["20200101", "20200102", "20200103", "20200104", "20200105"],
            "symbol": ["BBB.HK"] * 5,
            "total_turnover": [50.0, 40.0, 30.0, 20.0, 10.0],
        }
    ).to_parquet(data_dir / "BBB.HK.parquet")

    universe, stats = hk_daily_assets.build_universe_frame(
        asset_dir,
        start_date="20200101",
        end_date="20200131",
        rebalance_frequency="M",
        lookback_days=2,
        min_window_days=2,
        top_quantile=0.0,
        min_turnover=0.0,
    )

    assert stats["symbols_selected"] == 2
    assert universe.columns.tolist() == [
        "trade_date",
        "symbol",
        "liq_metric",
        "selected",
    ]
    assert universe["trade_date"].tolist() == ["20200105", "20200105"]
    assert universe["symbol"].tolist() == ["AAA.HK", "BBB.HK"]
    assert universe["liq_metric"].tolist() == [35.0, 25.0]


def test_discover_daily_asset_dir_prefers_final_latest(monkeypatch, tmp_path):
    daily_root = tmp_path / "rqdata" / "hk" / "daily"
    final_dir = daily_root / "hk_all_2000_20260312_daily_final_latest" / "data"
    full_dir = daily_root / "hk_all_2000_20260312_daily_full_latest" / "data"
    final_dir.mkdir(parents=True)
    full_dir.mkdir(parents=True)

    monkeypatch.setattr(hk_daily_assets, "ASSETS_DIR", tmp_path)

    resolved = hk_daily_assets.discover_daily_asset_dir()

    assert resolved == final_dir.parent


def test_discover_daily_asset_dir_falls_back_to_full_latest(monkeypatch, tmp_path):
    daily_root = tmp_path / "rqdata" / "hk" / "daily"
    full_dir = daily_root / "hk_all_2000_20260312_daily_full_latest" / "data"
    full_dir.mkdir(parents=True)

    monkeypatch.setattr(hk_daily_assets, "ASSETS_DIR", tmp_path)

    resolved = hk_daily_assets.discover_daily_asset_dir()

    assert resolved == full_dir.parent
