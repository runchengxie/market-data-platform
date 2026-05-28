from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

HK_CURRENT_CONTRACT_RELATIVE_PATH = Path("metadata") / "current_assets" / "hk_current.json"
HK_CURRENT_PATH_SPECS = {
    "daily": ("assets", "rqdata", "hk", "daily", "hk_all_daily_latest"),
    "daily_clean": ("assets", "rqdata", "hk", "daily", "hk_all_daily_clean_latest"),
    "intraday": ("assets", "rqdata", "hk", "intraday", "hk_intraday_latest"),
    "tick_depth_raw": ("assets", "rqdata", "hk", "tick_depth", "hk_tick_depth_latest"),
    "tick_depth_daily": (
        "assets",
        "rqdata",
        "hk",
        "tick_depth_daily",
        "hk_tick_depth_daily_latest",
    ),
    "execution_cost_model": (
        "assets",
        "rqdata",
        "hk",
        "execution_cost",
        "hk_execution_cost_model_latest",
    ),
    "etf_daily": ("assets", "rqdata", "hk", "daily", "hk_etf_daily_latest"),
    "etf_daily_clean": ("assets", "rqdata", "hk", "daily", "hk_etf_daily_clean_latest"),
    "etf_instruments": (
        "assets",
        "rqdata",
        "hk",
        "instruments",
        "hk_etf_instruments_latest.parquet",
    ),
    "valuation": ("assets", "rqdata", "hk", "valuation", "hk_all_valuation_latest"),
    "instruments": ("assets", "rqdata", "hk", "instruments", "hk_all_instruments_latest.parquet"),
    "pit": ("assets", "rqdata", "hk", "pit_financials", "hk_all_2000_2025_full_market_latest"),
    "ex_factors": ("assets", "rqdata", "hk", "ex_factors", "hk_all_ex_factors_latest"),
    "dividends": ("assets", "rqdata", "hk", "dividends", "hk_all_dividends_latest"),
    "shares": ("assets", "rqdata", "hk", "shares", "hk_all_shares_latest"),
    "exchange_rate": ("assets", "rqdata", "hk", "exchange_rate", "hk_exchange_rate_latest"),
    "southbound": ("assets", "rqdata", "hk", "southbound", "hk_connect_southbound_latest"),
    "financial_details": (
        "assets",
        "rqdata",
        "hk",
        "financial_details",
        "hk_financial_details_latest",
    ),
    "industry_changes": (
        "assets",
        "rqdata",
        "hk",
        "industry_changes",
        "hk_all_industry_changes_latest",
    ),
    "universe_by_date": ("assets", "universe", "hk_all_full_by_date.csv"),
    "universe_symbols": ("assets", "universe", "hk_all_full_symbols.txt"),
    "universe_meta": ("assets", "universe", "hk_all_full_by_date.meta.yml"),
}


def default_current_contract_path(artifacts_root: str | Path, *, market: str = "hk") -> Path:
    market_text = str(market or "hk").strip().lower() or "hk"
    return (
        Path(artifacts_root).expanduser().resolve()
        / "metadata"
        / "current_assets"
        / f"{market_text}_current.json"
    )


def default_hk_current_contract_path(artifacts_root: str | Path) -> Path:
    return default_current_contract_path(artifacts_root, market="hk")


def hk_current_candidate_paths(artifacts_root: str | Path) -> dict[str, Path]:
    root = Path(artifacts_root).expanduser().resolve()
    return {
        asset_key: root.joinpath(*parts)
        for asset_key, parts in HK_CURRENT_PATH_SPECS.items()
    }


