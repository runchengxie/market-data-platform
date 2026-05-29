## ADDED Requirements

### Requirement: Repository Maintenance Inventory
The project SHALL provide a repo-wide maintenance inventory that classifies source files, scripts, generated artifacts, compatibility layers, migration-only entry points, tests, and documentation by current ownership and lifecycle status.

#### Scenario: Inventory captures current lifecycle status
- **WHEN** the maintainability audit is run or updated
- **THEN** the output identifies each audited area as active, compatibility, migration-only, archival, generated/cache, cleanup candidate, or needs-refactor
- **AND** each non-active classification includes evidence such as entry point usage, tests, documentation references, or cleanup criteria

#### Scenario: Generated and runtime artifacts are separated from source
- **WHEN** the audit inspects repository contents
- **THEN** generated files, cache directories, large report outputs, egg-info metadata, and runtime data artifacts are listed separately from maintained source code
- **AND** any tracked generated or runtime artifacts are flagged for removal, `.gitignore` coverage, or explicit retention rationale

### Requirement: Maintainability Hotspot Report
The project SHALL identify modules, functions, public facades, and import boundaries that create team-maintenance risk.

#### Scenario: Large modules and functions are reported
- **WHEN** maintainability metrics are collected
- **THEN** the report includes the largest files, largest functions, functions over 100 lines, functions over 250 lines, functions with 10 or more arguments, maximum file length, and maximum function length
- **AND** the report marks the highest-risk modules as refactor candidates before implementation work begins

#### Scenario: Architecture coupling is reviewed
- **WHEN** the audit evaluates module boundaries
- **THEN** it checks that core modules remain independent of CLI, provider, HK implementation, and release implementation boundaries
- **AND** public API facades do not export private helpers

### Requirement: Cleanup Decision Log
The project SHALL maintain a decision log for removal, archival, deprecation, and refactoring candidates found during the audit.

#### Scenario: Compatibility items have clear lifecycle decisions
- **WHEN** a compatibility package, command, import path, preset, or migration command is retained
- **THEN** the decision log records its replacement, risk, current status, audit evidence, and cleanup condition

#### Scenario: One-off migration functionality is not treated as new product surface
- **WHEN** a migration-only or one-off command is still present in the main CLI
- **THEN** the decision log states whether it remains supported, moves to an internal script, moves to archived documentation, or needs a deprecation path

### Requirement: Refactor Scope Control
The project SHALL decompose risky modules in small, behavior-preserving steps rather than mixing broad rewrites with cleanup.

#### Scenario: Refactor candidates have bounded tasks
- **WHEN** a large module or function is selected for refactoring
- **THEN** the work item identifies the extracted responsibility, expected destination module, required tests, and validation commands
- **AND** unrelated behavior changes are excluded from the same task
