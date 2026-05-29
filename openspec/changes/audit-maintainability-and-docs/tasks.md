## 1. Audit Current State

- [x] 1.1 Run and save current quality coverage, maintainability, compatibility, and architecture governance outputs.
- [x] 1.2 Inventory tracked generated/cache/runtime files, including egg-info metadata, `__pycache__`, `.pytest_cache`, `.ruff_cache`, reports, and artifact metadata that should not be maintained as source.
- [x] 1.3 Classify source modules and scripts as active, compatibility, migration-only, archival, generated/cache, cleanup candidate, or needs-refactor.
- [x] 1.4 Produce a hotspot list for files over 1,000 lines, functions over 250 lines, functions with 10 or more arguments, broad public facades, and risky import boundaries.
- [x] 1.5 Audit CLI commands, console scripts, and Python import compatibility layers against tests, docs, and local `rg` evidence.

## 2. Cleanup And Lifecycle Decisions

- [x] 2.1 Update or create a maintenance audit document with active capabilities, retained compatibility surface, cleanup candidates, and refactor priorities.
- [x] 2.2 Update `docs/compatibility.md` so every retained compatibility item has replacement, risk, current status, audit evidence, and cleanup condition.
- [x] 2.3 Decide which migration-only commands remain in the main CLI, which move to internal scripts or archived docs, and which need deprecation steps.
- [x] 2.4 Remove tracked generated/cache artifacts that are safe to delete and update `.gitignore` for recurring generated outputs.
- [x] 2.5 Identify stale release presets or historical report artifacts that need archival documentation rather than active maintenance.

## 3. Quality Coverage Tightening

- [x] 3.1 Replace at least one low-risk Ruff directory or file exclusion with checked coverage or a narrower per-file ignore with rationale.
- [x] 3.2 Bring at least one low-risk Pyright-excluded module under basic type checking, using local annotations or typed boundary helpers as needed.
- [x] 3.3 Run and update quality/maintainability baselines only when the checked scope improves or a documented intentional change requires it.
- [x] 3.4 Extend governance tests if needed so quality coverage, compatibility tables, architecture boundaries, and documentation contracts stay enforceable.

## 4. Refactor High-Risk Modules

- [x] 4.1 Select one high-risk large module with stable extraction boundaries and document the selected responsibility before editing.
- [x] 4.2 Extract a focused helper, renderer, parser, path utility, state model, or argument-building component without changing behavior.
- [x] 4.3 Add or adjust focused tests for the extracted responsibility.
- [x] 4.4 Verify maintainability metrics do not regress and update the audit document with remaining hotspot priorities.

## 5. Documentation Alignment

- [x] 5.1 Review `README.md` for current CLI surface, provider status, artifact boundaries, compatibility notes, test commands, and quality governance commands.
- [x] 5.2 Review `AGENTS.md` for current project scope, data handling rules, active ownership, and required local checks.
- [x] 5.3 Review `docs/README.md` and each `docs/*.md` page for factual drift against implemented commands and package names.
- [x] 5.4 Rewrite stale migration wording so active behavior, retained compatibility, and historical records are clearly separated.
- [x] 5.5 Simplify avoidable "不是...而是..." contrast phrasing and translation-like wording while keeping code-level English terms consistent.
- [x] 5.6 Add or update a current-state snapshot that summarizes supported functionality, known debt, excluded quality coverage, and cleanup priorities.

## 6. Verification

- [x] 6.1 Run `uv run --extra dev python -m pytest`.
- [x] 6.2 Run `uv run --extra dev python -m ruff check .`.
- [x] 6.3 Run `uv run --extra dev python -m pyright`.
- [x] 6.4 Run `uv run --extra dev python scripts/dev/quality_debt.py --skip-ruff --check-baseline`.
- [x] 6.5 Run `uv run --extra dev python scripts/dev/maintainability_metrics.py --check-baseline`.
- [x] 6.6 Run `uv run --extra dev python scripts/dev/compatibility_governance.py --check`.
- [x] 6.7 Run `uv run --extra dev python scripts/dev/architecture_governance.py --check`.
- [x] 6.8 Record any skipped provider-backed checks and the reason they were not run.

Provider-backed checks skipped: no live RQData/TuShare downloads were run because default CI and this change do not require external credentials or network-backed provider calls. Offline tests and CLI/governance checks passed.
