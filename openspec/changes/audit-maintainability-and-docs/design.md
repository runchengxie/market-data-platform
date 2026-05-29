## Context

The repository is a Python market data platform that now owns shared contracts, current asset metadata, CN provider MVPs, HK depth workflows, HK asset production workflows, release tooling, and compatibility wrappers for older package and console-script names. The current tree also contains generated metadata directories, cache output, historical reports, and migration-era documentation.

The project already has useful governance:

- CI runs pytest, Ruff, Pyright, quality coverage baseline, maintainability baseline, compatibility governance, and architecture governance.
- `scripts/dev/quality_debt.py --json --skip-ruff` reports configured Ruff/Pyright coverage and blocks new broad exclusions through baseline checks.
- `scripts/dev/maintainability_metrics.py` reports large files, large functions, argument counts, and public facade size.
- `docs/compatibility.md` tracks retained compatibility items and cleanup conditions.

The main problem is that the safeguards are still mostly defensive. Current measured facts show that Ruff checks 13,591 of 47,105 source lines and Pyright checks 7,552 of 47,105 source lines. Several files exceed 1,000 lines, including `hk_assets/asset_health.py`, `release_tools/hk_asset_workflow.py`, `hk_assets/audit_assets.py`, `hk_depth/downloader.py`, `hk_assets/mirror_financial.py`, `hk_assets/mirror_workflow.py`, `hk_assets/coverage.py`, `hk_assets/intraday_health.py`, `hk_assets/build.py`, `data_providers.py`, `hk_workflows.py`, `release_tools/package_assets.py`, `data_warehouse.py`, `hk_assets/args.py`, and `cli.py`.

## Goals / Non-Goals

**Goals:**

- Create an evidence-backed audit of active code, compatibility code, one-off migration code, generated artifacts, stale docs, test coverage, and quality-tool exclusions.
- Convert broad concerns into specific cleanup and refactor candidates with owners, evidence, risk, and validation commands.
- Bring selected low-risk modules back under Ruff/Pyright and document remaining excludes with narrower scope.
- Keep existing behavior stable while improving maintainability, documentation truthfulness, and onboarding clarity.
- Make root README, `AGENTS.md`, and `docs/*.md` reflect the current project state and quality workflow.
- Improve Chinese documentation style by replacing stale migration phrasing and unnecessary contrast framing with direct maintainer-facing prose.

**Non-Goals:**

- Rewriting all HK asset, HK depth, or release tooling in one change.
- Removing compatibility commands or Python import paths without usage evidence and documented migration criteria.
- Running secret-backed provider downloads in CI.
- Moving or deleting large runtime data under shared artifact roots outside the Git checkout.
- Changing data contracts, asset keys, current contract schema, or provider semantics unless a specific audit finding requires a separate proposal.

## Decisions

1. Use existing governance scripts as the audit backbone.

   Rationale: The repository already has scripts for quality coverage, maintainability metrics, compatibility governance, and architecture boundaries. Extending their output and documentation is lower risk than introducing a new audit framework.

   Alternative considered: Add a separate audit tool. This would duplicate existing logic and make CI harder to reason about.

2. Treat broad Ruff/Pyright excludes as tracked debt, not immediate failure.

   Rationale: Large migrated modules use pandas-heavy data workflows and provider SDK boundaries that are expensive to type all at once. The implementation should shrink exclusions module by module and keep the baseline guard against regression.

   Alternative considered: Enable strict type checking for all `src/`. That would create too many unrelated failures and slow down practical cleanup.

3. Start refactors at stable seams.

   Rationale: The largest files mix CLI parsing, IO, provider calls, health calculations, rendering, and orchestration. Safer first targets are pure helpers, rendering/reporting logic, argument parsing, path resolution, and state models, because these can be extracted with focused tests and minimal provider interaction.

   Alternative considered: Split files only by size. File length alone does not identify a stable responsibility boundary.

4. Keep compatibility lifecycle decisions explicit.

   Rationale: The project still serves downstream callers that may depend on `hkdata`, `hk_data_platform.*`, old provider modules, migration commands, and console-script aliases. Removing them safely requires evidence and a deprecation path.

   Alternative considered: Delete all compatibility layers now. That would simplify code but risks breaking downstream workflows without sufficient usage data.

5. Separate factual documentation updates from historical records.

   Rationale: README and `docs/README.md` should state the current supported surface. `docs/migration-plan.md` can keep history, while `docs/compatibility.md` records retained legacy surface and cleanup conditions.

   Alternative considered: Keep all migration detail in the root README. That makes onboarding harder and increases stale wording.

6. Use direct Chinese documentation style with consistent code-level English terms.

   Rationale: Terms such as CLI, provider, current contract, release, baseline, cache, artifacts, and workflow appear in code and commands. Keeping them consistent is clearer than inventing variable translations. Non-essential contrast phrases should be simplified.

   Alternative considered: Fully translate all technical terms. That would drift from command names and module names.

## Risks / Trade-offs

- Removing or moving compatibility code too early -> Use `docs/compatibility.md`, repo-local `rg`, tests, and downstream confirmation before deletion.
- Tightening Pyright on pandas/provider-heavy modules creates noisy local ignores -> Prefer typed boundary wrappers, small dataclasses, and local narrowing before adding ignores.
- Refactoring large operational modules can break production data workflows -> Start with behavior-preserving extractions and run focused tests plus existing CI commands after each step.
- Documentation may become too audit-heavy -> Keep root docs concise and put detailed lifecycle tables or snapshots in `docs/quality-governance.md` or a dedicated audit document.
- Baselines can normalize debt if updated too casually -> Require task-level rationale for any baseline update and favor improvements over neutral resets.
