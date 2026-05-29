## Why

This project has absorbed several migrated HK/CN data workflows and now carries a mix of active platform code, compatibility layers, migration commands, historical presets, large operational modules, and documentation written during multiple transition stages. The current governance scripts make debt visible, but Ruff and Pyright still exclude large parts of `src/`, and the documentation should be checked against the current code surface before the project becomes harder for a team to maintain.

## What Changes

- Add a repo-wide maintainability audit that classifies active code, compatibility code, one-off migration code, generated/cache artifacts, obsolete entry points, and modules that need decomposition.
- Produce a cleanup plan for unnecessary or archival functionality, including migration-only commands, compatibility wrappers, stale presets, generated files, and large historical report artifacts that should not be maintained as source code.
- Tighten quality governance incrementally by reducing broad Ruff/Pyright excludes, preferring per-file ignores with reasons, and using the existing baseline scripts to prevent regressions while selected modules are brought under checking.
- Review Python structure against common maintainability principles: focused modules, low coupling across core/provider/HK/release boundaries, explicit public APIs, small functions, clear CLI boundaries, and PEP 8-compatible style.
- Review tests and CI commands so they reflect the current supported workflows, provider extras, governance scripts, and documentation contracts.
- Audit `README.md`, `AGENTS.md`, and `docs/*.md` for factual drift, stale migration wording, mixed Chinese/English terminology, overly abstract phrasing, translation-like sentences, and avoidable "不是...而是..." style.
- Document the resulting decisions so future contributors know what is supported, what is deprecated, what is archival, and which checks must pass.

## Capabilities

### New Capabilities

- `maintainability-audit`: Defines how the project inventories code ownership, large-module debt, compatibility layers, one-off scripts, generated files, and cleanup candidates.
- `quality-coverage-tightening`: Defines how Ruff, Pyright, tests, and governance baselines are expanded without breaking the whole migrated codebase at once.
- `documentation-fact-alignment`: Defines how root docs and `docs/` content are checked and updated for current behavior, terminology, and readable Chinese style.

### Modified Capabilities

None.

## Impact

- Affected source areas include `src/market_data_platform`, `src/hk_data_platform`, `scripts/dev`, `tests`, `pyproject.toml`, `.github/workflows/ci.yml`, `README.md`, `AGENTS.md`, and `docs/*.md`.
- Current baseline facts to preserve in the audit: `scripts/dev/maintainability_metrics.py` reports 131 Python files and 51,136 Python lines across `src`, `scripts`, and `tests`, with 69 functions over 100 lines and 17 over 250 lines. `scripts/dev/quality_debt.py --json --skip-ruff` reports Ruff checking 13,591 of 47,105 source lines and Pyright checking 7,552 of 47,105 source lines.
- The change should not remove public compatibility layers or migration commands until usage is audited and deprecation or archival criteria are documented.
- No provider credentials, large data files, or runtime data artifacts should be introduced into Git as part of this work.
