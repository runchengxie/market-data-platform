# Migration Plan

## Phase 1: Shared Contract

Status: in progress.

- Create this repository.
- Keep data implementations in existing projects.
- Use a shared artifacts root.
- Register tick-depth and execution-cost asset keys in the current contract.

## Phase 2: Publish Tick-Depth Assets Into Shared Root

- Make `rqdata-hk-depth-snapshots emit-asset` write formal raw and daily assets
  under the shared root.
- Regenerate `hk_current.json` with `tick_depth_raw` and `tick_depth_daily`.
- Store health and reconciliation reports under shared `reports/`.

## Phase 3: Move Control Plane Code

Move generic code here first:

- current contract helpers
- dataset registry helpers
- manifest summary helpers
- shared health policy
- packaging and release metadata conventions

Keep compatibility wrappers in `cross-sectional-trees` until downstream usage is
migrated.

## Phase 4: Strategy Read-Only Boundary

`cross-sectional-trees` should only need:

- artifacts root
- `hk_current.json`
- resolved asset paths
- asset manifests

It should no longer own provider asset refresh, registry generation, or release
packaging logic.
