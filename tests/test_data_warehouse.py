import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import yaml

from market_data_platform import data_warehouse
from market_data_platform.cli import build_parser


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def test_refresh_catalog_scans_manifest_backed_artifacts(tmp_path):
    artifacts_root = tmp_path / "artifacts"
    raw_manifest = (
        artifacts_root
        / "assets"
        / "rqdata"
        / "hk"
        / "daily"
        / "hk_all_daily_latest"
        / "manifest.yml"
    )
    standardized_manifest = (
        artifacts_root / "standardized" / "hk" / "daily_panel" / "hk_daily_panel" / "manifest.yml"
    )

    _write_yaml(
        raw_manifest,
        {
            "name": "hk_all_daily_latest",
            "created_at": "2026-03-20T10:00:00",
            "status": "completed",
            "dataset": "daily",
            "market": "hk",
            "query": {
                "start_date": "20260101",
                "end_date": "20260320",
                "frequency": "1d",
            },
            "columns": ["trade_date", "symbol", "close"],
            "totals": {"rows": 4, "files": 2, "symbols": 2, "bytes": 1234},
        },
    )
    _write_yaml(
        standardized_manifest,
        {
            "name": "hk_daily_panel",
            "created_at": "2026-03-20T11:00:00",
            "status": "completed",
            "layer": "standardized",
            "dataset": "daily_panel",
            "market": "hk",
            "view_name": "hk_daily_panel",
            "frequency": "M",
            "source_asset_dir": str(raw_manifest.parent),
            "source_manifest": str(raw_manifest),
            "output_root": str(standardized_manifest.parent),
            "output_glob": str(standardized_manifest.parent / "data" / "**" / "*.parquet"),
            "columns": ["trade_date", "trade_date_key", "symbol", "close"],
            "column_dtypes": {
                "trade_date": "datetime64[ns]",
                "trade_date_key": "object",
                "symbol": "object",
                "close": "float64",
            },
            "totals": {"output_rows": 2, "output_files": 1, "symbols": 2, "trade_dates": 1},
            "quality": {"duplicate_rows_dropped": 1},
        },
    )

    db_path = artifacts_root / "metadata" / "catalog.sqlite"
    summary_out = artifacts_root / "metadata" / "catalog_summary.csv"
    args = SimpleNamespace(
        artifacts_root=str(artifacts_root),
        db_path=str(db_path),
        summary_out=str(summary_out),
    )

    assert data_warehouse.refresh_catalog(args) == 0
    assert db_path.exists()
    assert summary_out.exists()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT layer, dataset, row_count, symbol_count FROM artifacts ORDER BY layer, dataset"
        ).fetchall()
        assert rows == [
            ("raw_asset", "daily", 4, 2),
            ("standardized", "daily_panel", 2, 2),
        ]
        columns = conn.execute(
            "SELECT column_name FROM artifact_columns WHERE artifact_id = ? ORDER BY ordinal",
            (str(standardized_manifest.resolve()),),
        ).fetchall()
        assert [item[0] for item in columns] == ["trade_date", "trade_date_key", "symbol", "close"]
        lineage = conn.execute(
            "SELECT relation, source_path FROM artifact_lineage "
            "WHERE artifact_id = ? ORDER BY relation",
            (str(standardized_manifest.resolve()),),
        ).fetchall()
        assert lineage == [
            ("source_asset_dir", str(raw_manifest.parent)),
            ("source_manifest", str(raw_manifest)),
        ]


