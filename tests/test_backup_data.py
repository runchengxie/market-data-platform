import json
from pathlib import Path

import pytest
import yaml

from market_data_platform import backup_data, cli


def test_backup_data_copies_selected_paths_and_writes_manifest(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "artifacts" / "cache").mkdir(parents=True)
    (repo_root / "artifacts" / "cache" / "prices.parquet").write_text(
        "cache-data",
        encoding="utf-8",
    )
    (repo_root / "artifacts" / "assets" / "universe").mkdir(parents=True)
    (repo_root / "artifacts" / "assets" / "universe" / "universe_by_date.csv").write_text(
        "trade_date,symbol\n20250131,00005.HK\n",
        encoding="utf-8",
    )
    (repo_root / "config").mkdir()
    (repo_root / "config" / "hk.yml").write_text("market: hk\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        backup_data,
        "_git_metadata",
        lambda repo_root: {
            "commit": "deadbeef" * 5,
            "short_commit": "deadbeef",
            "branch": "main",
            "is_dirty": True,
        },
    )

    assert (
        backup_data.main(
            [
                "--out-root",
                "artifacts/snapshots",
                "--name",
                "hk_frozen",
                "--config",
                "config/hk.yml",
            ]
        )
        == 0
    )

    snapshot_dir = repo_root / "artifacts/snapshots" / "hk_frozen"
    assert (snapshot_dir / "artifacts" / "cache" / "prices.parquet").exists()
    assert (snapshot_dir / "artifacts" / "assets" / "universe" / "universe_by_date.csv").exists()
    assert (snapshot_dir / "config" / "hk.yml").exists()

    manifest = yaml.safe_load((snapshot_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["name"] == "hk_frozen"
    assert manifest["git"]["short_commit"] == "deadbeef"
    assert manifest["git"]["is_dirty"] is True
    assert manifest["totals"]["paths"] == 3
    assert manifest["totals"]["files"] == 3


def test_backup_data_skip_missing_allows_partial_snapshot(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "artifacts" / "cache").mkdir(parents=True)
    (repo_root / "artifacts" / "cache" / "prices.parquet").write_text(
        "cache-data",
        encoding="utf-8",
    )

    monkeypatch.chdir(repo_root)

    assert (
        backup_data.main(
            [
                "--out-root",
                "artifacts/snapshots",
                "--name",
                "partial",
                "--no-universe",
                "--include-path",
                "missing_dir",
                "--skip-missing",
            ]
        )
        == 0
    )

    snapshot_dir = repo_root / "artifacts/snapshots" / "partial"
    assert (snapshot_dir / "artifacts" / "cache" / "prices.parquet").exists()
    manifest = yaml.safe_load((snapshot_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["totals"]["paths"] == 1


def test_backup_data_hk_current_preset_copies_current_contract_and_resolved_assets(
    tmp_path, monkeypatch
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    daily_dir = (
        repo_root
        / "artifacts"
        / "assets"
        / "rqdata"
        / "hk"
        / "daily"
        / "hk_all_2000_20260409_daily_clean"
    )
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / "00005.HK.parquet").write_text("daily", encoding="utf-8")
    (daily_dir / "manifest.yml").write_text("dataset: daily\n", encoding="utf-8")

    instruments_dir = repo_root / "artifacts" / "assets" / "rqdata" / "hk" / "instruments"
    instruments_dir.mkdir(parents=True, exist_ok=True)
    instruments_file = instruments_dir / "hk_all_instruments_20260409.parquet"
    instruments_file.write_text("instruments", encoding="utf-8")
    instruments_manifest = instruments_dir / "hk_all_instruments_20260409.manifest.yml"
    instruments_manifest.write_text("dataset: instruments\n", encoding="utf-8")

    current_contract_path = (
        repo_root / "artifacts" / "metadata" / "current_assets" / "hk_current.json"
    )
    current_contract_path.parent.mkdir(parents=True, exist_ok=True)
    current_contract_path.write_text(
        json.dumps(
            {
                "contract": {"name": "hk_current", "market": "hk", "version": 1},
                "assets": {
                    "daily_clean": {
                        "resolved_path": str(daily_dir.resolve()),
                        "manifest_path": str((daily_dir / "manifest.yml").resolve()),
                        "exists": True,
                    },
                    "instruments": {
                        "resolved_path": str(instruments_file.resolve()),
                        "manifest_path": str(instruments_manifest.resolve()),
                        "exists": True,
                    },
                    "valuation": {
                        "resolved_path": str(
                            (
                                repo_root
                                / "artifacts"
                                / "assets"
                                / "rqdata"
                                / "hk"
                                / "valuation"
                                / "missing_demo"
                            ).resolve()
                        ),
                        "exists": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(repo_root)

    assert (
        backup_data.main(
            [
                "--out-root",
                "artifacts/snapshots",
                "--name",
                "hk_current_frozen",
                "--preset",
                "hk_current",
                "--no-cache",
                "--no-universe",
            ]
        )
        == 0
    )

    snapshot_dir = repo_root / "artifacts" / "snapshots" / "hk_current_frozen"
    assert (snapshot_dir / "artifacts" / "metadata" / "current_assets" / "hk_current.json").exists()
    assert (
        snapshot_dir
        / "artifacts"
        / "assets"
        / "rqdata"
        / "hk"
        / "daily"
        / "hk_all_2000_20260409_daily_clean"
        / "00005.HK.parquet"
    ).exists()
    assert (
        snapshot_dir
        / "artifacts"
        / "assets"
        / "rqdata"
        / "hk"
        / "instruments"
        / "hk_all_instruments_20260409.parquet"
    ).exists()
    assert (
        snapshot_dir
        / "artifacts"
        / "assets"
        / "rqdata"
        / "hk"
        / "instruments"
        / "hk_all_instruments_20260409.manifest.yml"
    ).exists()

    manifest = yaml.safe_load((snapshot_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["selection"] == {
        "preset": "hk_current",
        "current_contract_path": str(current_contract_path.resolve()),
        "current_asset_keys": ["daily_clean", "instruments"],
    }


def test_backup_data_hk_current_preset_prunes_universe_file_and_directory_overlap(
    tmp_path, monkeypatch
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    daily_dir = (
        repo_root
        / "artifacts"
        / "assets"
        / "rqdata"
        / "hk"
        / "daily"
        / "hk_all_2000_20260409_daily_clean"
    )
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / "00005.HK.parquet").write_text("daily", encoding="utf-8")
    (daily_dir / "manifest.yml").write_text("dataset: daily\n", encoding="utf-8")

    universe_dir = repo_root / "artifacts" / "assets" / "universe"
    universe_dir.mkdir(parents=True, exist_ok=True)
    universe_by_date = universe_dir / "hk_all_full_by_date.csv"
    universe_symbols = universe_dir / "hk_all_full_symbols.txt"
    universe_meta = universe_dir / "hk_all_full_by_date.meta.yml"
    universe_by_date.write_text("trade_date,symbol\n20260409,00005.HK\n", encoding="utf-8")
    universe_symbols.write_text("00005.HK\n", encoding="utf-8")
    universe_meta.write_text("generated_at: 2026-04-09\n", encoding="utf-8")

    current_contract_path = (
        repo_root / "artifacts" / "metadata" / "current_assets" / "hk_current.json"
    )
    current_contract_path.parent.mkdir(parents=True, exist_ok=True)
    current_contract_path.write_text(
        json.dumps(
            {
                "contract": {"name": "hk_current", "market": "hk", "version": 1},
                "assets": {
                    "daily_clean": {
                        "resolved_path": str(daily_dir.resolve()),
                        "manifest_path": str((daily_dir / "manifest.yml").resolve()),
                        "exists": True,
                    },
                    "universe_by_date": {
                        "resolved_path": str(universe_by_date.resolve()),
                        "exists": True,
                    },
                    "universe_symbols": {
                        "resolved_path": str(universe_symbols.resolve()),
                        "exists": True,
                    },
                    "universe_meta": {
                        "resolved_path": str(universe_meta.resolve()),
                        "exists": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(repo_root)

    assert (
        backup_data.main(
            [
                "--out-root",
                "artifacts/snapshots",
                "--name",
                "hk_current_with_universe",
                "--preset",
                "hk_current",
                "--no-cache",
            ]
        )
        == 0
    )

    snapshot_dir = repo_root / "artifacts" / "snapshots" / "hk_current_with_universe"
    assert (snapshot_dir / "artifacts" / "assets" / "universe" / "hk_all_full_by_date.csv").exists()
    assert (snapshot_dir / "artifacts" / "assets" / "universe" / "hk_all_full_symbols.txt").exists()
    assert (
        snapshot_dir / "artifacts" / "assets" / "universe" / "hk_all_full_by_date.meta.yml"
    ).exists()

    manifest = yaml.safe_load((snapshot_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["totals"]["paths"] == 3
    assert manifest["selection"] == {
        "preset": "hk_current",
        "current_contract_path": str(current_contract_path.resolve()),
        "current_asset_keys": [
            "daily_clean",
            "universe_by_date",
            "universe_symbols",
            "universe_meta",
        ],
    }


def test_backup_data_places_repo_external_paths_under_external_prefix(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    external_file = tmp_path / "outside.txt"
    external_file.write_text("outside", encoding="utf-8")

    monkeypatch.chdir(repo_root)

    assert (
        backup_data.main(
            [
                "--out-root",
                "artifacts/snapshots",
                "--name",
                "external_only",
                "--no-cache",
                "--no-universe",
                "--include-path",
                str(external_file),
            ]
        )
        == 0
    )

    snapshot_dir = repo_root / "artifacts/snapshots" / "external_only"
    rel_target = backup_data._relative_target_path(external_file.resolve(), repo_root.resolve())
    copied_path = snapshot_dir / rel_target
    assert copied_path.exists()
    assert copied_path.read_text(encoding="utf-8") == "outside"

    manifest = yaml.safe_load((snapshot_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["entries"][0]["target"] == str(copied_path)


def test_backup_data_rejects_existing_snapshot_dir(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "artifacts" / "snapshots" / "hk_frozen").mkdir(parents=True)

    monkeypatch.chdir(repo_root)

    with pytest.raises(SystemExit, match="Refusing to overwrite existing snapshot"):
        backup_data.main(["--name", "hk_frozen"])


def test_backup_data_hk_current_preset_requires_current_contract(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    monkeypatch.chdir(repo_root)

    with pytest.raises(SystemExit, match="Current contract not found or invalid"):
        backup_data.main(
            [
                "--name",
                "hk_current_missing_contract",
                "--preset",
                "hk_current",
                "--no-cache",
                "--no-universe",
            ]
        )


def test_backup_data_requires_at_least_one_selected_path(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    monkeypatch.chdir(repo_root)

    with pytest.raises(SystemExit, match="No paths selected for backup."):
        backup_data.main(["--name", "empty", "--no-cache", "--no-universe"])


def test_backup_data_cleans_output_dir_after_copy_failure(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "artifacts" / "cache").mkdir(parents=True)
    (repo_root / "artifacts" / "cache" / "prices.parquet").write_text(
        "cache-data",
        encoding="utf-8",
    )

    monkeypatch.chdir(repo_root)

    def _raise_copy(_source: Path, _target: Path) -> None:
        raise RuntimeError("copy failed")

    monkeypatch.setattr(backup_data, "_copy_path", _raise_copy)

    snapshot_dir = repo_root / "artifacts" / "snapshots" / "broken"
    with pytest.raises(RuntimeError, match="copy failed"):
        backup_data.main(["--name", "broken", "--no-universe"])

    assert not snapshot_dir.exists()


def test_marketdata_cli_runs_backup_data(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "config.yml").write_text("market: hk\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)

    assert (
        cli.main(
            [
                "backup-data",
                "--out-root",
                "artifacts/snapshots",
                "--name",
                "cli_snapshot",
                "--no-cache",
                "--no-universe",
                "--include-path",
                "config.yml",
            ]
        )
        == 0
    )

    snapshot_dir = repo_root / "artifacts" / "snapshots" / "cli_snapshot"
    assert (snapshot_dir / "config.yml").exists()
    assert (snapshot_dir / "manifest.yml").exists()
