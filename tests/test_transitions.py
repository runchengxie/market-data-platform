from __future__ import annotations

import json
from types import SimpleNamespace

from market_data_platform import cli, hk_workflows, transitions
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


def test_cli_forwards_native_hk_depth_args_after_separator(monkeypatch):
    observed: list[list[str]] = []

    def run_backend(argv: list[str]) -> int:
        observed.append(argv)
        return 0

    monkeypatch.setattr(cli, "_run_hk_depth_cli", run_backend)

    assert cli.main(["rqdata", "hk-depth", "--", "health", "--input", "raw"]) == 0
    assert observed == [["health", "--input", "raw"]]


def test_cli_forwards_hk_assets_transition_backend_args_after_separator(monkeypatch):
    observed: list[tuple[str, list[str]]] = []

    def run_backend(name: str, argv: list[str]) -> int:
        observed.append((name, argv))
        return 0

    monkeypatch.setattr(cli, "run_transition_backend", run_backend)

    assert cli.main(["rqdata", "hk-assets", "--", "mirror-hk-daily"]) == 0
    assert observed == [("hk-assets", ["mirror-hk-daily"])]


def test_migration_status_reports_transition_ownership(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "transition_status",
        lambda: [{"name": "hk-assets", "status": "transition_backend", "available": True}],
    )

    assert cli.main(["migration", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert {item["name"] for item in payload["native"]} >= {"cn-tushare", "cn-rqdata", "hk-depth"}
    assert payload["transition_backends"][0]["status"] == "transition_backend"


def test_sync_hk_transition_links_repoints_broken_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    artifacts_root = workspace / "market-data-platform" / "artifacts"
    for relative in (
        "assets/rqdata",
        "assets/style",
        "assets/universe",
        "metadata/current_assets",
    ):
        artifacts_root.joinpath(relative).mkdir(parents=True)

    cstree_artifacts = workspace / "cross-sectional-trees" / "artifacts"
    for relative in (
        "assets/rqdata",
        "assets/style",
        "assets/universe",
        "metadata/current_assets",
    ):
        link = cstree_artifacts.joinpath(relative)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(workspace / "missing" / relative)

    rows = hk_workflows.sync_hk_transition_links(
        artifacts_root,
        workspace_root=workspace,
    )

    assert {row["status"] for row in rows} == {"updated"}
    assert (
        cstree_artifacts.joinpath("assets/rqdata").resolve(strict=False)
        == artifacts_root.joinpath("assets/rqdata")
    )
    assert (
        cstree_artifacts.joinpath("metadata/current_assets").resolve(strict=False)
        == artifacts_root.joinpath("metadata/current_assets")
    )


def test_sync_hk_transition_links_copies_dataset_registry_when_present(tmp_path):
    workspace = tmp_path / "workspace"
    artifacts_root = workspace / "market-data-platform" / "artifacts"
    registry = artifacts_root / "metadata" / "dataset_registry.csv"
    registry.parent.mkdir(parents=True)
    registry.write_text("dataset_name\nplatform\n", encoding="utf-8")
    target = workspace / "cross-sectional-trees" / "artifacts" / "metadata" / "dataset_registry.csv"
    target.parent.mkdir(parents=True)
    target.write_text("dataset_name\nstale\n", encoding="utf-8")

    rows = hk_workflows.sync_hk_transition_links(
        artifacts_root,
        workspace_root=workspace,
    )

    assert target.read_text(encoding="utf-8") == "dataset_name\nplatform\n"
    registry_rows = [row for row in rows if row.get("file") == str(target)]
    assert registry_rows == [{"file": str(target), "source": str(registry), "status": "updated"}]


def test_cli_sync_hk_links_prints_file_rows(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "sync_hk_transition_links",
        lambda *args, **kwargs: [
            {
                "file": "/tmp/cross/dataset_registry.csv",
                "source": "/tmp/platform/dataset_registry.csv",
                "status": "updated",
            }
        ],
    )

    assert cli.main(["migration", "sync-hk-links", "--artifacts-root", "/tmp/platform"]) == 0

    assert (
        capsys.readouterr().out
        == "updated: /tmp/cross/dataset_registry.csv -> /tmp/platform/dataset_registry.csv\n"
    )


def test_run_hk_depth_refresh_dry_run_builds_full_pipeline(tmp_path):
    workspace = tmp_path / "workspace"
    artifacts_root = workspace / "market-data-platform" / "artifacts"

    summary = hk_workflows.run_hk_depth_refresh(
        artifacts_root=artifacts_root,
        workspace_root=workspace,
        start_date="20260526",
        end_date="20260526",
        symbols="00001.HK",
        name="depth_increment_20260526",
        dry_run=True,
    )

    commands = [step["command"] for step in summary["steps"]]
    assert {cmd[2] for cmd in commands} == {"market_data_platform.hk_depth.cli"}
    assert [cmd[3] for cmd in commands] == [
        "download",
        "health",
        "aggregate-daily",
        "emit-asset",
        "emit-asset",
    ]
    assert "--symbols" in commands[0]
    assert summary["aliases"][0]["status"] == "dry_run_created"
    assert summary["aliases"][1]["target"].endswith(
        "assets/rqdata/hk/tick_depth_daily/depth_increment_20260526"
    )


def test_cli_refresh_hk_current_invokes_platform_workflow(monkeypatch, capsys):
    observed = {}

    def run_refresh(**kwargs):
        observed.update(kwargs)
        return {"returncode": 0, "current_contract": "/tmp/hk_current.json"}

    monkeypatch.setattr(cli, "run_hk_current_refresh", run_refresh)

    assert (
        cli.main(
            [
                "rqdata",
                "refresh-hk-current",
                "--artifacts-root",
                "/tmp/platform-artifacts",
                "--target-date",
                "20260526",
                "--refresh-asset",
                "daily",
                "--inspect-asset",
                "daily_clean",
                "--skip-history",
                "--no-refresh-universe",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["current_contract"] == "/tmp/hk_current.json"
    assert observed["artifacts_root"] == "/tmp/platform-artifacts"
    assert observed["target_date"] == "20260526"
    assert observed["refresh_assets"] == ["daily"]
    assert observed["inspect_assets"] == ["daily_clean"]
    assert observed["skip_history"] is True
    assert observed["no_refresh_universe"] is True


def test_cli_inspect_hk_current_invokes_platform_workflow(monkeypatch, capsys):
    observed = {}

    def run_health(**kwargs):
        observed.update(kwargs)
        return {"returncode": 0, "health_report": "/tmp/current_health.json"}

    monkeypatch.setattr(cli, "run_hk_current_health", run_health)

    assert (
        cli.main(
            [
                "rqdata",
                "inspect-hk-current",
                "--artifacts-root",
                "/tmp/platform-artifacts",
                "--target-date",
                "20260526",
                "--asset",
                "daily",
                "--fail-on-severity",
                "warning",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["health_report"] == "/tmp/current_health.json"
    assert observed["artifacts_root"] == "/tmp/platform-artifacts"
    assert observed["target_date"] == "20260526"
    assert observed["assets"] == ["daily"]
    assert observed["fail_on_severity"] == "warning"


def test_cli_refresh_hk_intraday_invokes_platform_workflow(monkeypatch, capsys):
    observed = {}

    def run_refresh(**kwargs):
        observed.update(kwargs)
        return {"returncode": 0, "asset_alias": "/tmp/hk_intraday_latest"}

    monkeypatch.setattr(cli, "run_hk_intraday_refresh", run_refresh)

    assert (
        cli.main(
            [
                "rqdata",
                "refresh-hk-intraday",
                "--artifacts-root",
                "/tmp/platform-artifacts",
                "--start-date",
                "20260526",
                "--end-date",
                "20260526",
                "--inspect-fail-on-severity",
                "warning",
                "--verify-sampled-segments",
                "1",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["asset_alias"] == "/tmp/hk_intraday_latest"
    assert observed["artifacts_root"] == "/tmp/platform-artifacts"
    assert observed["start_date"] == "20260526"
    assert observed["end_date"] == "20260526"
    assert observed["inspect_fail_on_severity"] == "warning"
    assert observed["verify_sampled_segments"] == 1


def test_cli_refresh_hk_depth_invokes_platform_workflow(monkeypatch, capsys):
    observed = {}

    def run_refresh(**kwargs):
        observed.update(kwargs)
        return {"returncode": 0, "raw_asset": "/tmp/raw"}

    monkeypatch.setattr(cli, "run_hk_depth_refresh", run_refresh)

    assert (
        cli.main(
            [
                "rqdata",
                "refresh-hk-depth",
                "--artifacts-root",
                "/tmp/platform-artifacts",
                "--start-date",
                "20260526",
                "--end-date",
                "20260526",
                "--symbols",
                "00001.HK",
                "--name",
                "depth_increment_20260526",
                "--no-publish-assets",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["raw_asset"] == "/tmp/raw"
    assert observed["artifacts_root"] == "/tmp/platform-artifacts"
    assert observed["start_date"] == "20260526"
    assert observed["end_date"] == "20260526"
    assert observed["symbols"] == "00001.HK"
    assert observed["name"] == "depth_increment_20260526"
    assert observed["publish_assets"] is False


def test_cli_refresh_hk_fundamentals_invokes_platform_workflow(monkeypatch, capsys):
    observed = {}

    def run_refresh(**kwargs):
        observed.update(kwargs)
        return {"returncode": 0, "pit_output": "/tmp/pit"}

    monkeypatch.setattr(cli, "run_hk_fundamentals_refresh", run_refresh)

    assert (
        cli.main(
            [
                "rqdata",
                "refresh-hk-fundamentals",
                "--artifacts-root",
                "/tmp/platform-artifacts",
                "--target-date",
                "20260526",
                "--pit-patch-start-quarter",
                "2024q4",
                "--pit-patch-end-quarter",
                "2026q1",
                "--no-pit-inspect",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["pit_output"] == "/tmp/pit"
    assert observed["artifacts_root"] == "/tmp/platform-artifacts"
    assert observed["target_date"] == "20260526"
    assert observed["pit_patch_start_quarter"] == "2024q4"
    assert observed["pit_patch_end_quarter"] == "2026q1"
    assert observed["inspect_pit"] is False
