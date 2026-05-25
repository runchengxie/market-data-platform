from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

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


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


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
    manifest = _mapping(entry.get("manifest"))
    totals = _mapping(manifest.get("totals"))
    rows = totals.get("rows")
    files = totals.get("files")
    if rows is not None:
        return str(rows)
    if files is not None:
        return f"{files} files"
    return ""


def _registry_symbols_text(entry: Mapping[str, Any]) -> str:
    manifest = _mapping(entry.get("manifest"))
    totals = _mapping(manifest.get("totals"))
    for key in ("symbols_written", "symbols", "files"):
        if totals.get(key) is not None:
            return str(totals[key])
    return ""


def _registry_date_range(entry: Mapping[str, Any]) -> str:
    manifest = _mapping(entry.get("manifest"))
    start = _registry_date(manifest.get("query_start_date"))
    end = _registry_date(manifest.get("query_end_date") or entry.get("as_of"))
    if start and end:
        return f"{start} to {end}"
    if end:
        return f"as of {end}"
    return ""


def build_dataset_registry_rows(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    contract_meta = _mapping(contract.get("contract"))
    target_date = str(contract_meta.get("target_date") or "").strip()
    assets = _mapping(contract.get("assets"))
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
        entry = _mapping(raw_entry)
        resolved_path = str(entry.get("resolved_path") or entry.get("alias_path") or "").strip()
        if not resolved_path:
            continue
        manifest = _mapping(entry.get("manifest"))
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
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(DATASET_REGISTRY_COLUMNS)
    for row in build_dataset_registry_rows(contract):
        writer.writerow([row.get(column, "") for column in DATASET_REGISTRY_COLUMNS])
    return buffer.getvalue()


def write_dataset_registry(path: str | Path, contract: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dataset_registry_csv(contract), encoding="utf-8")
