from __future__ import annotations

import json
import subprocess
from typing import Any

from market_data_platform.release_tools import hk_asset_workflow as workflow
from market_data_platform.release_tools import hk_asset_workflow_health as workflow_health
from market_data_platform.release_tools import hk_asset_workflow_repair as workflow_repair


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

    refresh_assets = [step.asset_name for step in plan.steps if step.phase == "refresh"]
    assert refresh_assets == ["etf_instruments", "etf_daily", "etf_daily_clean"]
    assert plan.selected_mutating_assets == ("etf_instruments", "etf_daily", "etf_daily_clean")


def test_hk_asset_workflow_nonfatal_step_skips_dependent_steps(monkeypatch, capsys):
    args = _normalized_args(
        "--target-date",
        "20260528",
        "--phase",
        "refresh",
    )
    current = workflow._current_snapshot_bundle()
    workflow_report: dict[str, Any] = {}
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, dry_run: bool):
        calls.append(command)
        return subprocess.CompletedProcess(command, workflow.PROVIDER_PERMISSION_EXIT_CODE)

    monkeypatch.setattr(workflow, "_run", fake_run)
    steps = [
        workflow.Step(
            phase="refresh",
            label="Mirror HK ETF daily",
            command=["fetch-etf"],
            asset_name="etf_daily",
            nonfatal_returncodes=(workflow.PROVIDER_PERMISSION_EXIT_CODE,),
        ),
        workflow.Step(
            phase="refresh",
            label="Build HK ETF daily clean layer",
            command=["clean-etf"],
            asset_name="etf_daily_clean",
            depends_on_assets=("etf_daily",),
        ),
    ]

    result = workflow._run_workflow_steps(
        args=args,
        phases=("refresh",),
        steps=steps,
        workflow_report=workflow_report,
        active_bundle=current,
    )

    output = capsys.readouterr().out

    assert result.gate_triggered is False
    assert calls == [["fetch-etf"]]
    assert "non-actionable provider/boundary gap" in output
    assert "skipped: dependency marked non-actionable: etf_daily" in output
    assert workflow_report["workflow"]["non_actionable_assets"][0]["asset_name"] == "etf_daily"
    assert workflow_report["workflow"]["skipped_steps"][0]["asset_name"] == "etf_daily_clean"


def test_hk_asset_workflow_health_analysis_extracts_candidates(tmp_path):
    report_path = tmp_path / "health.json"
    report_path.write_text(
        json.dumps(
            {
                "summary": {"history_issue_count": 2, "target_date": "20260528"},
                "quality_checks": [
                    {
                        "check": "daily_nonpositive_price",
                        "severity": "error",
                        "sample_symbols": ["00005.HK"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    analysis = workflow_health.load_health_report_analysis(report_path, asset_name="daily")

    assert analysis["quality"]["issue_count"] == 1
    assert analysis["quality"]["overall_severity"] == "error"
    assert analysis["repair_candidates"] == [
        {
            "symbol": "00005.HK",
            "trade_date": "20260528",
            "start_date": None,
            "end_date": None,
            "checks": ["daily_nonpositive_price"],
            "fields": [],
            "sources": ["quality_checks"],
            "reference_contexts": [],
            "errors": [],
            "max_severity": "error",
            "asset_name": "daily",
        }
    ]


def test_hk_asset_workflow_health_suppresses_raw_daily_when_clean_passes_gate():
    daily_step = workflow.Step(
        phase="inspect",
        label="Inspect daily",
        command=["inspect-daily"],
        asset_name="daily",
    )
    clean_step = workflow.Step(
        phase="inspect",
        label="Inspect daily clean",
        command=["inspect-daily-clean"],
        asset_name="daily_clean",
    )
    daily_summary = {
        "overall_severity": "error",
        "severity_counts": {"error": 1, "warning": 0, "info": 0},
        "report_path": "daily.json",
        "quality_checks": [
            {"check": "daily_price_bounds_violation", "severity": "error"},
        ],
    }
    clean_summary = {
        "overall_severity": "none",
        "severity_counts": {"error": 0, "warning": 0, "info": 0},
        "report_path": "daily_clean.json",
        "quality_checks": [],
    }
    workflow_report: dict[str, Any] = {}

    filtered = workflow_health.suppress_gate_hits_for_clean_daily_consumer_path(
        [(daily_step, daily_summary), (clean_step, clean_summary)],
        threshold="warning",
        report=workflow_report,
    )

    assert filtered == [(clean_step, clean_summary)]
    assert workflow_report["gate"]["suppressed_triggered_assets"] == [
        {
            "asset_name": "daily",
            "overall_severity": "error",
            "severity_counts": {"error": 1, "warning": 0, "info": 0},
            "report_path": "daily.json",
            "reason": (
                "raw daily price-bounds-only issues are tolerated when daily_clean passes the gate"
            ),
        }
    ]


def test_hk_asset_workflow_repair_prefers_remaining_candidates_and_clones():
    source_report = {
        "inspect": {
            "assets": {
                "daily": {
                    "repair_candidates": [{"symbol": "00001.HK", "max_severity": "error"}],
                    "post_repair_repair_candidates": [
                        {"symbol": "00002.HK", "max_severity": "warning"}
                    ],
                }
            }
        },
        "repair": {
            "remaining_candidates": {
                "assets": {
                    "daily": {
                        "repair_candidates": [{"symbol": "00003.HK", "max_severity": "warning"}]
                    }
                }
            }
        },
    }

    candidates, source_kind = workflow_repair.repair_source_candidates(
        source_report,
        asset_name="daily",
        only_unresolved=True,
    )
    candidates[0]["symbol"] = "CHANGED"

    assert source_kind == "repair.remaining_candidates"
    assert (
        source_report["repair"]["remaining_candidates"]["assets"]["daily"]["repair_candidates"][0][
            "symbol"
        ]
        == "00003.HK"
    )
