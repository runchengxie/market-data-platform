#!/usr/bin/env python3
"""Check lightweight architecture and public API boundaries."""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_MODULES = (
    "src/market_data_platform/artifacts.py",
    "src/market_data_platform/contract.py",
    "src/market_data_platform/current_assets.py",
    "src/market_data_platform/data_provider_contracts.py",
    "src/market_data_platform/manifest.py",
    "src/market_data_platform/paths.py",
    "src/market_data_platform/registry.py",
    "src/market_data_platform/repo_paths.py",
)
BANNED_CORE_IMPORT_PREFIXES = (
    "market_data_platform.cli",
    "market_data_platform.hk_assets",
    "market_data_platform.hk_depth",
    "market_data_platform.providers",
    "market_data_platform.release_tools",
    "market_data_platform.rqdata_runtime",
)
PUBLIC_EXPORTS_PATH = Path("src/market_data_platform/hk_assets/_public_exports.py")
PUBLIC_API_PATH = Path("src/market_data_platform/hk_assets/public_api.py")


@dataclass(frozen=True)
class BoundaryIssue:
    path: str
    line: int
    message: str


def _relative_path(repo_root: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _imported_modules(tree: ast.Module) -> Iterable[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module


def check_core_import_boundaries(repo_root: Path = REPO_ROOT) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    for relative_text in CORE_MODULES:
        path = repo_root / relative_text
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, module in _imported_modules(tree):
            if module.startswith(BANNED_CORE_IMPORT_PREFIXES):
                issues.append(
                    BoundaryIssue(
                        path=relative_text,
                        line=line,
                        message=f"core module imports implementation boundary: {module}",
                    )
                )
    return issues


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


def _has_public_api_all_assignment(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        has_all_target = any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
        if not has_all_target:
            continue
        value = node.value
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "list"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == "PUBLIC_API_EXPORTS"
        )
    return False


def check_public_api_exports(repo_root: Path = REPO_ROOT) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    exports_path = repo_root / PUBLIC_EXPORTS_PATH
    public_api_path = repo_root / PUBLIC_API_PATH
    if not exports_path.exists():
        issues.append(BoundaryIssue(str(PUBLIC_EXPORTS_PATH), 1, "public exports file missing"))
        return issues

    exports_tree = ast.parse(exports_path.read_text(encoding="utf-8"))
    exports = _literal_names_from_assignment(exports_tree, "PUBLIC_API_EXPORTS")
    for name in exports:
        if name.startswith("_"):
            issues.append(
                BoundaryIssue(
                    str(PUBLIC_EXPORTS_PATH),
                    1,
                    f"private helper appears in public exports: {name}",
                )
            )

    if not public_api_path.exists():
        issues.append(BoundaryIssue(str(PUBLIC_API_PATH), 1, "public_api.py missing"))
    else:
        public_api_tree = ast.parse(public_api_path.read_text(encoding="utf-8"))
        if not _has_public_api_all_assignment(public_api_tree):
            issues.append(
                BoundaryIssue(
                    str(PUBLIC_API_PATH),
                    1,
                    "__all__ must be derived from PUBLIC_API_EXPORTS",
                )
            )
    return issues


def check_private_test_imports(repo_root: Path = REPO_ROOT) -> list[BoundaryIssue]:
    issues: list[BoundaryIssue] = []
    tests_root = repo_root / "tests"
    if not tests_root.exists():
        return issues
    for path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module not in {
                "market_data_platform.hk_assets",
                "market_data_platform.hk_assets.public_api",
            }:
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    issues.append(
                        BoundaryIssue(
                            path=_relative_path(repo_root, path),
                            line=node.lineno,
                            message=(
                                "test imports private helper through public facade: "
                                f"{alias.name}"
                            ),
                        )
                    )
    return issues


def build_report(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    issues = [
        *check_core_import_boundaries(repo_root),
        *check_public_api_exports(repo_root),
        *check_private_test_imports(repo_root),
    ]
    return {"issues": [asdict(issue) for issue in issues]}


def format_text(report: dict[str, Any]) -> str:
    issues = report["issues"]
    if not issues:
        return "Architecture governance issues: 0"
    lines = ["Architecture governance issues:"]
    lines.extend(
        f"- {issue['path']}:{issue['line']} {issue['message']}"
        for issue in issues
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate architecture dependency and public API boundaries.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--check", action="store_true", help="Fail if boundary issues exist.")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root. Defaults to this checkout.",
    )
    args = parser.parse_args(argv)

    report = build_report(args.root.resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    return 1 if args.check and report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
