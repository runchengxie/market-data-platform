import pandas as pd
import pytest

from market_data_platform.symbols import (
    canonicalize_symbol_columns,
    drop_legacy_symbol_columns,
    ensure_symbol_columns,
)


def test_ensure_symbol_columns_accepts_stock_ticker_only():
    frame = pd.DataFrame({"stock_ticker": ["AAA", " BBB ", ""], "weight": [0.3, 0.4, 0.3]})
    out = ensure_symbol_columns(frame, context="positions.csv")
    assert out["symbol"].tolist() == ["AAA", "BBB", ""]
    assert "ts_code" not in out.columns
    assert out["stock_ticker"].tolist() == ["AAA", " BBB ", ""]


def test_ensure_symbol_columns_accepts_symbol_only():
    frame = pd.DataFrame({"symbol": ["AAA", " BBB ", ""], "weight": [0.3, 0.4, 0.3]})
    out = ensure_symbol_columns(frame, context="positions.csv")
    assert out["symbol"].tolist() == ["AAA", "BBB", ""]
    assert "ts_code" not in out.columns
    assert "stock_ticker" not in out.columns


def test_ensure_symbol_columns_accepts_order_book_id_only():
    frame = pd.DataFrame({"order_book_id": ["00005.XHKG", " 00700.XHKG "], "weight": [0.4, 0.6]})

    out = ensure_symbol_columns(frame, context="positions.csv")

    assert out["symbol"].tolist() == ["00005.XHKG", "00700.XHKG"]
    assert out["order_book_id"].tolist() == ["00005.XHKG", " 00700.XHKG "]


def test_ensure_symbol_columns_prefers_canonical_symbol_over_aliases():
    frame = pd.DataFrame(
        {
            "symbol": ["00005.HK", ""],
            "ts_code": ["SHOULD_NOT_WIN", "00011.HK"],
            "stock_ticker": ["5", "11"],
        }
    )

    out = ensure_symbol_columns(frame, context="positions.csv")

    assert out["symbol"].tolist() == ["00005.HK", "00011.HK"]


def test_ensure_symbol_columns_fails_without_any_symbol_alias():
    frame = pd.DataFrame({"weight": [1.0]})

    with pytest.raises(SystemExit, match="missing symbol/stock_ticker/ts_code/order_book_id"):
        ensure_symbol_columns(frame, context="positions.csv")


def test_canonicalize_symbol_columns_drops_legacy_aliases_but_keeps_order_book_id():
    frame = pd.DataFrame(
        {
            "ts_code": ["00005.HK"],
            "stock_ticker": ["5"],
            "order_book_id": ["00005.XHKG"],
            "weight": [1.0],
        }
    )

    out = canonicalize_symbol_columns(frame, context="positions.csv")

    assert out.columns.tolist() == ["order_book_id", "weight", "symbol"]
    assert out.iloc[0]["symbol"] == "00005.HK"
    assert "ts_code" not in out.columns
    assert "stock_ticker" not in out.columns


def test_canonicalize_symbol_columns_can_drop_order_book_id():
    frame = pd.DataFrame(
        {
            "order_book_id": ["00005.XHKG"],
            "weight": [1.0],
        }
    )

    out = canonicalize_symbol_columns(
        frame,
        context="positions.csv",
        drop_order_book_id=True,
    )

    assert out.columns.tolist() == ["weight", "symbol"]
    assert out.iloc[0]["symbol"] == "00005.XHKG"
    assert "order_book_id" not in out.columns


def test_drop_legacy_symbol_columns_preserves_attrs():
    frame = pd.DataFrame({"ts_code": ["AAA"], "value": [1]})
    frame.attrs["cache_key"] = "demo"

    out = drop_legacy_symbol_columns(frame)

    assert out.columns.tolist() == ["value"]
    assert out.attrs == {"cache_key": "demo"}
