from __future__ import annotations

import importlib
import sys

import pytest

from market_data_platform.providers.rqdata_cn import (
    normalize_cn_symbol,
    read_symbols_file,
    to_rqdata_cn_symbol,
)


def test_cn_symbol_normalization_and_rqdata_mapping():
    assert normalize_cn_symbol("600000.XSHG") == "600000.SH"
    assert normalize_cn_symbol("1.sz") == "000001.SZ"
    assert normalize_cn_symbol("300750") == "300750.SZ"

    assert to_rqdata_cn_symbol("600000.SH") == "600000.XSHG"
    assert to_rqdata_cn_symbol("000001.SZ") == "000001.XSHE"


def test_read_symbols_file_supports_text_and_csv(tmp_path):
    text_path = tmp_path / "symbols.txt"
    text_path.write_text("600000.XSHG\n000001.SZ\n", encoding="utf-8")
    csv_path = tmp_path / "symbols.csv"
    csv_path.write_text("order_book_id\n600000.XSHG\n000001.XSHE\n", encoding="utf-8")

    assert read_symbols_file(text_path) == ["600000.SH", "000001.SZ"]
    assert read_symbols_file(csv_path) == ["600000.SH", "000001.SZ"]


def test_legacy_rqdata_module_warns_on_import():
    sys.modules.pop("market_data_platform.rqdata_cn", None)
    with pytest.warns(DeprecationWarning, match="market_data_platform.providers.rqdata_cn"):
        legacy = importlib.import_module("market_data_platform.rqdata_cn")
    assert legacy.normalize_cn_symbol("600000.XSHG") == "600000.SH"
