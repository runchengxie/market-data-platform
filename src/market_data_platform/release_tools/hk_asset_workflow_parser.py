from __future__ import annotations

import argparse
from pathlib import Path

from .hk_asset_workflow_config import (
    AVAILABLE_PART_CHOICES,
    DEFAULT_DAILY_PATCH_LOOKBACK_DAYS,
    DEFAULT_DATED_PATCH_LOOKBACK_DAYS,
    INSPECT_ASSETS,
    REFRESH_ASSETS,
    REPAIR_ASSETS,
)
from .hk_asset_workflow_paths import ASSETS_ROOT, REPORTS_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Thin maintainer workflow for HK RQData refresh, inspect, package, and release."
        ),
    )
    parser.add_argument(
        "--phase",
        action="append",
        choices=["refresh", "inspect", "repair", "package", "release"],
        default=[],
        help="Workflow phase to run. Repeatable. Default: refresh, inspect, package.",
    )
    parser.add_argument(
        "--target-date",
        required=True,
        help="Target date in YYYYMMDD or YYYY-MM-DD.",
    )
    parser.add_argument(
        "--config",
        help="Optional config path or alias forwarded to RQData commands.",
    )
    parser.add_argument("--username", help="Optional RQData username override.")
    parser.add_argument("--password", help="Optional RQData password override.")
    parser.add_argument("--resume", action="store_true", help="Pass --resume to mirror commands.")
    parser.add_argument(
        "--refresh-mode",
        choices=["full", "patch"],
        default="full",
        help=(
            "Refresh strategy for supported assets. "
            "'full' re-mirrors the whole date range; "
            "'patch' only re-fetches a tail window then merges it into a refreshed snapshot. "
            "Default: full."
        ),
    )
    parser.add_argument(
        "--daily-patch-lookback-days",
        type=int,
        default=DEFAULT_DAILY_PATCH_LOOKBACK_DAYS,
        help=(
            "Calendar-day overlap to re-fetch before the current daily asset end date when "
            "--refresh-mode=patch. Default: 20."
        ),
    )
    parser.add_argument(
        "--dated-patch-lookback-days",
        type=int,
        default=DEFAULT_DATED_PATCH_LOOKBACK_DAYS,
        help=(
            "Calendar-day overlap to re-fetch before the current dated-asset end date when "
            "--refresh-mode=patch. Applies to valuation/ex_factors/dividends/shares. Default: 40."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    parser.add_argument(
        "--refresh-asset",
        action="append",
        choices=REFRESH_ASSETS,
        default=[],
        help="Only refresh selected asset(s). Repeatable.",
    )
    parser.add_argument(
        "--inspect-asset",
        action="append",
        choices=INSPECT_ASSETS,
        default=[],
        help="Only inspect selected asset(s). Repeatable.",
    )
    parser.add_argument(
        "--repair-asset",
        action="append",
        choices=REPAIR_ASSETS,
        default=[],
        help=(
            "Only repair selected asset(s). Repeatable. "
            "Default: daily/valuation/ex_factors/dividends/shares."
        ),
    )
    parser.add_argument(
        "--repair-source-report",
        type=Path,
        help=(
            "Structured workflow report used to source repair_candidates. "
            "Default: same path as --workflow-report."
        ),
    )
    parser.add_argument(
        "--repair-min-severity",
        default="warning",
        choices=["info", "warning", "error"],
        help="Minimum candidate severity to include in repair runs. Default: warning.",
    )
    parser.add_argument(
        "--repair-only-unresolved",
        action="store_true",
        help=(
            "When running --phase repair, only consume remaining unresolved candidates "
            "from a prior "
            "repair workflow report instead of the original inspect.repair_candidates."
        ),
    )
    parser.add_argument(
        "--no-repair-rerun-inspect",
        action="store_false",
        dest="repair_rerun_inspect",
        help="Do not automatically rerun inspect on repaired assets after --phase repair.",
    )
    parser.set_defaults(repair_rerun_inspect=True)
    parser.add_argument(
        "--repair-rerun-inspect-asset",
        action="append",
        choices=INSPECT_ASSETS,
        default=[],
        help=(
            "Limit automatic post-repair inspection to selected asset(s). Repeatable. "
            "Default: inspect every asset that had repair steps."
        ),
    )
    parser.add_argument(
        "--repair-post-inspect-skip-history",
        action="store_true",
        help=(
            "When rerunning inspection after repair, skip full-history checks. "
            "Useful for fast verification of target-date repairs before a later full audit."
        ),
    )
    parser.add_argument(
        "--part",
        action="append",
        choices=AVAILABLE_PART_CHOICES,
        default=[],
        help="Only stage or upload selected release part(s). Repeatable.",
    )
    parser.add_argument("--start-date", default="20000101", help="Start date for dated HK mirrors.")
    parser.add_argument(
        "--southbound-start-date",
        default="20141117",
        help="Start date for southbound mirrors.",
    )
    parser.add_argument(
        "--universe-by-date",
        type=Path,
        default=ASSETS_ROOT / "universe" / "hk_all_full_by_date.csv",
        help="Universe-by-date CSV used for full-market mirrors.",
    )
    parser.add_argument(
        "--southbound-by-date",
        type=Path,
        default=ASSETS_ROOT / "universe" / "hk_connect_full_by_date.csv",
        help="Universe-by-date CSV used for southbound mirrors.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_ROOT,
        help="Directory used for health report outputs.",
    )
    parser.add_argument(
        "--workflow-report",
        type=Path,
        help=(
            "Optional JSON report path for this workflow run. "
            "Default: artifacts/reports/hk_asset_refresh_<target_date>.json"
        ),
    )
    parser.add_argument(
        "--inspect-fail-on-severity",
        default="none",
        choices=["none", "info", "warning", "error"],
        help="Fail threshold forwarded to inspect-hk-asset-health. Default: none.",
    )
    parser.add_argument(
        "--gate-on-severity",
        default="warning",
        choices=["none", "info", "warning", "error"],
        help=(
            "Workflow gate threshold evaluated from inspect JSON summaries. "
            "When hit, deferred latest alias repoints stay blocked and downstream package/release "
            "steps are skipped. Default: warning."
        ),
    )
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="Do not add --include-history to inspect-hk-asset-health.",
    )
    parser.add_argument(
        "--valuation-history-tail-days",
        type=int,
        default=370,
        help=(
            "Limit valuation --include-history scans to the trailing N calendar days. "
            "Use 0 for full valuation history. Default: 370."
        ),
    )
    parser.add_argument(
        "--valuation-history-timeout-seconds",
        type=float,
        default=600.0,
        help=(
            "Stop valuation history scanning after this many seconds and mark the "
            "report truncated. "
            "Use 0 to disable the timeout. Default: 600."
        ),
    )
    parser.add_argument(
        "--valuation-history-progress-every-symbols",
        type=int,
        default=250,
        help="Print valuation history progress every N symbols. Use 0 to disable. Default: 250.",
    )
    parser.add_argument(
        "--prune-successful-patches",
        action="store_true",
        help=(
            "After successful patch/repair merges, delete the intermediate "
            "__patch/__repair directories "
            "created by this workflow run."
        ),
    )
    parser.add_argument(
        "--no-repoint-latest",
        action="store_true",
        help="Leave generic latest symlinks untouched after refresh.",
    )
    parser.add_argument(
        "--no-refresh-universe",
        action="store_false",
        dest="refresh_universe",
        help=(
            "Do not rebuild artifacts/assets/universe/hk_all_full_* after a "
            "daily/daily_clean refresh. "
            "By default, the workflow refreshes universe files after inspect gates pass."
        ),
    )
    parser.set_defaults(refresh_universe=True)
    parser.add_argument("--preset", default="hk_full", help="Preset forwarded to package_assets.")
    parser.add_argument(
        "--distribution-name",
        default="hk-full-rqdata",
        help="Distribution name used for package/release manifests.",
    )
    parser.add_argument("--package-dest", type=Path, help="Override staged package root.")
    parser.add_argument("--tar-dir", type=Path, help="Override tarball output directory.")
    parser.add_argument("--repo", help="GitHub repo in owner/name format for release upload.")
    parser.add_argument("--tag", help="Optional GitHub release tag override.")
    parser.add_argument("--title", help="Optional GitHub release title override.")
    parser.add_argument("--prerelease", action="store_true", help="Mark release as prerelease.")
    parser.add_argument("--draft", action="store_true", help="Create the GitHub release as draft.")
    parser.add_argument("--latest", action="store_true", help="Mark the GitHub release as latest.")
    parser.add_argument(
        "--clobber",
        action="store_true",
        help="Overwrite existing release assets if needed.",
    )
    return parser
