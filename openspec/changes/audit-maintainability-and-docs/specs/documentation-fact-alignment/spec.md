## ADDED Requirements

### Requirement: Documentation Matches Current Project Behavior
The root README, `AGENTS.md`, and `docs/*.md` SHALL describe the current project surface accurately, including supported CLIs, Python packages, data boundaries, provider status, compatibility layers, migration state, tests, and quality governance.

#### Scenario: CLI and package names are accurate
- **WHEN** a documented command, console script, or Python package name appears in README, `AGENTS.md`, or `docs/*.md`
- **THEN** it matches an implemented entry point, documented compatibility layer, or explicitly archived historical reference

#### Scenario: Migration wording reflects current state
- **WHEN** documentation describes migrated HK assets, HK depth, CN providers, cross-repository boundaries, or historical import paths
- **THEN** it distinguishes active platform behavior from retained compatibility and archived migration history

### Requirement: Documentation Covers Tests And Governance
The documentation SHALL explain how maintainers validate the project locally and in CI.

#### Scenario: Test documentation is complete enough for maintainers
- **WHEN** a maintainer wants to verify a change
- **THEN** the docs identify the default test command, optional extras required for provider or DuckDB workflows, and the governance scripts that CI runs

#### Scenario: Documentation contracts are testable
- **WHEN** documentation references quality checks, compatibility entries, architecture boundaries, or commands
- **THEN** there is either an existing governance test/script covering it or a planned task to add one

### Requirement: Documentation Style Is Plain And Consistent
The documentation SHALL use direct, maintainable Chinese prose with consistent technical terminology.

#### Scenario: Avoidable contrast phrasing is simplified
- **WHEN** text uses patterns such as "不是...而是...", "目标不是...而是...", or similar contrast framing
- **THEN** the wording is simplified unless the contrast prevents a real misunderstanding

#### Scenario: Mixed terminology is intentional
- **WHEN** English terms such as CLI, provider, current contract, release, baseline, cache, or artifacts appear in Chinese documentation
- **THEN** their use is consistent across files and preferred over ad hoc translations only when they match code-level terminology

#### Scenario: Deep or translation-like wording is rewritten
- **WHEN** documentation contains abstract phrasing, stale migration-era caveats, or translation-like sentences
- **THEN** it is rewritten into concrete maintainer-facing instructions, facts, or lifecycle decisions

### Requirement: Documentation Has A Current Snapshot
The documentation SHALL include or link to a current project snapshot that summarizes supported functionality, known debt, and near-term cleanup priorities.

#### Scenario: Snapshot is updated after audit
- **WHEN** the maintainability and documentation audit is completed
- **THEN** maintainers can find a concise current-state summary covering active capabilities, compatibility surface, excluded quality coverage, largest maintainability hotspots, and cleanup priorities
