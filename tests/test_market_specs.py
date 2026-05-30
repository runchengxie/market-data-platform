import pytest

from market_data_platform.data_provider_contracts import require_supported_market, to_rqdata_symbol
from market_data_platform.symbols import normalize_symbol_for_market


def test_a_share_symbols_map_between_canonical_and_rqdata_ids():
    assert normalize_symbol_for_market("600000.XSHG", market="a_share") == "600000.SH"
    assert normalize_symbol_for_market("000001.XSHE", market="a_share") == "000001.SZ"
    assert normalize_symbol_for_market("600519.sh", market="a_share") == "600519.SH"
    assert normalize_symbol_for_market("1", market="a_share") == "000001.SZ"

    assert to_rqdata_symbol("a_share", "600000.SH") == "600000.XSHG"
    assert to_rqdata_symbol("a_share", "000001.SZ") == "000001.XSHE"
    assert to_rqdata_symbol("a_share", "600000") == "600000.XSHG"
    assert to_rqdata_symbol("a_share", "000001") == "000001.XSHE"


def test_supported_markets_include_a_share_and_reject_unknown():
    assert require_supported_market("a_share") == "a_share"
    assert require_supported_market("hk") == "hk"
    with pytest.raises(ValueError, match="Supported markets: a_share, hk"):
        require_supported_market("us")
