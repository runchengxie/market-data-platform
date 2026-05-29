## ADDED Requirements

### Requirement: Static Check Coverage Baseline
The project SHALL report Ruff and Pyright source coverage and prevent new broad exclusions from entering the repository without an explicit baseline update and rationale.

#### Scenario: Coverage report reflects configured excludes
- **WHEN** quality coverage is reported
- **THEN** the report includes checked files, checked lines, excluded files, excluded lines, total source files, total source lines, percentage checked, and configured exclude patterns for Ruff and Pyright

#### Scenario: Coverage cannot regress silently
- **WHEN** CI or a local maintainer runs the baseline check
- **THEN** the check fails if checked lines decrease, excluded lines increase, new exclude patterns appear, or protected paths become excluded

### Requirement: Incremental Lint And Type Tightening
The project SHALL reduce Ruff and Pyright debt by moving selected modules from directory-level exclusions to checked coverage or to narrowly scoped per-file/per-rule exceptions with reasons.

#### Scenario: A module is brought under Ruff
- **WHEN** a previously excluded module is added to Ruff coverage
- **THEN** import sorting, syntax/style, upgrade, bugbear, comprehension, return, and unused-noqa checks apply through the repository configuration
- **AND** remaining intentional exceptions are represented as narrow ignores rather than a broad directory exclusion

#### Scenario: A module is brought under Pyright
- **WHEN** a previously excluded module is added to Pyright coverage
- **THEN** its public functions, CLI boundaries, provider adapters, and data model helpers have enough annotations or local narrowing to pass basic type checking
- **AND** unavoidable pandas/provider typing gaps are isolated with local comments or typed wrappers

### Requirement: Maintainability Baseline
The project SHALL track maintainability metrics and fail when large-function, many-argument, public-facade, or maximum-size metrics regress.

#### Scenario: Maintainability metrics are checked
- **WHEN** the maintainability baseline check runs
- **THEN** it compares current metrics to the accepted baseline for large functions, very large functions, argument count, max file lines, max function lines, and public facade exports

#### Scenario: Refactors improve or preserve the baseline
- **WHEN** a cleanup or refactor task is completed
- **THEN** maintainability metrics do not regress
- **AND** any intentional baseline update includes a reason in the relevant task or documentation

### Requirement: CI Documents Supported Checks
The project SHALL keep CI and local documentation aligned on the supported quality commands.

#### Scenario: Documentation lists current required checks
- **WHEN** maintainers read `README.md`, `AGENTS.md`, or `docs/quality-governance.md`
- **THEN** they can find the current pytest, Ruff, Pyright, quality coverage baseline, maintainability baseline, compatibility governance, and architecture governance commands

#### Scenario: Optional provider checks are explicit
- **WHEN** a workflow needs provider-specific dependencies or credentials
- **THEN** documentation identifies the required optional extras and environment variables without implying that secret-backed provider runs are part of default CI
