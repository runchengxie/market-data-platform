# Integrations

## cross-sectional-trees

Current role:

- Strategy research, features, models, backtests, holdings, and reports.
- Read-only consumer of published HK data assets.
- Temporary owner of many data maintenance commands until they migrate here.

Configuration:

```bash
export HK_DATA_PLATFORM_ROOT=/data/hk-data-platform
```

This keeps strategy run/cache/report outputs in the strategy repository while
mapping HK data inputs to the shared root. Only set `CSTREE_ARTIFACTS_ROOT` or
`paths.artifacts_root` to the platform root when the strategy project should
also write its default outputs there.

Output-root override:

```yaml
paths:
  artifacts_root: "/data/hk-data-platform"
```

Consumption rule:

- Prefer `metadata/current_assets/hk_current.json` plus each asset manifest.
- Avoid direct dependencies on another project's working directory.
- Do not scan raw tick-depth snapshots inside low-frequency strategy runs.

## rqdata-hk-depth-snapshots

Current role:

- Tick-depth raw download, health, daily aggregation, reconciliation, packaging.

Recommended publication paths:

```text
<artifacts_root>/assets/rqdata/hk/tick_depth/<snapshot>/
<artifacts_root>/assets/rqdata/hk/tick_depth_daily/<snapshot>/
```

After publishing a formal snapshot, update or regenerate `hk_current.json` so
`tick_depth_raw` and `tick_depth_daily` point at the selected assets.

Example:

```bash
export HK_DATA_PLATFORM_ROOT=/data/hk-data-platform

rqdata-hk-depth emit-asset \
  --kind daily \
  --source artifacts/cache/rqdata/hk_tick_depth_daily/core_20250401_20260409/data.parquet \
  --output "$HK_DATA_PLATFORM_ROOT/assets/rqdata/hk/tick_depth_daily/core_20250401_20260409"

ln -sfn core_20250401_20260409 \
  "$HK_DATA_PLATFORM_ROOT/assets/rqdata/hk/tick_depth_daily/hk_tick_depth_daily_latest"

hkdata contract build \
  --artifacts-root "$HK_DATA_PLATFORM_ROOT" \
  --target-date 20260409
```

## Future execution_cost_model

Execution cost models should be lightweight derived assets, not direct strategy
reads of raw tick parquet. A model asset should document:

- calibration window
- source tick-depth and intraday assets
- usable universe
- spread, depth, participation, impact, and quality assumptions
- as-of date and version
