from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from .hk_asset_workflow_config import (
    DEFAULT_PACKAGE_PARTS,
    DEFAULT_PHASES,
    INSPECT_ASSETS,
    REFRESH_ASSETS,
    REPAIR_ASSETS,
)
from .hk_asset_workflow_paths import SnapshotBundle, Step
from .hk_asset_workflow_state import WorkflowPlan


@dataclass(frozen=True)
class WorkflowStepBuilders:
    refresh: Callable[..., list[Step]]
    inspect: Callable[..., list[Step]]
    repair: Callable[..., list[Step]]
    universe_refresh: Callable[..., Step]
    package: Callable[..., Step]
    release: Callable[..., Step]


def phase_selection(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.phase or DEFAULT_PHASES))


def selected_refresh_assets(args: argparse.Namespace) -> tuple[str, ...]:
    selected = list(dict.fromkeys(args.refresh_asset or REFRESH_ASSETS))
    if "etf_daily_clean" in selected and "etf_daily" not in selected:
        selected.insert(selected.index("etf_daily_clean"), "etf_daily")
    if (
        any(asset in selected for asset in ("etf_daily", "etf_daily_clean"))
        and "etf_instruments" not in selected
    ):
        insert_at = min(
            selected.index(asset)
            for asset in ("etf_daily", "etf_daily_clean")
            if asset in selected
        )
        selected.insert(insert_at, "etf_instruments")
    return tuple(dict.fromkeys(selected))


def selected_inspect_assets(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.inspect_asset or INSPECT_ASSETS))


def selected_parts(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.part or DEFAULT_PACKAGE_PARTS))


def selected_repair_assets(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.repair_asset or REPAIR_ASSETS))


def should_refresh_universe(
    args: argparse.Namespace,
    *,
    phases: tuple[str, ...],
    selected_mutating_assets: tuple[str, ...],
) -> bool:
    if not bool(getattr(args, "refresh_universe", True)):
        return False
    if args.no_repoint_latest:
        return False
    if not any(phase in phases for phase in ("refresh", "repair")):
        return False
    return "daily_clean" in selected_mutating_assets


def planned_bundle(
    current: SnapshotBundle,
    refreshed: SnapshotBundle,
    *,
    selected_refresh_assets: tuple[str, ...],
) -> SnapshotBundle:
    payload = current.__dict__.copy()
    mapping = {
        "instruments": "instruments_file",
        "etf_instruments": "etf_instruments_file",
        "daily": "daily_dir",
        "daily_clean": "daily_clean_dir",
        "etf_daily": "etf_daily_dir",
        "etf_daily_clean": "etf_daily_clean_dir",
        "valuation": "valuation_dir",
        "ex_factors": "ex_factors_dir",
        "dividends": "dividends_dir",
        "shares": "shares_dir",
        "industry_changes": "industry_changes_dir",
        "southbound": "southbound_dir",
    }
    for asset_name in selected_refresh_assets:
        field_name = mapping.get(asset_name)
        if field_name is not None:
            payload[field_name] = getattr(refreshed, field_name)
    return SnapshotBundle(**payload)


def build_workflow_plan(
    args: argparse.Namespace,
    *,
    phases: tuple[str, ...],
    current: SnapshotBundle,
    refreshed: SnapshotBundle,
    active_bundle: SnapshotBundle,
    builders: WorkflowStepBuilders,
) -> WorkflowPlan:
    selected_refresh = selected_refresh_assets(args)
    selected_repair = selected_repair_assets(args)
    planned_refresh_bundle = planned_bundle(
        current,
        refreshed,
        selected_refresh_assets=selected_refresh,
    )

    steps: list[Step] = []
    if "refresh" in phases:
        steps.extend(builders.refresh(args, current=current, refreshed=refreshed))
    if "inspect" in phases:
        inspect_bundle = active_bundle if "refresh" not in phases else planned_refresh_bundle
        steps.extend(builders.inspect(args, bundle=inspect_bundle))
    repair_steps: list[Step] = []
    if "repair" in phases:
        repair_bundle_current = active_bundle if "refresh" not in phases else planned_refresh_bundle
        repair_bundle_refreshed = planned_bundle(
            repair_bundle_current,
            refreshed,
            selected_refresh_assets=selected_repair,
        )
        repair_steps = builders.repair(
            args,
            current=repair_bundle_current,
            refreshed=repair_bundle_refreshed,
        )
        steps.extend(repair_steps)
        if args.repair_rerun_inspect and repair_steps:
            repaired_assets = tuple(
                dict.fromkeys(
                    step.asset_name
                    for step in repair_steps
                    if step.asset_name in INSPECT_ASSETS
                )
            )
            if args.repair_rerun_inspect_asset:
                selected_post_repair = set(args.repair_rerun_inspect_asset)
                repaired_assets = tuple(
                    asset for asset in repaired_assets if asset in selected_post_repair
                )
            if repaired_assets:
                steps.extend(
                    builders.inspect(
                        args,
                        bundle=repair_bundle_refreshed,
                        asset_names=repaired_assets,
                        inspection_stage="post_repair",
                    )
                )
    repair_assets_with_steps = tuple(
        dict.fromkeys(step.asset_name for step in repair_steps if step.asset_name)
    )
    selected_mutating_assets = tuple(
        dict.fromkeys(
            [
                *(selected_refresh if "refresh" in phases else ()),
                *(repair_assets_with_steps if "repair" in phases else ()),
            ]
        )
    )
    final_planned_bundle = planned_bundle(
        current,
        refreshed,
        selected_refresh_assets=selected_mutating_assets,
    )
    if should_refresh_universe(
        args,
        phases=phases,
        selected_mutating_assets=selected_mutating_assets,
    ):
        steps.append(builders.universe_refresh(args, bundle=final_planned_bundle))
    if "package" in phases:
        package_bundle = (
            active_bundle
            if not any(phase in phases for phase in ("refresh", "repair"))
            else final_planned_bundle
        )
        steps.append(builders.package(args, bundle=package_bundle))
    if "release" in phases:
        steps.append(builders.release(args))

    return WorkflowPlan(
        steps=steps,
        repair_steps=repair_steps,
        selected_mutating_assets=selected_mutating_assets,
        planned_bundle=final_planned_bundle,
    )