def infer_manifest_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidates: list[Path] = []
    if path.is_dir():
        candidates.append(path / "manifest.yml")
    else:
        candidates.append(path.with_name(f"{path.stem}.manifest.yml"))
        candidates.append(path.parent / "manifest.yml")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _as_str_key_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def load_manifest_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        return None
    schema_version = str(payload.get("schema_version") or "").strip()
    dataset = str(payload.get("dataset") or "").strip() or None
    if dataset is None and schema_version:
        dataset = schema_version.split(".", 1)[0] or None
    query = _as_str_key_mapping(payload.get("query"))
    totals = _as_str_key_mapping(payload.get("totals"))
    date_range = _as_str_key_mapping(payload.get("date_range"))
    output_dir = str(payload.get("output_dir") or "").strip()
    if not output_dir and schema_version:
        output_dir = str(path.parent)
    snapshot_name = Path(output_dir).name if output_dir else None
    query_end_date = None
    for key in ("end_date", "date", "mapping_date", "as_of_date"):
        value = query.get(key)
        if value is None:
            continue
        query_end_date = str(value).strip() or None
        if query_end_date:
            break
    if not query_end_date:
        query_end_date = str(date_range.get("end") or "").strip() or None
    query_start_date = None
    for key in ("start_date", "start", "from"):
        value = query.get(key)
        if value is None:
            continue
        query_start_date = str(value).strip() or None
        if query_start_date:
            break
    if not query_start_date:
        query_start_date = str(date_range.get("start") or "").strip() or None
    totals_out = {
        str(key): int(value)
        for key, value in totals.items()
        if str(key).strip() and str(value).strip().isdigit()
    }
    row_count = payload.get("row_count")
    if row_count is not None and str(row_count).strip().isdigit():
        totals_out.setdefault("rows", int(row_count))
    symbol_count = payload.get("symbol_count")
    if symbol_count is not None and str(symbol_count).strip().isdigit():
        totals_out.setdefault("symbols", int(symbol_count))
    files = payload.get("files")
    if isinstance(files, list):
        totals_out.setdefault("files", len(files))
    return {
        "dataset": dataset,
        "schema_version": schema_version or None,
        "status": str(payload.get("status") or "").strip() or None,
        "output_dir": output_dir or None,
        "snapshot_name": snapshot_name,
        "query_start_date": query_start_date,
        "query_end_date": query_end_date,
        "totals": totals_out,
    }


