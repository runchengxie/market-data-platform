from __future__ import annotations

from market_data_platform.release_tools import hk_asset_workflow as workflow


def _normalized_args(*argv: str):
    args = workflow.build_parser().parse_args(list(argv))
    workflow._normalize_workflow_args(args)
    return args


def test_hk_asset_workflow_package_dry_run(capsys):
    exit_code = workflow.main(
        [
            "--target-date",
            "2026-05-28",
            "--phase",
            "package",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Stage HK asset release parts" in output
    assert "market_data_platform.release_tools.package_assets" in output
    assert "Workflow complete: phases=package target_date=20260528" in output


def test_hk_asset_workflow_plan_adds_etf_clean_dependencies():
    args = _normalized_args(
        "--target-date",
        "20260528",
        "--phase",
        "refresh",
        "--refresh-asset",
        "etf_daily_clean",
        "--dry-run",
    )
    phases = workflow._phase_selection(args)
    current = workflow._current_snapshot_bundle()
    refreshed = workflow._refreshed_snapshot_bundle(args.target_date)

    plan = workflow._build_workflow_plan(
        args,
        phases=phases,
        current=current,
        refreshed=refreshed,
        active_bundle=current,
    )

    refresh_assets = [
        step.asset_name
        for step in plan.steps
        if step.phase == "refresh"
    ]
    assert refresh_assets == ["etf_instruments", "etf_daily", "etf_daily_clean"]
    assert plan.selected_mutating_assets == ("etf_instruments", "etf_daily", "etf_daily_clean")
