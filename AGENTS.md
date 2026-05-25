# AGENTS.md

## Project Scope

This repository owns the shared Hong Kong data control plane. It is not a
strategy repository and it should not store large market data in Git.

The intended responsibilities are:

- HK asset contracts and registry conventions.
- Shared directory layout for RQData daily, PIT, valuation, industry, universe,
  intraday, tick-depth, and execution-cost-model assets.
- Health, reconciliation, packaging, and release workflows as they are migrated
  from existing projects.

## Data Rules

- Do not commit parquet, archive parts, provider caches, run outputs, reports, or
  local credentials.
- Large data belongs under a shared artifacts root, NAS, object storage, or
  release assets.
- Git tracks code, docs, schema, small fixtures, and migration records.

## Current Stage

This is a stage-1 skeleton. `cross-sectional-trees` and
`rqdata-hk-depth-snapshots` remain the active implementation owners while this
repo stabilizes the shared contract.

## Commands

Recommended setup:

```bash
uv sync --extra dev
```

Checks:

```bash
uv run pytest
uv run ruff check .
uv run pyright
```