def _path_kind(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def _detect_as_of(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"(\d{8})", str(text))
    return match.group(1) if match else None


def describe_current_path(path: Path) -> dict[str, Any]:
    alias_path = path.expanduser()
    if not alias_path.is_absolute():
        alias_path = alias_path.absolute()
    resolved_path = alias_path.resolve(strict=False)
    manifest_path = infer_manifest_path(alias_path)
    manifest = load_manifest_summary(manifest_path)
    as_of = None
    if isinstance(manifest, Mapping):
        as_of = str(manifest.get("query_end_date") or "").strip() or None
        if not as_of:
            as_of = _detect_as_of(manifest.get("snapshot_name"))
    if not as_of:
        as_of = _detect_as_of(resolved_path.name)
    return {
        "alias_path": str(alias_path),
        "exists": alias_path.exists(),
        "is_symlink": alias_path.is_symlink(),
        "path_kind": _path_kind(alias_path),
        "resolved_path": str(resolved_path),
        "resolved_name": resolved_path.name,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "manifest": manifest,
        "as_of": as_of,
    }


def build_hk_current_contract(
    artifacts_root: str | Path,
    *,
    generated_by: str | None = None,
    target_date: str | None = None,
) -> dict[str, Any]:
    artifacts_root_path = Path(artifacts_root).expanduser().resolve()
    contract_path = default_hk_current_contract_path(artifacts_root_path)
    return {
        "contract": {
            "name": "hk_current",
            "market": "hk",
            "version": 1,
            "artifacts_root": str(artifacts_root_path),
            "contract_path": str(contract_path),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "generated_by": generated_by,
            "target_date": target_date,
        },
        "assets": {
            asset_key: describe_current_path(path)
            for asset_key, path in hk_current_candidate_paths(artifacts_root_path).items()
        },
    }


def write_current_contract(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def load_current_contract(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


DATASET_REGISTRY_RELATIVE_PATH = Path("metadata") / "dataset_registry.csv"
DATASET_REGISTRY_COLUMNS = (
    "dataset_name",
    "version",
    "market",
    "type",
    "date_range",
    "source",
    "records",
    "symbols",
    "description",
    "path",
)
DATASET_REGISTRY_DESCRIPTIONS = {
    "current_contract": "current HK asset contract with resolved aliases and manifest summaries",
    "daily": "current HK raw daily OHLCV asset",
    "daily_clean": "current HK daily clean layer",
    "intraday": "current HK intraday 5m asset",
    "tick_depth_raw": "current HK raw 10-level tick depth snapshot asset",
    "tick_depth_daily": "current HK daily aggregate derived from 10-level tick depth snapshots",
    "execution_cost_model": (
        "current HK execution cost model calibrated from market microstructure assets"
    ),
    "etf_daily": "current HK ETF raw daily asset",
    "etf_daily_clean": "current HK ETF daily clean layer",
    "etf_instruments": "current HK ETF instrument master",
    "valuation": "current HK valuation factors",
    "instruments": "current HK instrument master",
    "pit": "current HK PIT fundamentals asset",
    "ex_factors": "current HK ex-factor events",
    "dividends": "current HK dividend events",
    "shares": "current HK share capital events",
    "exchange_rate": "current HK exchange-rate reference asset",
    "southbound": "current HK Connect southbound eligibility asset",
    "financial_details": "current HK financial details asset",
    "industry_changes": "current HK industry membership changes",
    "universe_by_date": "current HK full-market universe by date",
    "universe_symbols": "current HK latest full-market universe symbols",
    "universe_meta": "current HK universe build metadata",
}
DATASET_REGISTRY_SOURCES = {
    "daily_clean": "derived",
    "etf_daily_clean": "derived",
    "tick_depth_daily": "derived",
    "execution_cost_model": "derived",
    "universe_by_date": "derived",
    "universe_symbols": "derived",
    "universe_meta": "derived",
    "instruments": "rqdata",
    "etf_instruments": "rqdata",
    "tick_depth_raw": "rqdata",
}


def default_dataset_registry_path(artifacts_root: str | Path) -> Path:
    return Path(artifacts_root).expanduser().resolve() / DATASET_REGISTRY_RELATIVE_PATH


def _registry_date(value: object | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(\d{8})", text)
    if match:
        token = match.group(1)
        return f"{token[:4]}-{token[4:6]}-{token[6:8]}"
    return text


def _registry_path(path_text: object, *, artifacts_root: Path) -> str:
    path = Path(str(path_text or "")).expanduser()
    try:
        return path.resolve(strict=False).relative_to(artifacts_root.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def _registry_records_text(entry: Mapping[str, Any]) -> str:
    manifest = _as_str_key_mapping(entry.get("manifest"))
    totals = _as_str_key_mapping(manifest.get("totals"))
    rows = totals.get("rows")
    files = totals.get("files")
    if rows is not None:
        return str(rows)
    if files is not None:
        return f"{files} files"
    return ""


def _registry_symbols_text(entry: Mapping[str, Any]) -> str:
    manifest = _as_str_key_mapping(entry.get("manifest"))
    totals = _as_str_key_mapping(manifest.get("totals"))
    for key in ("symbols_written", "symbols", "files"):
        if totals.get(key) is not None:
            return str(totals[key])
    return ""


def _registry_date_range(entry: Mapping[str, Any]) -> str:
    manifest = _as_str_key_mapping(entry.get("manifest"))
    start = _registry_date(manifest.get("query_start_date"))
    end = _registry_date(manifest.get("query_end_date") or entry.get("as_of"))
    if start and end:
        return f"{start} to {end}"
    if end:
        return f"as of {end}"
    return ""


def build_dataset_registry_rows(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    contract_meta = _as_str_key_mapping(contract.get("contract"))
    target_date = str(contract_meta.get("target_date") or "").strip()
    assets = _as_str_key_mapping(contract.get("assets"))
    artifacts_root = Path(str(contract_meta.get("artifacts_root") or "artifacts")).resolve()
    contract_path = str(contract_meta.get("contract_path") or "").strip()
    rows: list[dict[str, str]] = []
    if contract_path:
        rows.append(
            {
                "dataset_name": "hk_current_contract",
                "version": target_date,
                "market": "hk",
                "type": "metadata",
                "date_range": f"as of {_registry_date(target_date)}" if target_date else "",
                "source": "local",
                "records": f"{len(assets)} assets",
                "symbols": str(len(assets)),
                "description": DATASET_REGISTRY_DESCRIPTIONS["current_contract"],
                "path": _registry_path(contract_path, artifacts_root=artifacts_root),
            }
        )
    for asset_key, raw_entry in assets.items():
        if not isinstance(raw_entry, Mapping):
            continue
        entry = _as_str_key_mapping(raw_entry)
        resolved_path = str(entry.get("resolved_path") or entry.get("alias_path") or "").strip()
        if not resolved_path:
            continue
        manifest = _as_str_key_mapping(entry.get("manifest"))
        as_of = str(entry.get("as_of") or manifest.get("query_end_date") or target_date).strip()
        version = re.sub(r"\D", "", as_of) or target_date
        rows.append(
            {
                "dataset_name": f"hk_{asset_key}",
                "version": version,
                "market": "hk",
                "type": str(asset_key),
                "date_range": _registry_date_range(entry),
                "source": DATASET_REGISTRY_SOURCES.get(
                    str(asset_key),
                    "rqdata" if manifest.get("dataset") else "local",
                ),
                "records": _registry_records_text(raw_entry),
                "symbols": _registry_symbols_text(raw_entry),
                "description": DATASET_REGISTRY_DESCRIPTIONS.get(
                    str(asset_key),
                    f"current HK {asset_key} asset",
                ),
                "path": _registry_path(resolved_path, artifacts_root=artifacts_root),
            }
        )
    return rows


def render_dataset_registry_csv(
    contract: Mapping[str, Any],
    *,
    generated_at: datetime | None = None,
) -> str:
    generated = generated_at or datetime.now().astimezone()
    buffer = io.StringIO()
    buffer.write("# Dataset Registry for current HK research data assets.\n")
    buffer.write(
        "# Auto-generated from artifacts/metadata/current_assets/hk_current.json; "
        "prefer hk_current.json plus each asset manifest for source-of-truth freshness.\n"
    )
    buffer.write(f"# Last updated: {generated.date().isoformat()}\n")
    fieldnames: list[str] = list(DATASET_REGISTRY_COLUMNS)
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(build_dataset_registry_rows(contract))
    return buffer.getvalue()


def write_dataset_registry(path: Path, contract: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dataset_registry_csv(contract), encoding="utf-8")


def current_contract_entry(
    contract: Mapping[str, Any] | None,
    asset_key: str,
) -> dict[str, Any] | None:
    if not isinstance(contract, Mapping):
        return None
    assets = contract.get("assets")
    if not isinstance(assets, Mapping):
        return None
    entry = assets.get(asset_key)
    return dict(entry) if isinstance(entry, Mapping) else None


def match_current_contract_entry(
    contract: Mapping[str, Any] | None,
    *,
    configured_path: Path | None,
    resolved_path: Path | None,
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(contract, Mapping):
        return None
    assets = contract.get("assets")
    if not isinstance(assets, Mapping):
        return None
    configured_text = str(configured_path) if configured_path is not None else None
    resolved_text = str(resolved_path) if resolved_path is not None else None
    for asset_key, entry in assets.items():
        if not isinstance(entry, Mapping):
            continue
        alias_path = str(entry.get("alias_path") or "").strip() or None
        contract_resolved = str(entry.get("resolved_path") or "").strip() or None
        if configured_text and alias_path and configured_text == alias_path:
            return str(asset_key), dict(entry)
        if resolved_text and contract_resolved and resolved_text == contract_resolved:
            return str(asset_key), dict(entry)
    return None
