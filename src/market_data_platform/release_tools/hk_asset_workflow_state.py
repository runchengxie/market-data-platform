from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hk_asset_workflow_paths import SnapshotBundle, Step


@dataclass
class WorkflowPlan:
    steps: list[Step]
    repair_steps: list[Step]
    selected_mutating_assets: tuple[str, ...]
    planned_bundle: SnapshotBundle


@dataclass
class WorkflowExecutionResult:
    gate_triggered: bool
    successful_patch_merge_dirs: list[Path]


@dataclass
class WorkflowGateState:
    stage: str | None
    enabled: bool
    triggered: bool
    results: list[tuple[Step, dict[str, Any]]]
    remaining_inspect_steps: int
    pending_alias_steps: list[Step]