def test_materialize_standardized_from_asset_dir_writes_partitioned_output(tmp_path):
    asset_dir = (
        tmp_path / "artifacts" / "assets" / "rqdata" / "hk" / "daily" / "hk_all_daily_latest"
    )
    data_dir = asset_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        asset_dir / "manifest.yml",
        {
            "name": "hk_all_daily_latest",
            "dataset": "daily",
            "market": "hk",
            "status": "completed",
        },
    )

    pd.DataFrame(
        {
            "trade_date": ["20260105", "20260131", "20260131"],
            "symbol": ["00005.HK", "00005.HK", "00005.HK"],
            "close": [10.0, 11.0, 12.0],
        }
    ).to_parquet(data_dir / "00005.HK.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": ["20260110", "20260131"],
            "symbol": ["00700.HK", "00700.HK"],
            "close": [20.0, 21.0],
        }
    ).to_parquet(data_dir / "00700.HK.parquet", index=False)

    out_root = tmp_path / "artifacts" / "standardized"
    args = SimpleNamespace(
        name="hk_daily_panel",
        market="hk",
        preset="rqdata-daily",
        dataset_name="daily_panel",
        asset_dir=str(asset_dir),
        file=None,
        date_col=None,
        symbol_col=None,
        frequency="M",
        out_root=str(out_root),
        force=False,
    )

    assert data_warehouse.materialize_standardized(args) == 0

    output_dir = out_root / "hk" / "daily_panel" / "hk_daily_panel"
    manifest = yaml.safe_load((output_dir / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["layer"] == "standardized"
    assert manifest["source_asset_dir"] == str(asset_dir)
    assert manifest["source_manifest"] == str(asset_dir / "manifest.yml")
    assert manifest["frequency"] == "M"
    assert manifest["totals"]["input_files"] == 2
    assert manifest["totals"]["output_rows"] == 2
    assert manifest["totals"]["symbols"] == 2
    assert manifest["quality"]["duplicate_rows_dropped"] == 1

    parquet_files = sorted((output_dir / "data").glob("trade_year=*/part-*.parquet"))
    assert len(parquet_files) == 2
    combined = pd.concat([pd.read_parquet(path) for path in parquet_files], ignore_index=True)
    assert combined["symbol"].tolist() == ["00005.HK", "00700.HK"]
    assert combined["trade_date_key"].tolist() == ["20260131", "20260131"]
    assert combined["close"].tolist() == [12.0, 21.0]


def test_materialize_standardized_auto_maps_legacy_ts_code_input(tmp_path):
    asset_dir = (
        tmp_path / "artifacts" / "assets" / "rqdata" / "hk" / "daily" / "hk_all_daily_legacy"
    )
    data_dir = asset_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        asset_dir / "manifest.yml",
        {
            "name": "hk_all_daily_legacy",
            "dataset": "daily",
            "market": "hk",
            "status": "completed",
        },
    )

    pd.DataFrame(
        {
            "trade_date": ["20260105", "20260131"],
            "ts_code": ["00005.HK", "00005.HK"],
            "close": [10.0, 12.0],
        }
    ).to_parquet(data_dir / "00005.HK.parquet", index=False)

    out_root = tmp_path / "artifacts" / "standardized"
    args = SimpleNamespace(
        name="hk_daily_panel_legacy",
        market="hk",
        preset="rqdata-daily",
        dataset_name="daily_panel",
        asset_dir=str(asset_dir),
        file=None,
        date_col=None,
        symbol_col=None,
        frequency="M",
        out_root=str(out_root),
        force=False,
    )

    assert data_warehouse.materialize_standardized(args) == 0

    output_dir = out_root / "hk" / "daily_panel" / "hk_daily_panel_legacy"
    parquet_files = sorted((output_dir / "data").glob("trade_year=*/part-*.parquet"))
    assert len(parquet_files) == 1
    combined = pd.concat([pd.read_parquet(path) for path in parquet_files], ignore_index=True)
    assert combined["symbol"].tolist() == ["00005.HK"]
    assert combined["ts_code"].tolist() == ["00005.HK"]
    assert combined["trade_date_key"].tolist() == ["20260131"]


class _FakeDuckDBConnection:
    def __init__(self, result: pd.DataFrame):
        self.result = result
        self.queries: list[str] = []
        self.closed = False

    def execute(self, sql: str):
        self.queries.append(sql)
        return self

    def df(self) -> pd.DataFrame:
        return self.result.copy()

    def close(self) -> None:
        self.closed = True


class _FakeDuckDBModule:
    def __init__(self, result: pd.DataFrame):
        self.result = result
        self.connections: list[_FakeDuckDBConnection] = []
        self.paths: list[str] = []

    def connect(self, path: str) -> _FakeDuckDBConnection:
        self.paths.append(path)
        conn = _FakeDuckDBConnection(self.result)
        self.connections.append(conn)
        return conn


def test_query_standardized_registers_views_and_renders_json(tmp_path, monkeypatch, capsys):
    standardized_root = tmp_path / "artifacts" / "standardized"
    manifest_path = standardized_root / "hk" / "daily_panel" / "hk_daily_panel" / "manifest.yml"
    _write_yaml(
        manifest_path,
        {
            "name": "hk_daily_panel",
            "layer": "standardized",
            "view_name": "hk_daily_panel",
            "output_glob": str(manifest_path.parent / "data" / "**" / "*.parquet"),
        },
    )

    fake_duckdb = _FakeDuckDBModule(pd.DataFrame([{"value": 1}]))
    monkeypatch.setattr(data_warehouse, "_import_duckdb", lambda: fake_duckdb)

    args = SimpleNamespace(
        sql="select 1 as value",
        sql_file=None,
        db_path=str(tmp_path / "artifacts" / "metadata" / "warehouse.duckdb"),
        standardized_root=str(standardized_root),
        format="json",
        out=None,
    )

    assert data_warehouse.query_standardized(args) == 0
    output = capsys.readouterr().out.strip()
    assert '"value": 1' in output
    assert len(fake_duckdb.connections) == 1
    assert fake_duckdb.connections[0].closed is True
    assert any(
        'CREATE OR REPLACE VIEW standardized."hk_daily_panel"' in query
        for query in fake_duckdb.connections[0].queries
    )
    assert fake_duckdb.connections[0].queries[-1] == "select 1 as value"


def test_materialize_standardized_defaults_follow_artifacts_root(tmp_path):
    artifacts_root = tmp_path / "external-artifacts"
    asset_dir = artifacts_root / "assets" / "rqdata" / "hk" / "daily" / "hk_all_daily_latest"
    data_dir = asset_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        asset_dir / "manifest.yml",
        {
            "name": "hk_all_daily_latest",
            "dataset": "daily",
            "market": "hk",
            "status": "completed",
        },
    )

    pd.DataFrame(
        {
            "trade_date": ["20260105", "20260131"],
            "symbol": ["00005.HK", "00005.HK"],
            "close": [10.0, 12.0],
        }
    ).to_parquet(data_dir / "00005.HK.parquet", index=False)

    args = SimpleNamespace(
        artifacts_root=str(artifacts_root),
        name="hk_daily_panel",
        market="hk",
        preset="rqdata-daily",
        dataset_name="daily_panel",
        asset_dir=str(asset_dir),
        file=None,
        date_col=None,
        symbol_col=None,
        frequency="M",
        out_root=None,
        force=False,
    )

    assert data_warehouse.materialize_standardized(args) == 0
    output_dir = artifacts_root / "standardized" / "hk" / "daily_panel" / "hk_daily_panel"
    assert (output_dir / "manifest.yml").exists()


