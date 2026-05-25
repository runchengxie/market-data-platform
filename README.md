# hk-data-platform

Shared control plane for Hong Kong research data assets.

This repository is the staging area for splitting reusable HK data management out
of strategy repositories. It should own contracts, registry conventions, schema,
health policy, packaging, and release workflows. Large data files stay outside
Git.

## Target Shape

```text
hk-data-platform/
  daily / PIT / valuation / industry / universe
  intraday 5m
  tick_depth raw / tick_depth_daily
  execution_cost_model
  current contract / dataset registry / health / reconcile / release

cross-sectional-trees/
  strategy, features, models, backtests, holdings
  read-only consumer of hk-data-platform

rqdata-hk-depth-snapshots/
  short term: independent tick-depth implementation
  medium term: tick_depth module inside hk-data-platform
```

## Stage-1 Boundary

For now, this repo defines the shared contract and path conventions. The active
implementation still lives in:

- `cross-sectional-trees`: daily, PIT, valuation, industry, universe, current
  contract, dataset registry, health, and release tooling.
- `rqdata-hk-depth-snapshots`: tick-depth download, health, daily aggregation,
  reconciliation, and packaging.

The first operational step is to point both projects at the same shared artifacts
root:

```bash
export HK_DATA_PLATFORM_ROOT=/data/hk-data-platform
```

`HK_DATA_PLATFORM_ROOT` is the neutral name for this repo. `CSTREE_ARTIFACTS_ROOT`
is only needed when a project intentionally wants its run/cache/report outputs to
move to the same root. Strategy repositories should normally keep their own
output root and consume HK data through `HK_DATA_PLATFORM_ROOT`.

## Shared Layout

```text
<artifacts_root>/
  assets/
    rqdata/
      hk/
        daily/
        intraday/
        pit_financials/
        valuation/
        industry_changes/
        tick_depth/
        tick_depth_daily/
        execution_cost/
    universe/
  metadata/
    current_assets/
      hk_current.json
    dataset_registry.csv
  reports/
  standardized/
```

## Current Contract

The shared current contract is:

```text
<artifacts_root>/metadata/current_assets/hk_current.json
```

It records asset keys, alias paths, resolved paths, manifest summaries, and
as-of dates. Strategy repositories should consume resolved assets from this
contract instead of scanning mutable `latest` aliases.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run pyright
```

See `docs/README.md` for the contract and migration notes.
