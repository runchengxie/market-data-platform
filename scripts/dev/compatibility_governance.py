#!/usr/bin/env python3
"""Check compatibility-surface lifecycle documentation and repo-local usage."""

from __future__ import annotations

import argparse
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "docs" / "compatibility.md"
REQUIRED_COLUMNS = (
    "兼容项",
    "当前用途",
    "风险",
    "推荐替代",
    "清理条件",
    "当前状态",
    "审计证据",
)
STABLE_CONSOLE_SCRIPTS = {"marketdata"}
SEARCH_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".sh"}
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "artifacts"}


@dataclass(frozen=True)
class CompatibilitySurface:
    label: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class UsageAudit:
    label: str
    source: int
    tests: int
    docs: int
    configs: int
    scripts: int
    other: int


EXPECTED_SURFACES: tuple[CompatibilitySurface, ...] = (
    CompatibilitySurface("hkdata CLI", ("hkdata",)),
    CompatibilitySurface("hk_data_platform.*", ("hk_data_platform",)),
    CompatibilitySurface(
        "market_data_platform.rqdata_cn / tushare_cn",
        ("market_data_platform.rqdata_cn", "market_data_platform.tushare_cn"),
    ),
    CompatibilitySurface("marketdata migration status", ("migration status",)),
    CompatibilitySurface("marketdata migration sync-hk-links", ("sync-hk-links",)),
    CompatibilitySurface(
        "marketdata migration import-cross-artifacts",
        ("import-cross-artifacts",),
    ),
    CompatibilitySurface("rqdata-hk-depth / rqdata-tick", ("rqdata-hk-depth", "rqdata-tick")),
    CompatibilitySurface("rqdata-hk-assets", ("rqdata-hk-assets",)),
    CompatibilitySurface("HK release presets", ("configs/presets/release",)),
)


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_inventory(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("|") and "兼容项" in line and "清理条件" in line
        ),
        None,
    )
    if header_index is None or header_index + 1 >= len(lines):
        return []

    headers = _split_table_row(lines[header_index])
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = _split_table_row(line)
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells, strict=True)))
    return rows


def _load_pyproject(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    payload = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _project_scripts(repo_root: Path = REPO_ROOT) -> set[str]:
    pyproject = _load_pyproject(repo_root)
    project = pyproject.get("project") if isinstance(pyproject.get("project"), Mapping) else {}
    scripts = project.get("scripts") if isinstance(project.get("scripts"), Mapping) else {}
    return {str(name) for name in scripts}


def inventory_issues(
    entries: Iterable[Mapping[str, str]],
    *,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    rows = list(entries)
    labels = {_plain_text(row.get("兼容项", "")) for row in rows}
    issues: list[str] = []
    for column in REQUIRED_COLUMNS:
        if any(column not in row for row in rows):
            issues.append(f"compatibility inventory is missing required column: {column}")
            break

    for surface in EXPECTED_SURFACES:
        if surface.label not in labels:
            issues.append(f"missing compatibility inventory entry: {surface.label}")

    for row in rows:
        label = row.get("兼容项", "<unknown>")
        for column in REQUIRED_COLUMNS:
            if not str(row.get(column, "")).strip():
                issues.append(f"{label}: missing {column}")

    inventory_text = "\n".join(labels)
    undocumented_scripts = sorted(
        script
        for script in _project_scripts(repo_root)
        if script not in STABLE_CONSOLE_SCRIPTS and script not in inventory_text
    )
    for script in undocumented_scripts:
        issues.append(f"console script lacks compatibility inventory entry: {script}")
    return issues


def _plain_text(value: str) -> str:
    return value.replace("`", "").strip()


def _classify_path(path: Path) -> str:
    first = path.parts[0] if path.parts else ""
    if first == "src":
        return "source"
    if first == "tests":
        return "tests"
    if first == "docs":
        return "docs"
    if first == "configs":
        return "configs"
    if first == "scripts":
        return "scripts"
    return "other"


def _candidate_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix not in SEARCH_SUFFIXES:
            continue
        relative = path.relative_to(repo_root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        files.append(relative)
    return sorted(files)


def audit_usage(repo_root: Path = REPO_ROOT) -> list[UsageAudit]:
    files = _candidate_files(repo_root)
    audits: list[UsageAudit] = []
    for surface in EXPECTED_SURFACES:
        counts = {
            "source": 0,
            "tests": 0,
            "docs": 0,
            "configs": 0,
            "scripts": 0,
            "other": 0,
        }
        for relative in files:
            text = (repo_root / relative).read_text(encoding="utf-8", errors="ignore")
            if not any(token in text for token in surface.tokens):
                continue
            counts[_classify_path(relative)] += 1
        audits.append(UsageAudit(label=surface.label, **counts))
    return audits


def build_report(
    repo_root: Path = REPO_ROOT,
    inventory_path: Path = INVENTORY_PATH,
) -> dict[str, Any]:
    entries = parse_inventory(inventory_path.read_text(encoding="utf-8"))
    issues = inventory_issues(entries, repo_root=repo_root)
    return {
        "inventory_path": str(inventory_path.relative_to(repo_root)),
        "entries": entries,
        "issues": issues,
        "usage": [asdict(item) for item in audit_usage(repo_root)],
    }


def format_text(report: Mapping[str, Any]) -> str:
    lines = [f"Compatibility inventory: {report['inventory_path']}"]
    issues = report.get("issues") or []
    if issues:
        lines.append("Issues:")
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("Inventory issues: 0")
    lines.extend(["", "Repo-local usage:"])
    for row in report.get("usage") or []:
        lines.append(
            "- {label}: src={source}, tests={tests}, docs={docs}, configs={configs}, "
            "scripts={scripts}, other={other}".format(**row)
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate compatibility lifecycle documentation and usage audits.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--check", action="store_true", help="Fail if inventory issues exist.")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root. Defaults to this checkout.",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=INVENTORY_PATH,
        help=f"Compatibility inventory path. Default: {INVENTORY_PATH.relative_to(REPO_ROOT)}",
    )
    args = parser.parse_args(argv)

    repo_root = args.root.resolve()
    inventory_path = args.inventory if args.inventory.is_absolute() else repo_root / args.inventory
    report = build_report(repo_root, inventory_path)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return 1 if args.check and report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
