from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_RUFF_SELECT = "E,F,I,UP,B,C4,RET,RUF100"


def _python_files() -> list[Path]:
    return sorted(path for path in SRC_ROOT.rglob("*.py") if path.is_file())


def _line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _load_pyproject() -> dict:
    payload = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _is_excluded(path: Path, patterns: set[str]) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(rel == pattern or rel.startswith(pattern.rstrip("/") + "/") for pattern in patterns)


def _coverage_for(patterns: set[str]) -> dict[str, int | float]:
    files = _python_files()
    line_counts = {path: _line_count(path) for path in files}
    excluded = [path for path in files if _is_excluded(path, patterns)]
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


def _tool_excludes(pyproject: dict, tool_name: str) -> set[str]:
    tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
    section = tool.get(tool_name) if isinstance(tool.get(tool_name), dict) else {}
    key = "extend-exclude" if tool_name == "ruff" else "exclude"
    values = section.get(key) or []
    return {str(item) for item in values}


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


def _run_ruff_debt(*, select: str) -> int:
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
    print("\nRuff debt scan:", flush=True)
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    return int(result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report static-check coverage and non-blocking Ruff debt for src/."
    )
    parser.add_argument(
        "--skip-ruff",
        action="store_true",
        help="Only report configured coverage; do not run the non-blocking Ruff debt scan.",
    )
    parser.add_argument(
        "--ruff-select",
        default=DEFAULT_RUFF_SELECT,
        help=f"Rule selection for the Ruff debt scan. Default: {DEFAULT_RUFF_SELECT}",
    )
    args = parser.parse_args(argv)

    pyproject = _load_pyproject()
    _print_coverage("Ruff", _coverage_for(_tool_excludes(pyproject, "ruff")))
    _print_coverage("Pyright", _coverage_for(_tool_excludes(pyproject, "pyright")))

    if not args.skip_ruff:
        _run_ruff_debt(select=args.ruff_select)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
