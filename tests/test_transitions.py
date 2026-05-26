from __future__ import annotations

import json
from types import SimpleNamespace

from market_data_platform import cli, transitions
from market_data_platform.providers import rqdata_cn, tushare_cn


def test_provider_modules_are_owned_by_provider_namespace():
    assert rqdata_cn.normalize_cn_symbol("600000.XSHG") == "600000.SH"
    assert tushare_cn.DEFAULT_TOKEN_ENV_KEYS == ("TUSHARE_TOKEN", "TUSHARE_TOKEN_2")


def test_transition_backend_forwards_to_existing_tool(monkeypatch):
    monkeypatch.setattr(
        transitions,
        "resolve_transition_command",
        lambda name: [f"/tmp/{name}"],
    )
    observed: list[list[str]] = []

    def runner(command, *, check, env):
        assert check is False
        assert env is not None
        observed.append(command)
        return SimpleNamespace(returncode=7)

    result = transitions.run_transition_backend("hk-assets", ["mirror-hk-daily"], runner=runner)

    assert result == 7
    assert observed == [["/tmp/hk-assets", "rqdata", "mirror-hk-daily"]]


def test_cli_forwards_transition_backend_args_after_separator(monkeypatch):
    observed: list[tuple[str, list[str]]] = []

    def run_backend(name: str, argv: list[str]) -> int:
        observed.append((name, argv))
        return 0

    monkeypatch.setattr(cli, "run_transition_backend", run_backend)

    assert cli.main(["rqdata", "hk-depth", "--", "health", "--input", "raw"]) == 0
    assert observed == [("hk-depth", ["health", "--input", "raw"])]


def test_migration_status_reports_transition_ownership(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "transition_status",
        lambda: [{"name": "hk-depth", "status": "transition_backend", "available": True}],
    )

    assert cli.main(["migration", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["native"][0]["name"] == "cn-tushare"
    assert payload["transition_backends"][0]["status"] == "transition_backend"
