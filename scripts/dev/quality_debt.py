from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
BASELINE_PATH = REPO_ROOT / "scripts" / "dev" / "quality_baseline.json"
DEFAULT_RUFF_SELECT = "E,F,I,UP,B,C4,RET,RUF100"
COMPLEXITY_RUFF_SELECT = "C90,PLR0911,PLR0912,PLR0913,PLR0915"
BASELINE_VERSION = 1
PROTECTED_INCLUDED_PATHS = (
    "src/market_data_platform/config_utils.py",
    "src/market_data_platform/data_provider_contracts.py",
    "src/market_data_platform/hk_depth/downloader.py",
    "src/market_data_platform/rqdata_cli_common.py",
    "src/market_data_platform/symbols.py",
)


def _python_files(src_root: Path = SRC_ROOT) -> list[Path]:
    return sorted(path for path in src_root.rglob("*.py") if path.is_file())


def _line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _load_pyproject(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    payload = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _is_excluded(path: Path, patterns: set[str], *, repo_root: Path = REPO_ROOT) -> bool:
    rel = path.relative_to(repo_root).as_posix()
    return any(_pattern_excludes_path(rel, pattern) for pattern in patterns)


def _pattern_excludes_path(path: str, pattern: str) -> bool:
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def _coverage_for(
    patterns: set[str],
    *,
    repo_root: Path = REPO_ROOT,
    src_root: Path | None = None,
) -> dict[str, int | float]:
    src_root = src_root or repo_root / "src"
    files = _python_files(src_root)
    line_counts = {path: _line_count(path) for path in files}
    excluded = [path for path in files if _is_excluded(path, patterns, repo_root=repo_root)]
    included = [path for path in files if path not in excluded]
    total_lines = sum(line_counts.values())
    included_lines = sum(line_counts[path] for path in included)
    return {
        "included_files": len(included),
        "excluded_files": len(excluded),
        "total_files": len(files),
        "included_lines": included_lines,
        "excluded_lines": total_lines - included_lines,
        "total_lines": total_lines,
        "included_pct": round((included_lines / total_lines * 100.0) if total_lines else 0.0, 1),
    }


def _tool_excludes(pyproject: Mapping[str, Any], tool_name: str) -> set[str]:
    tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), Mapping) else {}
    section = tool.get(tool_name) if isinstance(tool.get(tool_name), Mapping) else {}
    keys = ("exclude", "extend-exclude") if tool_name == "ruff" else ("exclude",)
    values: set[str] = set()
    for key in keys:
        for item in section.get(key) or []:
            values.add(str(item))
    return values


def _coverage_report(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    pyproject = _load_pyproject(repo_root)
    tools: dict[str, Any] = {}
    for tool_name in ("ruff", "pyright"):
        excludes = _tool_excludes(pyproject, tool_name)
        tools[tool_name] = {
            **_coverage_for(excludes, repo_root=repo_root),
            "excluded_patterns": sorted(excludes),
        }
    return {
        "version": BASELINE_VERSION,
        "source_root": "src",
        "tools": tools,
    }


def _load_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Quality baseline not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _baseline_snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": BASELINE_VERSION,
        "generated_on": date.today().isoformat(),
        "source_root": report["source_root"],
        "tools": report["tools"],
    }


