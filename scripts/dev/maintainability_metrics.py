#!/usr/bin/env python3
"""Collect maintainability metrics and enforce outlier baselines."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "scripts" / "dev" / "maintainability_baseline.json"
DEFAULT_ROOTS = ("src", "scripts", "tests")
DEFAULT_LIMIT = 15
PUBLIC_EXPORTS_PATH = Path("src/market_data_platform/hk_assets/_public_exports.py")
BASELINE_VERSION = 1


@dataclass(frozen=True)
class FileMetric:
    path: str
    lines: int


@dataclass(frozen=True)
class FunctionMetric:
    path: str
    name: str
    start_line: int
    end_line: int
    lines: int
    arguments: int


@dataclass(frozen=True)
class PublicFacadeMetric:
    path: str
    exports: int
    private_exports: int


@dataclass(frozen=True)
class Metrics:
    roots: list[str]
    python_files: int
    python_lines: int
    functions_over_100: int
    functions_over_250: int
    functions_over_500: int
    functions_with_10_plus_args: int
    max_file_lines: int
    max_function_lines: int
    max_argument_count: int
    public_facade: PublicFacadeMetric
    largest_files: list[FileMetric]
    largest_functions: list[FunctionMetric]

    def to_payload(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "version": BASELINE_VERSION,
            "thresholds": {
                "large_function_lines": 100,
                "very_large_function_lines": 250,
                "huge_function_lines": 500,
                "many_arguments": 10,
            },
        }


def _is_included_python_path(path: Path, roots: Sequence[str]) -> bool:
    if path.suffix != ".py":
        return False
    if any(part in {".git", ".venv", "__pycache__", "artifacts"} for part in path.parts):
        return False
    return bool(path.parts) and path.parts[0] in roots


def discover_python_files(
    repo_root: Path = REPO_ROOT,
    roots: Sequence[str] = DEFAULT_ROOTS,
) -> list[Path]:
    files: list[Path] = []
    for root_name in roots:
        root = repo_root / root_name
        if root.exists():
            files.extend(
                path
                for path in root.rglob("*.py")
                if _is_included_python_path(path.relative_to(repo_root), roots)
            )
    return sorted(files)


def _relative_path(repo_root: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _argument_count(arguments: ast.arguments) -> int:
    total = len(arguments.posonlyargs) + len(arguments.args) + len(arguments.kwonlyargs)
    if arguments.vararg is not None:
        total += 1
    if arguments.kwarg is not None:
        total += 1
    return total


def _function_metrics_for_file(repo_root: Path, path: Path, text: str) -> list[FunctionMetric]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    metrics: list[FunctionMetric] = []
    relative = _relative_path(repo_root, path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end_line = getattr(node, "end_lineno", None)
        if end_line is None:
            continue
        metrics.append(
            FunctionMetric(
                path=relative,
                name=node.name,
                start_line=node.lineno,
                end_line=end_line,
                lines=end_line - node.lineno + 1,
                arguments=_argument_count(node.args),
            )
        )
    return metrics


def _literal_names_from_assignment(tree: ast.Module, name: str) -> list[str]:
    for node in tree.body:
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value_node = node.value
        if value_node is None:
            continue
        value = ast.literal_eval(value_node)
        if isinstance(value, tuple | list):
            return [str(item) for item in value]
    return []


def public_facade_metric(repo_root: Path = REPO_ROOT) -> PublicFacadeMetric:
    path = repo_root / PUBLIC_EXPORTS_PATH
    if not path.exists():
        return PublicFacadeMetric(path=PUBLIC_EXPORTS_PATH.as_posix(), exports=0, private_exports=0)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exports = _literal_names_from_assignment(tree, "PUBLIC_API_EXPORTS")
    return PublicFacadeMetric(
        path=PUBLIC_EXPORTS_PATH.as_posix(),
        exports=len(exports),
        private_exports=sum(1 for name in exports if name.startswith("_")),
    )


def collect_metrics(
    repo_root: Path = REPO_ROOT,
    roots: Sequence[str] = DEFAULT_ROOTS,
    limit: int = DEFAULT_LIMIT,
) -> Metrics:
    files = discover_python_files(repo_root, roots)
    file_metrics: list[FileMetric] = []
    function_metrics: list[FunctionMetric] = []
    total_lines = 0

    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        line_count = len(text.splitlines())
        total_lines += line_count
        file_metrics.append(FileMetric(path=_relative_path(repo_root, path), lines=line_count))
        function_metrics.extend(_function_metrics_for_file(repo_root, path, text))

    largest_files = sorted(file_metrics, key=lambda item: item.lines, reverse=True)[:limit]
    largest_functions = sorted(
        function_metrics,
        key=lambda item: (item.lines, item.arguments),
        reverse=True,
    )[:limit]

    return Metrics(
        roots=list(roots),
        python_files=len(files),
        python_lines=total_lines,
        functions_over_100=sum(1 for item in function_metrics if item.lines > 100),
        functions_over_250=sum(1 for item in function_metrics if item.lines > 250),
        functions_over_500=sum(1 for item in function_metrics if item.lines > 500),
        functions_with_10_plus_args=sum(1 for item in function_metrics if item.arguments >= 10),
        max_file_lines=max((item.lines for item in file_metrics), default=0),
        max_function_lines=max((item.lines for item in function_metrics), default=0),
        max_argument_count=max((item.arguments for item in function_metrics), default=0),
        public_facade=public_facade_metric(repo_root),
        largest_files=largest_files,
        largest_functions=largest_functions,
    )


def _baseline_snapshot(metrics: Metrics) -> dict[str, Any]:
    return {
        "version": BASELINE_VERSION,
        "generated_on": date.today().isoformat(),
        "metrics": metrics.to_payload(),
    }


def write_baseline(path: Path, metrics: Metrics) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_baseline_snapshot(metrics), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_baseline(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _metric_value(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


def compare_to_baseline(metrics: Metrics, baseline: Mapping[str, Any]) -> list[str]:
    baseline_metrics = baseline.get("metrics")
    if not isinstance(baseline_metrics, Mapping):
        return ["maintainability: baseline is missing metrics"]

    current = metrics.to_payload()
    checked_keys = (
        "functions_over_100",
        "functions_over_250",
        "functions_over_500",
        "functions_with_10_plus_args",
        "max_file_lines",
        "max_function_lines",
        "max_argument_count",
    )
    issues: list[str] = []
    for key in checked_keys:
        current_value = _metric_value(current, key)
        baseline_value = _metric_value(baseline_metrics, key)
        if current_value > baseline_value:
            issues.append(f"{key} increased ({current_value} > {baseline_value})")

    current_facade = current.get("public_facade")
    baseline_facade = baseline_metrics.get("public_facade")
    if isinstance(current_facade, Mapping) and isinstance(baseline_facade, Mapping):
        for key in ("exports", "private_exports"):
            current_value = _metric_value(current_facade, key)
            baseline_value = _metric_value(baseline_facade, key)
            if current_value > baseline_value:
                issues.append(f"public_facade.{key} increased ({current_value} > {baseline_value})")
    return issues


def format_text(metrics: Metrics) -> str:
    rows = [
        ("python_files", metrics.python_files),
        ("python_lines", metrics.python_lines),
        ("functions_over_100", metrics.functions_over_100),
        ("functions_over_250", metrics.functions_over_250),
        ("functions_over_500", metrics.functions_over_500),
        ("functions_with_10_plus_args", metrics.functions_with_10_plus_args),
        ("max_file_lines", metrics.max_file_lines),
        ("max_function_lines", metrics.max_function_lines),
        ("max_argument_count", metrics.max_argument_count),
        ("public_facade_exports", metrics.public_facade.exports),
        ("private_public_facade_exports", metrics.public_facade.private_exports),
    ]
    lines = ["Maintainability metrics:"]
    lines.extend(f"- {name}: {value}" for name, value in rows)
    lines.extend(["", "Largest functions:"])
    lines.extend(
        f"- {item.lines} lines, {item.arguments} args: {item.path}:{item.start_line} {item.name}"
        for item in metrics.largest_functions
    )
    return "\n".join(lines)


def format_markdown(metrics: Metrics) -> str:
    lines = [
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Python files | {metrics.python_files} |",
        f"| Python lines | {metrics.python_lines} |",
        f"| Functions over 100 lines | {metrics.functions_over_100} |",
        f"| Functions over 250 lines | {metrics.functions_over_250} |",
        f"| Functions over 500 lines | {metrics.functions_over_500} |",
        f"| Functions with 10+ args | {metrics.functions_with_10_plus_args} |",
        f"| Max file lines | {metrics.max_file_lines} |",
        f"| Max function lines | {metrics.max_function_lines} |",
        f"| Max argument count | {metrics.max_argument_count} |",
        f"| Public facade exports | {metrics.public_facade.exports} |",
        f"| Private public facade exports | {metrics.public_facade.private_exports} |",
        "",
        "Largest functions:",
        "",
        "| Lines | Args | Function | Path |",
        "| ---: | ---: | --- | --- |",
    ]
    for item in metrics.largest_functions:
        lines.append(
            f"| {item.lines} | {item.arguments} | `{item.name}` | "
            f"`{item.path}:{item.start_line}` |"
        )
    return "\n".join(lines)


def _parse_roots(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(values) if values else DEFAULT_ROOTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect maintainability metrics and enforce baseline regressions.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print a markdown summary suitable for maintenance docs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of largest files/functions to include. Default: {DEFAULT_LIMIT}.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root. Defaults to this checkout.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        choices=DEFAULT_ROOTS,
        help="Root to include. May be repeated. Defaults to src, scripts, tests.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help=f"Maintainability baseline path. Default: {BASELINE_PATH.relative_to(REPO_ROOT)}",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Write the current metrics as the accepted maintainability baseline.",
    )
    parser.add_argument(
        "--check-baseline",
        action="store_true",
        help="Fail if maintainability metrics regress against the baseline.",
    )
    args = parser.parse_args(argv)

    repo_root = args.root.resolve()
    baseline_path = args.baseline if args.baseline.is_absolute() else repo_root / args.baseline
    metrics = collect_metrics(repo_root, _parse_roots(args.scope), max(args.limit, 0))
    issues: list[str] = []

    if args.write_baseline:
        write_baseline(baseline_path, metrics)
    if args.check_baseline:
        issues = compare_to_baseline(metrics, load_baseline(baseline_path))

    if args.json:
        print(json.dumps({**metrics.to_payload(), "baseline_issues": issues}, indent=2))
    elif args.markdown:
        print(format_markdown(metrics))
    else:
        print(format_text(metrics))

    if issues:
        for issue in issues:
            print(f"maintainability baseline regression: {issue}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
