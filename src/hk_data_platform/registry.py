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
    "current_contract": (
        "current {market_label} asset contract with resolved aliases and manifest summaries"
    ),
    "daily": "current {market_label} raw daily OHLCV asset",
    "daily_clean": "current {market_label} daily clean layer",
    "intraday": "current {market_label} intraday 5m asset",
    "tick_depth_raw": "current {market_label} raw 10-level tick depth snapshot asset",
    "tick_depth_daily": (
        "current {market_label} daily aggregate derived from 10-level tick depth snapshots"
    ),
    "execution_cost_model": (
        "current {market_label} execution cost model calibrated from market microstructure assets"
    ),
    "etf_daily": "current {market_label} ETF raw daily asset",
    "etf_daily_clean": "current {market_label} ETF daily clean layer",
    "etf_instruments": "current {market_label} ETF instrument master",
    "valuation": "current {market_label} valuation factors",
    "instruments": "current {market_label} instrument master",
    "pit": "current {market_label} PIT fundamentals asset",
    "ex_factors": "current {market_label} ex-factor events",
    "dividends": "current {market_label} dividend events",
    "shares": "current {market_label} share capital events",
    "exchange_rate": "current {market_label} exchange-rate reference asset",
    "southbound": "current {market_label} Connect southbound eligibility asset",
    "financial_details": "current {market_label} financial details asset",
    "industry_changes": "current {market_label} industry membership changes",
    "industry": "current {market_label} industry labels",
    "industry_citic": "current {market_label} CITIC industry labels",
    "industry_sw": "current {market_label} Shenwan industry labels",
    "st_flags": "current {market_label} ST flag history",
    "suspend": "current {market_label} suspension history",
    "limit_status": "current {market_label} limit-up/limit-down status history",
    "index_components": "current {market_label} index component history",
    "northbound": "current {market_label} northbound reference asset",
    "universe_by_date": "current {market_label} full-market universe by date",
    "universe_symbols": "current {market_label} latest full-market universe symbols",
    "universe_meta": "current {market_label} universe build metadata",
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
    "st_flags": "rqdata",
    "suspend": "rqdata",
    "limit_status": "rqdata",
    "index_components": "rqdata",
    "northbound": "rqdata",
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


def _market_label(market: str) -> str:
    return market.upper()


def _registry_description(asset_key: str, market: str) -> str:
    template = DATASET_REGISTRY_DESCRIPTIONS.get(
        asset_key,
        "current {market_label} {asset_key} asset",
    )
    return template.format(market_label=_market_label(market), asset_key=asset_key)


def build_dataset_registry_rows(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    contract_meta = _mapping(contract.get("contract"))
    target_date = str(contract_meta.get("target_date") or "").strip()
    assets = _mapping(contract.get("assets"))
    artifacts_root = Path(str(contract_meta.get("artifacts_root") or "artifacts")).resolve()
    contract_path = str(contract_meta.get("contract_path") or "").strip()
    market = str(contract_meta.get("market") or "hk").strip().lower() or "hk"
    contract_name = str(contract_meta.get("name") or f"{market}_current").strip()
    rows: list[dict[str, str]] = []
    if contract_path:
        rows.append(
            {
                "dataset_name": f"{contract_name}_contract",
                "version": target_date,
                "market": market,
                "type": "metadata",
                "date_range": f"as of {_registry_date(target_date)}" if target_date else "",
                "source": "local",
                "records": f"{len(assets)} assets",
                "symbols": str(len(assets)),
                "description": _registry_description("current_contract", market),
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
                "dataset_name": f"{market}_{asset_key}",
                "version": version,
                "market": market,
                "type": str(asset_key),
                "date_range": _registry_date_range(entry),
                "source": DATASET_REGISTRY_SOURCES.get(
                    str(asset_key),
                    "rqdata" if manifest.get("dataset") else "local",
                ),
                "records": _registry_records_text(raw_entry),
                "symbols": _registry_symbols_text(raw_entry),
                "description": _registry_description(str(asset_key), market),
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
    contract_meta = _mapping(contract.get("contract"))
    market = str(contract_meta.get("market") or "hk").strip().lower() or "hk"
    contract_name = str(contract_meta.get("name") or f"{market}_current").strip()
    buffer = io.StringIO()
    buffer.write(f"# Dataset Registry for current {_market_label(market)} research data assets.\n")
    buffer.write(
        f"# Auto-generated from artifacts/metadata/current_assets/{contract_name}.json; "
        f"prefer {contract_name}.json plus each asset manifest for source-of-truth freshness.\n"
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
