from __future__ import annotations

import importlib
import json
import sys

import pytest
import yaml

from market_data_platform.cli import build_parser
from market_data_platform.providers import tushare_cn


class FakeDataClient:
    def __init__(self, pd) -> None:
        self.pd = pd
        self.daily_dates: list[str] = []
        self.daily_fields: list[str | None] = []
        self.daily_basic_fields: list[str | None] = []

    def stock_basic(self, *, exchange: str, list_status: str, fields: str):
        assert exchange == ""
        assert "ts_code" in fields
        return self.pd.DataFrame(
            {
                "ts_code": [f"00000{1 if list_status == 'L' else 2}.SZ"],
                "list_status": [list_status],
                "name": ["demo"],
                "market": ["Main Board"],
            }
        )

    def trade_cal(self, **kwargs):
        return self.pd.DataFrame(
            {
                "cal_date": ["20260522", "20260523", "20260525"],
                "is_open": ["1", "0", "1"],
            }
        )

    def daily(self, *, trade_date: str, **kwargs):
        self.daily_dates.append(trade_date)
        self.daily_fields.append(kwargs.get("fields"))
        return self.pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [trade_date],
                "open": [10.0],
                "close": [10.1],
            }
        )

    def daily_basic(self, *, trade_date: str, **kwargs):
        self.daily_basic_fields.append(kwargs.get("fields"))
        return self.pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [trade_date],
                "close": [10.1],
                "total_mv": [100.0],
            }
        )


class FakeTushare:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def pro_api(self, *, token: str):
        self.tokens.append(token)
        return self

    def trade_cal(self, *, exchange: str, start_date: str, end_date: str):
        assert exchange == ""
        assert start_date == "20200101"
        assert end_date == "20200110"
        return []


def test_verify_tokens_reports_status_without_exposing_token(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-primary-token")
    monkeypatch.delenv("TUSHARE_TOKEN_2", raising=False)

    summary = tushare_cn.verify_tushare_tokens(tushare_module=FakeTushare())

    assert summary["valid_tokens"] == 1
    assert summary["results"][0] == {
        "env": "TUSHARE_TOKEN",
        "configured": True,
        "valid": True,
    }
    assert "secret-primary-token" not in json.dumps(summary)


def test_verify_tokens_redacts_token_echoed_by_provider_error(monkeypatch):
    class RejectingTushare(FakeTushare):
        def trade_cal(self, *, exchange: str, start_date: str, end_date: str):
            raise RuntimeError(f"invalid token: {self.tokens[-1]}")

    monkeypatch.setenv("TUSHARE_TOKEN", "secret-primary-token")

    summary = tushare_cn.verify_tushare_tokens(
        env_keys=["TUSHARE_TOKEN"],
        tushare_module=RejectingTushare(),
    )

    assert summary["valid_tokens"] == 0
    assert "secret-primary-token" not in json.dumps(summary)
    assert "<redacted>" in summary["results"][0]["error"]


def test_export_cn_instruments_writes_manifest_and_canonical_symbols(tmp_path):
    pd = pytest.importorskip("pandas")
    output = tmp_path / "cn_instruments.csv"
    symbols_output = tmp_path / "symbols.txt"

    manifest = tushare_cn.export_cn_instruments(
        out=output,
        symbols_out=symbols_output,
        list_statuses=["L", "D"],
        client=FakeDataClient(pd),
    )

    exported = pd.read_csv(output)
    assert exported["symbol"].tolist() == ["000001.SZ", "000002.SZ"]
    assert exported["market"].tolist() == ["Main Board", "Main Board"]
    assert exported["platform_market"].tolist() == ["cn", "cn"]
    assert symbols_output.read_text(encoding="utf-8") == "000001.SZ\n000002.SZ\n"
    assert manifest["provider"] == "tushare"
    assert yaml.safe_load((tmp_path / "cn_instruments.manifest.yml").read_text())["dataset"] == (
        "instruments"
    )


def test_daily_mirror_fetches_full_market_by_open_trade_date(monkeypatch, tmp_path):
    pd = pytest.importorskip("pandas")
    client = FakeDataClient(pd)

    def write_stub(frame, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frame.to_csv(index=False), encoding="utf-8")

    monkeypatch.setattr(tushare_cn, "_write_frame", write_stub)
    manifest = tushare_cn.mirror_cn_daily(
        out_dir=tmp_path / "cn_daily",
        start_date="20260522",
        end_date="20260525",
        client=client,
    )

    assert client.daily_dates == ["20260522", "20260525"]
    assert all(field_text is not None for field_text in client.daily_fields)
    assert "open" in client.daily_fields[0]
    assert "pct_chg" in client.daily_fields[0]
    assert "amount" in client.daily_fields[0]
    assert (tmp_path / "cn_daily" / "data" / "trade_date=20260522" / "part.parquet").exists()
    assert manifest["totals"]["trade_dates_written"] == 2
    assert manifest["query"]["partition_by"] == "trade_date"
    assert manifest["query"]["fields"] == list(tushare_cn.DEFAULT_DAILY_FIELDS)


def test_daily_basic_mirror_uses_default_fields(monkeypatch, tmp_path):
    pd = pytest.importorskip("pandas")
    client = FakeDataClient(pd)

    def write_stub(frame, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frame.to_csv(index=False), encoding="utf-8")

    monkeypatch.setattr(tushare_cn, "_write_frame", write_stub)
    manifest = tushare_cn.mirror_cn_daily_basic(
        out_dir=tmp_path / "cn_daily_basic",
        start_date="20260522",
        end_date="20260522",
        client=client,
    )

    assert client.daily_basic_fields
    assert "turnover_rate" in client.daily_basic_fields[0]
    assert "total_mv" in client.daily_basic_fields[0]
    assert "circ_mv" in client.daily_basic_fields[0]
    assert manifest["query"]["fields"] == list(tushare_cn.DEFAULT_DAILY_BASIC_FIELDS)


def test_stk_limit_command_is_exposed_with_limit_status_alias():
    parser = build_parser()
    required = ["--out-dir", "output", "--start-date", "20260522", "--end-date", "20260525"]

    primary = parser.parse_args(["tushare", "mirror-cn-stk-limit", *required])
    alias = parser.parse_args(["tushare", "mirror-cn-limit-status", *required])

    assert primary.tushare_command == "mirror-cn-stk-limit"
    assert alias.tushare_command == "mirror-cn-limit-status"


def test_legacy_tushare_module_warns_on_import():
    sys.modules.pop("market_data_platform.tushare_cn", None)
    with pytest.warns(DeprecationWarning, match="market_data_platform.providers.tushare_cn"):
        legacy = importlib.import_module("market_data_platform.tushare_cn")
    assert legacy.DEFAULT_TOKEN_ENV_KEYS == ("TUSHARE_TOKEN", "TUSHARE_TOKEN_2")