def test_query_standardized_defaults_follow_artifacts_root(tmp_path, monkeypatch, capsys):
    artifacts_root = tmp_path / "external-artifacts"
    manifest_path = (
        artifacts_root / "standardized" / "hk" / "daily_panel" / "hk_daily_panel" / "manifest.yml"
    )
    _write_yaml(
        manifest_path,
        {
            "name": "hk_daily_panel",
            "layer": "standardized",
            "view_name": "hk_daily_panel",
            "output_glob": str(manifest_path.parent / "data" / "**" / "*.parquet"),
        },
    )

    fake_duckdb = _FakeDuckDBModule(pd.DataFrame([{"value": 1}]))
    monkeypatch.setattr(data_warehouse, "_import_duckdb", lambda: fake_duckdb)

    args = SimpleNamespace(
        artifacts_root=str(artifacts_root),
        sql="select 1 as value",
        sql_file=None,
        db_path=None,
        standardized_root=None,
        format="json",
        out=None,
    )

    assert data_warehouse.query_standardized(args) == 0
    output = capsys.readouterr().out.strip()
    assert '"value": 1' in output
    assert fake_duckdb.paths == [str(artifacts_root / "metadata" / "warehouse.duckdb")]


def test_marketdata_cli_exposes_data_commands():
    parser = build_parser()

    args = parser.parse_args(["data", "query", "--sql", "select 1 as value"])

    assert args.command == "data"
    assert args.data_command == "query"