def _write_baseline(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_baseline_snapshot(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _coverage_issues(
    report: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    current_tools = report.get("tools") if isinstance(report.get("tools"), Mapping) else {}
    baseline_tools = baseline.get("tools") if isinstance(baseline.get("tools"), Mapping) else {}
    for tool_name in ("ruff", "pyright"):
        current = current_tools.get(tool_name)
        expected = baseline_tools.get(tool_name)
        if not isinstance(current, Mapping) or not isinstance(expected, Mapping):
            issues.append(f"{tool_name}: missing current or baseline coverage payload")
            continue

        current_excludes = set(current.get("excluded_patterns") or [])
        expected_excludes = set(expected.get("excluded_patterns") or [])
        protected_excludes = sorted(
            path
            for path in PROTECTED_INCLUDED_PATHS
            if any(_pattern_excludes_path(path, pattern) for pattern in current_excludes)
        )
        if protected_excludes:
            issues.append(
                f"{tool_name}: protected paths must stay checked: "
                f"{', '.join(protected_excludes)}"
            )

        added_excludes = sorted(current_excludes - expected_excludes)
        if added_excludes:
            issues.append(f"{tool_name}: new excludes not in baseline: {', '.join(added_excludes)}")

        if int(current["included_lines"]) < int(expected["included_lines"]):
            issues.append(
                f"{tool_name}: checked lines decreased "
                f"({current['included_lines']} < {expected['included_lines']})"
            )
        if int(current["excluded_lines"]) > int(expected["excluded_lines"]):
            issues.append(
                f"{tool_name}: excluded lines increased "
                f"({current['excluded_lines']} > {expected['excluded_lines']})"
            )
    return issues


def _print_coverage(name: str, coverage: dict[str, int | float]) -> None:
    print(
        f"{name}: {coverage['included_files']}/{coverage['total_files']} files, "
        f"{coverage['included_lines']}/{coverage['total_lines']} lines checked "
        f"({coverage['included_pct']}%).",
        flush=True,
    )
    print(
        f"{name}: {coverage['excluded_files']} files and "
        f"{coverage['excluded_lines']} lines currently excluded.",
        flush=True,
    )


def _run_ruff_debt(*, select: str, title: str) -> int:
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "src",
        "--isolated",
        "--select",
        select,
        "--line-length",
        "100",
        "--target-version",
        "py311",
        "--statistics",
        "--exit-zero",
    ]
    print(f"\n{title}:", flush=True)
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    return int(result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report static-check coverage and non-blocking Ruff debt for src/."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable coverage and baseline-check output.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help=f"Quality baseline path. Default: {BASELINE_PATH.relative_to(REPO_ROOT)}",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Write the current coverage snapshot as the accepted quality baseline.",
    )
    parser.add_argument(
        "--check-baseline",
        action="store_true",
        help="Fail if static-check coverage regresses against the baseline.",
    )
    parser.add_argument(
        "--skip-ruff",
        action="store_true",
        help="Only report configured coverage; do not run the non-blocking Ruff debt scan.",
    )
    parser.add_argument(
        "--complexity",
        action="store_true",
        help=f"Run a non-blocking Ruff complexity scan ({COMPLEXITY_RUFF_SELECT}).",
    )
    parser.add_argument(
        "--ruff-select",
        default=DEFAULT_RUFF_SELECT,
        help=f"Rule selection for the Ruff debt scan. Default: {DEFAULT_RUFF_SELECT}",
    )
    parser.add_argument(
        "--complexity-select",
        default=COMPLEXITY_RUFF_SELECT,
        help=f"Rule selection for the complexity scan. Default: {COMPLEXITY_RUFF_SELECT}",
    )
    args = parser.parse_args(argv)

    baseline_path = args.baseline if args.baseline.is_absolute() else REPO_ROOT / args.baseline
    report = _coverage_report()
    baseline_issues: list[str] = []

    if args.write_baseline:
        _write_baseline(baseline_path, report)

    if args.check_baseline:
        baseline_issues = _coverage_issues(report, _load_baseline(baseline_path))

    if args.json:
        payload = {**report, "baseline_issues": baseline_issues}
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        print()
        return 1 if baseline_issues else 0

    for display_name, tool_name in (("Ruff", "ruff"), ("Pyright", "pyright")):
        _print_coverage(display_name, report["tools"][tool_name])

    if args.write_baseline:
        print(f"\nWrote quality baseline: {baseline_path.relative_to(REPO_ROOT)}", flush=True)

    if baseline_issues:
        print("\nQuality baseline regressions:", file=sys.stderr, flush=True)
        for issue in baseline_issues:
            print(f"- {issue}", file=sys.stderr, flush=True)
        return 1

    if not args.skip_ruff:
        _run_ruff_debt(select=args.ruff_select, title="Ruff debt scan")
    if args.complexity:
        _run_ruff_debt(select=args.complexity_select, title="Ruff complexity debt scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
