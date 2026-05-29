from __future__ import annotations

import json
from pathlib import Path

from scripts.dev import (
    architecture_governance,
    compatibility_governance,
    maintainability_metrics,
    quality_debt,
)


def test_quality_coverage_report_uses_configured_excludes(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "included.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "excluded.py").write_text("x = 1\nx = 2\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "typed_out.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.ruff]",
                'extend-exclude = ["src/excluded.py"]',
                "[tool.pyright]",
                'exclude = ["src/pkg"]',
            ]
        ),
        encoding="utf-8",
    )

    report = quality_debt._coverage_report(tmp_path)

    assert report["tools"]["ruff"]["included_files"] == 2
    assert report["tools"]["ruff"]["excluded_lines"] == 2
    assert report["tools"]["pyright"]["included_files"] == 2
    assert report["tools"]["pyright"]["excluded_patterns"] == ["src/pkg"]


def test_quality_json_output_shape(capsys) -> None:
    assert quality_debt.main(["--json", "--skip-ruff"]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert payload["source_root"] == "src"
    assert {"ruff", "pyright"} == set(payload["tools"])
    assert "included_lines" in payload["tools"]["ruff"]


def test_quality_baseline_flags_new_excludes() -> None:
    report = quality_debt._coverage_report()
    baseline = json.loads(json.dumps(report))
    report["tools"]["ruff"]["excluded_patterns"].append("src/new_excluded.py")

    issues = quality_debt._coverage_issues(report, baseline)

    assert any("new excludes" in issue for issue in issues)


def test_quality_baseline_flags_protected_excludes_even_when_baselined() -> None:
    report = quality_debt._coverage_report()
    baseline = json.loads(json.dumps(report))
    protected = quality_debt.PROTECTED_INCLUDED_PATHS[0]
    report["tools"]["pyright"]["excluded_patterns"].append(protected)
    baseline["tools"]["pyright"]["excluded_patterns"].append(protected)

    issues = quality_debt._coverage_issues(report, baseline)

    assert any("protected paths must stay checked" in issue for issue in issues)


def test_maintainability_metrics_collect_function_lengths_and_args(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "demo.py").write_text(
        "\n".join(
            [
                "def oversized(a, b, c, d, e, f, g, h, i, j):",
                "    return a",
            ]
        ),
        encoding="utf-8",
    )

    metrics = maintainability_metrics.collect_metrics(tmp_path, ("src",), limit=1)

    assert metrics.python_files == 1
    assert metrics.max_argument_count == 10
    assert metrics.functions_with_10_plus_args == 1


def test_maintainability_baseline_flags_regression(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "demo.py").write_text("def demo(a, b):\n    return a + b\n", encoding="utf-8")
    metrics = maintainability_metrics.collect_metrics(tmp_path, ("src",), limit=1)
    baseline = {"metrics": {**metrics.to_payload(), "max_argument_count": 1}}

    issues = maintainability_metrics.compare_to_baseline(metrics, baseline)

    assert any("max_argument_count increased" in issue for issue in issues)


def test_maintainability_baseline_accepts_legacy_metrics_payload(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "demo.py").write_text("def demo(a, b):\n    return a + b\n", encoding="utf-8")
    metrics = maintainability_metrics.collect_metrics(tmp_path, ("src",), limit=1)

    assert maintainability_metrics.compare_to_baseline(metrics, metrics.to_payload()) == []


def test_compatibility_inventory_is_complete() -> None:
    report = compatibility_governance.build_report()

    assert report["issues"] == []
    assert any(row["label"] == "hkdata CLI" for row in report["usage"])


def test_compatibility_inventory_parser_requires_expected_entries() -> None:
    entries = compatibility_governance.parse_inventory(
        "\n".join(
            [
                "| 兼容项 | 当前用途 | 风险 | 推荐替代 | 清理条件 | 当前状态 | 审计证据 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| `hkdata` CLI | use | risk | replace | cleanup | retained | audit |",
            ]
        )
    )

    issues = compatibility_governance.inventory_issues(entries)

    assert any("hk_data_platform.*" in issue for issue in issues)


def test_architecture_governance_current_boundaries() -> None:
    report = architecture_governance.build_report()

    assert report["issues"] == []


def test_architecture_governance_flags_private_test_facade_import(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "test_bad.py").write_text(
        "from market_data_platform.hk_assets import _private_helper\n",
        encoding="utf-8",
    )

    issues = architecture_governance.check_private_test_imports(tmp_path)

    assert issues
    assert issues[0].message.endswith("_private_helper")


def test_docs_link_maintenance_audit_and_avoid_contrast_filler() -> None:
    docs_root = Path("docs")
    docs_index = docs_root.joinpath("README.md").read_text(encoding="utf-8")

    assert "maintenance-audit.md" in docs_index

    checked_docs = [
        Path("README.md"),
        Path("AGENTS.md"),
        *sorted(docs_root.glob("*.md")),
    ]
    forbidden_phrases = ("不是", "而是", "目标不是")
    offenders = [
        str(path)
        for path in checked_docs
        if any(phrase in path.read_text(encoding="utf-8") for phrase in forbidden_phrases)
    ]

    assert offenders == []
