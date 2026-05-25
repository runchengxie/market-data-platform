# Shared Contract

## Artifacts Root

The shared artifacts root is the storage boundary between data tooling and
strategy repositories.

Recommended environment variables:

```bash
export HK_DATA_PLATFORM_ROOT=/data/hk-data-platform
```

`HK_DATA_PLATFORM_ROOT` is the platform-neutral variable for shared HK data
inputs. `CSTREE_ARTIFACTS_ROOT` is a strategy output-root override; use it only
when a downstream project should also write run/cache/report outputs to this
root.

## Current Contract

```text
<artifacts_root>/metadata/current_assets/hk_current.json
```

Required top-level shape:

```json
{
  "contract": {
    "name": "hk_current",
    "market": "hk",
    "version": 1,
    "artifacts_root": "/data/hk-data-platform",
    "target_date": "20260409"
  },
  "assets": {
    "daily_clean": {
      "alias_path": ".../hk_all_daily_clean_latest",
      "resolved_path": ".../hk_all_2000_20260409_daily_clean_refetched_latest",
      "manifest_path": ".../manifest.yml",
      "as_of": "20260409"
    }
  }
}
```

Build it from the standard asset aliases:

```bash
hkdata contract build \
  --artifacts-root "$HK_DATA_PLATFORM_ROOT" \
  --target-date 20260409
```

## Asset Keys

| Asset key | Default path under artifacts root |
| --- | --- |
| `daily` | `assets/rqdata/hk/daily/hk_all_daily_latest` |
| `daily_clean` | `assets/rqdata/hk/daily/hk_all_daily_clean_latest` |
| `intraday` | `assets/rqdata/hk/intraday/hk_intraday_latest` |
| `tick_depth_raw` | `assets/rqdata/hk/tick_depth/hk_tick_depth_latest` |
| `tick_depth_daily` | `assets/rqdata/hk/tick_depth_daily/hk_tick_depth_daily_latest` |
| `execution_cost_model` | `assets/rqdata/hk/execution_cost/hk_execution_cost_model_latest` |
| `valuation` | `assets/rqdata/hk/valuation/hk_all_valuation_latest` |
| `instruments` | `assets/rqdata/hk/instruments/hk_all_instruments_latest.parquet` |
| `pit` | `assets/rqdata/hk/pit_financials/hk_all_2000_2025_full_market_latest` |
| `industry_changes` | `assets/rqdata/hk/industry_changes/hk_all_industry_changes_latest` |
| `universe_by_date` | `assets/universe/hk_all_full_by_date.csv` |
| `universe_symbols` | `assets/universe/hk_all_full_symbols.txt` |
| `universe_meta` | `assets/universe/hk_all_full_by_date.meta.yml` |

## Dataset Registry

```text
<artifacts_root>/metadata/dataset_registry.csv
```

The registry is a compact human-facing index derived from `hk_current.json` and
asset manifests. It is not the data source of truth.

## Manifest Rule

Every published asset directory should contain a `manifest.yml` or an adjacent
`*.manifest.yml` file for single-file assets. Manifests should expose enough
fields for downstream code to summarize dataset, status, row count, symbol
count, date range, and lineage.
