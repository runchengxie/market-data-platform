from __future__ import annotations

import filecmp
import fnmatch
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from market_data_platform.contract import build_current_contract, write_current_contract
from market_data_platform.paths import (
    current_contract_path,
    dataset_registry_path,
    resolve_artifacts_root,
)
from market_data_platform.registry import write_combined_dataset_registry

HK_REFRESH_ASSET_CHOICES = (
    "instruments",
    "etf_instruments",
    "daily",
    "daily_clean",
    "etf_daily",
    "etf_daily_clean",
    "valuation",
    "ex_factors",
    "dividends",
    "shares",
    "industry_changes",
    "southbound",
)

HK_INSPECT_ASSET_CHOICES = (
    "daily",
    "daily_clean",
    "valuation",
    "ex_factors",
    "dividends",
    "shares",
    "industry_changes",
    "southbound",
)

HK_DEFAULT_INTRADAY_FREQUENCY = "5m"
HK_DEFAULT_INTRADAY_BATCH_SIZE = 50
HK_DEFAULT_PIT_PATCH_START_QUARTER = "2024q4"
HK_DEFAULT_PIT_PATCH_END_QUARTER = "2026q1"
HK_DEFAULT_FINANCIAL_START_QUARTER = "2000q1"
HK_DEFAULT_FINANCIAL_END_QUARTER = "2026q1"
HK_DEPTH_DEFAULT_BATCH_SIZE = 5

_TRANSITION_LINKS = (
    (("assets", "rqdata"), ("assets", "rqdata")),
    (("assets", "style"), ("assets", "style")),
    (("assets", "universe"), ("assets", "universe")),
    (("metadata", "current_assets"), ("metadata", "current_assets")),
)

_CROSS_PLATFORM_ARTIFACT_DIRS = (
    ("asset", Path("assets") / "rqdata"),
    ("asset", Path("assets") / "style"),
    ("asset", Path("assets") / "universe"),
    ("metadata", Path("metadata")),
    ("intraday_cache", Path("cache") / "intraday"),
    ("release", Path("releases")),
)

_CROSS_PLATFORM_REPORT_PATTERNS = (
    "reports/broken_current_symlinks_*.json",
    "reports/health_logs/*",
    "reports/hk_*_health*.json",
    "reports/hk_*health*.json",
    "reports/hk_asset_*.json",
    "reports/hk_data_asset_audit*.json",
    "reports/repair_inputs/*",
)

_CROSS_PLATFORM_SKIP_NAMES = {".gitkeep", ".DS_Store"}


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _platform_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _link_status(link_path: Path, target_path: Path) -> str:
    if link_path.is_symlink():
        if link_path.resolve(strict=False) == target_path.resolve(strict=False):
            return "ok"
        return "updated"
    if link_path.exists():
        return "blocked_non_symlink"
    return "created"


def _sync_hk_transition_registry_file(
    root: Path,
    workspace: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    source = dataset_registry_path(root)
    if not source.exists():
        return None
    target = workspace / "cross-sectional-trees" / "artifacts" / "metadata" / "dataset_registry.csv"
    if target.is_symlink() and target.resolve(strict=False) == source.resolve(strict=False):
        return {"file": str(target), "source": str(source), "status": "ok"}
    if target.exists() and not target.is_symlink() and target.read_bytes() == source.read_bytes():
        return {"file": str(target), "source": str(source), "status": "ok"}
    status = "updated" if target.exists() or target.is_symlink() else "created"
    if dry_run:
        return {"file": str(target), "source": str(source), "status": f"dry_run_{status}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    shutil.copy2(source, target)
    return {"file": str(target), "source": str(source), "status": status}


def sync_hk_transition_links(
    artifacts_root: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Point transition HK backend asset links at the platform artifacts root."""

    root = resolve_artifacts_root(artifacts_root)
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    cstree_artifacts = workspace / "cross-sectional-trees" / "artifacts"
    rows: list[dict[str, Any]] = []
    blockers: list[Path] = []

    for link_parts, target_parts in _TRANSITION_LINKS:
        link_path = cstree_artifacts.joinpath(*link_parts)
        target_path = root.joinpath(*target_parts)
        status = _link_status(link_path, target_path)
        row = {
            "link": str(link_path),
            "target": str(target_path),
            "status": "dry_run_" + status if dry_run and status != "ok" else status,
        }
        rows.append(row)
        if status == "blocked_non_symlink":
            blockers.append(link_path)
            continue
        if dry_run or status == "ok":
            continue
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(target_path, target_is_directory=target_path.is_dir())

    registry_row = _sync_hk_transition_registry_file(root, workspace, dry_run=dry_run)
    if registry_row is not None:
        rows.append(registry_row)

    if blockers:
        blocked = ", ".join(str(path) for path in blockers)
        raise RuntimeError(f"Refusing to replace non-symlink transition paths: {blocked}")
    return rows


def _default_cross_artifacts_root(workspace_root: str | Path | None = None) -> Path:
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    return workspace / "cross-sectional-trees" / "artifacts"


def _iter_regular_artifact_files(base: Path, root: Path) -> Sequence[Path]:
    if not base.exists() or base.is_symlink():
        return []
    if base.is_file():
        candidates = [base]
    else:
        candidates = sorted(path for path in base.rglob("*") if path.is_file())
    return [
        path
        for path in candidates
        if not path.is_symlink()
        and path.name not in _CROSS_PLATFORM_SKIP_NAMES
        and "__pycache__" not in path.relative_to(root).parts
    ]


def _is_platform_report(relative_path: str) -> bool:
    return any(
        fnmatch.fnmatchcase(relative_path, pattern)
        for pattern in _CROSS_PLATFORM_REPORT_PATTERNS
    )


def _collect_cross_platform_artifacts(cross_root: Path) -> list[tuple[str, Path]]:
    selected: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for category, relative_dir in _CROSS_PLATFORM_ARTIFACT_DIRS:
        for source in _iter_regular_artifact_files(cross_root / relative_dir, cross_root):
            resolved = source.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            selected.append((category, source))

    reports_root = cross_root / "reports"
    for source in _iter_regular_artifact_files(reports_root, cross_root):
        relative_path = source.relative_to(cross_root).as_posix()
        if not _is_platform_report(relative_path):
            continue
        resolved = source.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        selected.append(("report", source))
    return sorted(selected, key=lambda item: item[1].relative_to(cross_root).as_posix())


def _same_file_contents(source: Path, target: Path) -> bool:
    try:
        return filecmp.cmp(source, target, shallow=False)
    except OSError:
        return False


def _target_import_status(
    source: Path,
    target: Path,
    *,
    dry_run: bool,
    overwrite: bool,
) -> str:
    if target.is_symlink():
        if target.exists() and _same_file_contents(source, target):
            return "exists_same"
        return "blocked_target_symlink"
    if target.is_dir():
        return "blocked_target_directory"
    if target.exists():
        if _same_file_contents(source, target):
            return "exists_same"
        if not overwrite:
            return "exists_different"
        return "dry_run_overwrite" if dry_run else "overwritten"
    return "dry_run_copy" if dry_run else "copied"


def _summarize_import_rows(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        summary[status] = summary.get(status, 0) + 1
    return dict(sorted(summary.items()))


def import_cross_platform_artifacts(
    artifacts_root: str | Path | None = None,
    *,
    cross_artifacts_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Copy platform-owned artifacts out of cross-sectional-trees.

    This intentionally excludes research runs, sweeps, live run outputs, exports,
    benchmark attribution reports, and slippage calibration reports.
    """

    root = resolve_artifacts_root(artifacts_root)
    cross_root = (
        Path(cross_artifacts_root).expanduser().resolve()
        if cross_artifacts_root
        else _default_cross_artifacts_root(workspace_root)
    )
    rows: list[dict[str, Any]] = []
    for category, source in _collect_cross_platform_artifacts(cross_root):
        relative_path = source.relative_to(cross_root)
        target = root / relative_path
        status = _target_import_status(source, target, dry_run=dry_run, overwrite=overwrite)
        if status in {"copied", "overwritten"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        rows.append(
            {
                "category": category,
                "relative_path": relative_path.as_posix(),
                "source": str(source),
                "target": str(target),
                "bytes": source.stat().st_size,
                "status": status,
            }
        )

    payload: dict[str, Any] = {
        "source_artifacts_root": str(cross_root),
        "target_artifacts_root": str(root),
        "dry_run": dry_run,
        "overwrite": overwrite,
        "summary": _summarize_import_rows(rows),
        "items": rows,
    }
    if not dry_run:
        timestamp = datetime.now(UTC)
        generated_at = timestamp.isoformat()
        manifest = root / "metadata" / "migration" / (
            f"cross_artifacts_import_{timestamp.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        payload["generated_at"] = generated_at
        payload["manifest"] = str(manifest)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _checkout_python(repo_root: Path) -> Path:
    candidates = (
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "Scripts" / "python.exe",
    )
    return next(
        (candidate for candidate in candidates if candidate.is_file()),
        Path(sys.executable),
    )


def _backend_environment(repo_root: Path, artifacts_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    source_root = repo_root / "src"
    existing = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = f"{source_root}{os.pathsep}{existing}" if existing else str(source_root)
    env["DATA_PLATFORM_ROOT"] = str(artifacts_root)
    env["HK_DATA_PLATFORM_ROOT"] = str(artifacts_root)
    return env


def _workflow_environment(repo_root: Path, artifacts_root: Path) -> dict[str, str]:
    return _backend_environment(repo_root, artifacts_root)


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _platform_relative(root: Path, *parts: str) -> Path:
    return root.joinpath(*parts)


def _hk_assets_command(platform_repo: Path, *args: str) -> list[str]:
    return [
        str(_checkout_python(platform_repo)),
        "-m",
        "market_data_platform.cli",
        "rqdata",
        "hk-assets",
        "--",
        *args,
    ]


def _depth_command(*args: str) -> list[str]:
    python = _checkout_python(_platform_repo_root())
    return [str(python), "-m", "market_data_platform.hk_depth.cli", *args]


def _run_command(
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    dry_run: bool,
) -> subprocess.CompletedProcess[Any]:
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    return runner(command, check=False, cwd=str(cwd), env=env)


def _count_text_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _create_relative_symlink(target: Path, link: Path, *, dry_run: bool = False) -> dict[str, str]:
    target = target.resolve(strict=False)
    link = link.expanduser()
    if not link.is_absolute():
        link = link.resolve()
    status = "ok"
    if link.is_symlink():
        if link.resolve(strict=False) != target:
            status = "updated"
    elif link.exists():
        raise RuntimeError(f"Refusing to replace non-symlink alias: {link}")
    else:
        status = "created"
    if dry_run:
        return {"alias": str(link), "target": str(target), "status": f"dry_run_{status}"}
    if status != "ok":
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        relative_target = os.path.relpath(target, start=link.parent)
        link.symlink_to(relative_target, target_is_directory=target.is_dir())
    return {"alias": str(link), "target": str(target), "status": status}


def rebuild_hk_current_contract(
    artifacts_root: str | Path | None = None,
    *,
    target_date: str,
    generated_by: str = "marketdata rqdata refresh-hk-current",
) -> dict[str, str]:
    root = resolve_artifacts_root(artifacts_root)
    output = current_contract_path(root, market="hk")
    payload = build_current_contract(
        root,
        market="hk",
        provider="rqdata",
        generated_by=generated_by,
        target_date=target_date,
    )
    payload = dict(payload)
    payload["contract"] = dict(payload.get("contract") or {})
    payload["contract"]["contract_path"] = str(output)
    write_current_contract(output, payload)

    contracts: list[dict[str, Any]] = [payload]
    cn_contract = current_contract_path(root, market="cn")
    if cn_contract.exists():
        loaded = json.loads(cn_contract.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            contracts.append(loaded)
    registry_output = dataset_registry_path(root)
    write_combined_dataset_registry(registry_output, contracts)
    return {
        "current_contract": str(output),
        "dataset_registry": str(registry_output),
    }


def _rebuild_and_sync_hk_current_contract(
    root: Path,
    *,
    target_date: str,
    generated_by: str,
    workspace: Path,
) -> dict[str, str]:
    paths = rebuild_hk_current_contract(
        root,
        target_date=target_date,
        generated_by=generated_by,
    )
    registry_row = _sync_hk_transition_registry_file(root, workspace, dry_run=False)
    if registry_row is not None:
        paths["transition_dataset_registry"] = str(registry_row["file"])
    return paths


def run_hk_current_refresh(
    *,
    artifacts_root: str | Path | None = None,
    target_date: str,
    refresh_assets: Sequence[str] = (),
    inspect_assets: Sequence[str] = (),
    refresh_mode: str = "patch",
    daily_patch_lookback_days: int = 20,
    dated_patch_lookback_days: int = 40,
    gate_on_severity: str = "warning",
    inspect_fail_on_severity: str = "none",
    resume: bool = True,
    skip_history: bool = False,
    no_refresh_universe: bool = False,
    config: str | None = None,
    workflow_report: str | Path | None = None,
    dry_run: bool = False,
    sync_transition_links: bool = True,
    rebuild_contract: bool = True,
    workspace_root: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    platform_repo = _platform_repo_root()
    links = (
        sync_hk_transition_links(root, workspace_root=workspace, dry_run=dry_run)
        if sync_transition_links
        else []
    )

    command: list[str] = [
        str(_checkout_python(platform_repo)),
        "-m",
        "market_data_platform.release_tools.hk_asset_workflow",
        "--phase",
        "refresh",
        "--phase",
        "inspect",
        "--target-date",
        target_date,
        "--refresh-mode",
        refresh_mode,
        "--gate-on-severity",
        gate_on_severity,
        "--inspect-fail-on-severity",
        inspect_fail_on_severity,
        "--daily-patch-lookback-days",
        str(daily_patch_lookback_days),
        "--dated-patch-lookback-days",
        str(dated_patch_lookback_days),
    ]
    if resume:
        command.append("--resume")
    if skip_history:
        command.append("--skip-history")
    if no_refresh_universe:
        command.append("--no-refresh-universe")
    if dry_run:
        command.append("--dry-run")
    if config:
        command.extend(["--config", config])
    if workflow_report is not None:
        command.extend(["--workflow-report", str(workflow_report)])
    for asset in refresh_assets:
        command.extend(["--refresh-asset", str(asset)])
    for asset in inspect_assets:
        command.extend(["--inspect-asset", str(asset)])

    completed = runner(
        command,
        check=False,
        cwd=str(platform_repo),
        env=_workflow_environment(platform_repo, root),
    )
    returncode = int(completed.returncode)
    contract_paths: dict[str, str] = {}
    if returncode == 0 and rebuild_contract and not dry_run:
        contract_paths = _rebuild_and_sync_hk_current_contract(
            root,
            target_date=target_date,
            generated_by="marketdata rqdata refresh-hk-current",
            workspace=workspace,
        )

    return {
        "artifacts_root": str(root),
        "command": command,
        "returncode": returncode,
        "transition_links": links,
        **contract_paths,
    }


def run_hk_current_health(
    *,
    artifacts_root: str | Path | None = None,
    target_date: str | None = None,
    assets: Sequence[str] = (),
    fail_on_severity: str = "none",
    output: str | Path | None = None,
    dry_run: bool = False,
    sync_transition_links: bool = True,
    workspace_root: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    platform_repo = _platform_repo_root()
    links = (
        sync_hk_transition_links(root, workspace_root=workspace, dry_run=dry_run)
        if sync_transition_links
        else []
    )
    target_token = str(target_date or "").replace("-", "").strip()
    output_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else _platform_relative(
            root,
            "reports",
            f"hk_current_health_{target_token or 'current'}_platform.json",
        )
    )
    command = _hk_assets_command(
        platform_repo,
        "inspect-hk-current-health",
        "--artifacts-root",
        str(root),
        "--format",
        "json",
        "--out",
        str(output_path),
        "--fail-on-severity",
        fail_on_severity,
    )
    if target_token:
        command.extend(["--target-date", target_token])
    for asset in assets:
        command.extend(["--asset", str(asset)])

    completed = _run_command(
        runner,
        command,
        cwd=platform_repo,
        env=_workflow_environment(platform_repo, root),
        dry_run=dry_run,
    )
    return {
        "artifacts_root": str(root),
        "command": command,
        "returncode": int(completed.returncode),
        "transition_links": links,
        "health_report": str(output_path),
    }


def run_hk_intraday_refresh(
    *,
    artifacts_root: str | Path | None = None,
    start_date: str,
    end_date: str,
    frequency: str = HK_DEFAULT_INTRADAY_FREQUENCY,
    symbols_file: str | Path | None = None,
    batch_size: int = HK_DEFAULT_INTRADAY_BATCH_SIZE,
    inspect_fail_on_severity: str = "error",
    resume: bool = True,
    verify_sampled_segments: int = 0,
    verify_full_asset: bool = False,
    config: str | None = None,
    dry_run: bool = False,
    sync_transition_links: bool = True,
    rebuild_contract: bool = True,
    workspace_root: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    platform_repo = _platform_repo_root()
    links = (
        sync_hk_transition_links(root, workspace_root=workspace, dry_run=dry_run)
        if sync_transition_links
        else []
    )
    frequency = str(frequency or HK_DEFAULT_INTRADAY_FREQUENCY).strip()
    start_token = str(start_date).replace("-", "").strip()
    end_token = str(end_date).replace("-", "").strip()
    symbol_path = (
        Path(symbols_file).expanduser().resolve()
        if symbols_file is not None
        else _platform_relative(
            root,
            "assets",
            "rqdata",
            "hk",
            "daily",
            "hk_all_daily_clean_latest",
            "symbols.txt",
        )
    )
    cache_dir = _platform_relative(root, "cache", "intraday")
    output_path = cache_dir / f"hk_intraday_{frequency}_{start_token}_{end_token}.parquet"
    health_path = _platform_relative(
        root,
        "reports",
        f"hk_intraday_health_{start_token}_{end_token}.json",
    )
    daily_asset_dir = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "daily",
        "hk_all_daily_clean_latest",
    )
    asset_alias = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "intraday",
        "hk_intraday_latest",
    )
    command = _hk_assets_command(
        platform_repo,
        "sync-hk-intraday",
        "--symbols-file",
        str(symbol_path),
        "--start-date",
        start_token,
        "--end-date",
        end_token,
        "--frequency",
        frequency,
        "--batch-size",
        str(batch_size),
        "--output",
        str(output_path),
        "--health-out",
        str(health_path),
        "--daily-asset-dir",
        str(daily_asset_dir),
        "--inspect-fail-on-severity",
        inspect_fail_on_severity,
        "--out-root",
        str(_platform_relative(root, "assets", "rqdata")),
        "--asset-alias",
        str(asset_alias),
    )
    if resume:
        command.append("--resume")
    if verify_full_asset:
        command.append("--verify-full-asset")
    if verify_sampled_segments:
        command.extend(["--verify-sampled-segments", str(verify_sampled_segments)])
    if config:
        command.extend(["--config", config])

    completed = _run_command(
        runner,
        command,
        cwd=platform_repo,
        env=_workflow_environment(platform_repo, root),
        dry_run=dry_run,
    )
    returncode = int(completed.returncode)
    contract_paths: dict[str, str] = {}
    if returncode == 0 and rebuild_contract and not dry_run:
        contract_paths = _rebuild_and_sync_hk_current_contract(
            root,
            target_date=end_token,
            generated_by="marketdata rqdata refresh-hk-intraday",
            workspace=workspace,
        )
    return {
        "artifacts_root": str(root),
        "command": command,
        "returncode": returncode,
        "transition_links": links,
        "output": str(output_path),
        "health_report": str(health_path),
        "asset_alias": str(asset_alias),
        **contract_paths,
    }


def run_hk_depth_refresh(
    *,
    artifacts_root: str | Path | None = None,
    start_date: str,
    end_date: str,
    symbols: str | None = None,
    symbols_file: str | Path | None = None,
    name: str | None = None,
    fields: str | None = None,
    batch_size: int = HK_DEPTH_DEFAULT_BATCH_SIZE,
    raw_layout: str = "symbol-date",
    calendar: str = "provider",
    fail_on_severity: str = "error",
    resume: bool = True,
    continue_on_error: bool = False,
    publish_assets: bool = True,
    dry_run: bool = False,
    rebuild_contract: bool = True,
    workspace_root: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    platform_repo = _platform_repo_root()
    start_token = str(start_date).replace("-", "").strip()
    end_token = str(end_date).replace("-", "").strip()
    snapshot_name = str(name or f"hk_tick_depth_{start_token}_{end_token}").strip()
    symbol_path = (
        Path(symbols_file).expanduser().resolve()
        if symbols_file is not None
        else _platform_relative(
            root,
            "assets",
            "rqdata",
            "hk",
            "daily",
            "hk_all_daily_clean_latest",
            "symbols.txt",
        )
    )

    raw_cache = _platform_relative(root, "cache", "rqdata", "hk_tick_depth", snapshot_name)
    daily_cache = _platform_relative(root, "cache", "rqdata", "hk_tick_depth_daily", snapshot_name)
    daily_output = daily_cache / "data.parquet"
    health_report = _platform_relative(root, "reports", f"tick_health_{snapshot_name}.json")
    aggregate_report = _platform_relative(root, "reports", f"tick_aggregate_{snapshot_name}.json")
    raw_asset = _platform_relative(root, "assets", "rqdata", "hk", "tick_depth", snapshot_name)
    daily_asset = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "tick_depth_daily",
        snapshot_name,
    )
    raw_alias = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "tick_depth",
        "hk_tick_depth_latest",
    )
    daily_alias = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "tick_depth_daily",
        "hk_tick_depth_daily_latest",
    )

    download = _depth_command(
        "download",
        "--start-date",
        start_token,
        "--end-date",
        end_token,
        "--out",
        str(raw_cache),
        "--batch-size",
        str(batch_size),
        "--raw-layout",
        raw_layout,
        "--calendar",
        calendar,
    )
    if symbols:
        download.extend(["--symbols", symbols])
    else:
        download.extend(["--symbols-file", str(symbol_path)])
    if fields:
        download.extend(["--fields", fields])
    download.append("--resume" if resume else "--no-resume")
    if continue_on_error:
        download.append("--continue-on-error")

    commands: list[list[str]] = [
        download,
        _depth_command(
            "health",
            "--input",
            str(raw_cache),
            "--out-json",
            str(health_report),
            "--fail-on-severity",
            fail_on_severity,
        ),
        _depth_command(
            "aggregate-daily",
            "--input",
            str(raw_cache),
            "--output",
            str(daily_output),
            "--meta-output",
            str(aggregate_report),
        ),
    ]
    if publish_assets:
        commands.extend(
            [
                _depth_command(
                    "emit-asset",
                    "--kind",
                    "raw",
                    "--source",
                    str(raw_cache),
                    "--output",
                    str(raw_asset),
                ),
                _depth_command(
                    "emit-asset",
                    "--kind",
                    "daily",
                    "--source",
                    str(daily_output),
                    "--output",
                    str(daily_asset),
                ),
            ]
        )

    results: list[dict[str, Any]] = []
    returncode = 0
    env = _backend_environment(platform_repo, root)
    for command in commands:
        completed = _run_command(
            runner,
            command,
            cwd=platform_repo,
            env=env,
            dry_run=dry_run,
        )
        code = int(completed.returncode)
        results.append({"command": command, "returncode": code})
        if code != 0:
            returncode = code
            break

    aliases: list[dict[str, str]] = []
    contract_paths: dict[str, str] = {}
    if returncode == 0 and publish_assets:
        aliases.append(_create_relative_symlink(raw_asset, raw_alias, dry_run=dry_run))
        aliases.append(_create_relative_symlink(daily_asset, daily_alias, dry_run=dry_run))
        if rebuild_contract and not dry_run:
            contract_paths = _rebuild_and_sync_hk_current_contract(
                root,
                target_date=end_token,
                generated_by="marketdata rqdata refresh-hk-depth",
                workspace=workspace,
            )

    return {
        "artifacts_root": str(root),
        "snapshot_name": snapshot_name,
        "returncode": returncode,
        "steps": results,
        "raw_cache": str(raw_cache),
        "daily_cache": str(daily_cache),
        "health_report": str(health_report),
        "aggregate_report": str(aggregate_report),
        "raw_asset": str(raw_asset),
        "daily_asset": str(daily_asset),
        "aliases": aliases,
        **contract_paths,
    }


def run_hk_fundamentals_refresh(
    *,
    artifacts_root: str | Path | None = None,
    target_date: str,
    pit_patch_start_quarter: str = HK_DEFAULT_PIT_PATCH_START_QUARTER,
    pit_patch_end_quarter: str = HK_DEFAULT_PIT_PATCH_END_QUARTER,
    financial_start_quarter: str = HK_DEFAULT_FINANCIAL_START_QUARTER,
    financial_end_quarter: str = HK_DEFAULT_FINANCIAL_END_QUARTER,
    symbols_file: str | Path | None = None,
    financial_fields_file: str | Path | None = None,
    inspect_pit: bool = True,
    config: str | None = None,
    resume: bool = True,
    dry_run: bool = False,
    sync_transition_links: bool = True,
    rebuild_contract: bool = True,
    workspace_root: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    root = resolve_artifacts_root(artifacts_root)
    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    platform_repo = _platform_repo_root()
    links = (
        sync_hk_transition_links(root, workspace_root=workspace, dry_run=dry_run)
        if sync_transition_links
        else []
    )
    target_token = str(target_date).replace("-", "").strip()
    env = _workflow_environment(platform_repo, root)

    daily_symbols = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "daily",
        "hk_all_daily_clean_latest",
        "symbols.txt",
    )
    symbol_path = Path(symbols_file).expanduser().resolve() if symbols_file else daily_symbols
    symbol_count = _count_text_rows(symbol_path)
    financial_fields_path = (
        Path(financial_fields_file).expanduser().resolve()
        if financial_fields_file
        else _platform_relative(
            root,
            "assets",
            "rqdata",
            "hk",
            "financial_details",
            "hk_financial_details_latest",
            "fields.txt",
        )
    )

    pit_alias = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "pit_financials",
        "hk_all_2000_2025_full_market_latest",
    )
    pit_name = (
        f"hk_all_2000_2026_full_market_asof_{target_token}_patch_"
        f"{pit_patch_start_quarter}_{pit_patch_end_quarter}"
    )
    pit_output = pit_alias.parent / pit_name
    financial_alias = _platform_relative(
        root,
        "assets",
        "rqdata",
        "hk",
        "financial_details",
        "hk_financial_details_latest",
    )
    financial_name = (
        f"hk_financial_details_hk_all{symbol_count or 'unknown'}_superset_"
        f"{financial_start_quarter[:4]}_{financial_end_quarter[:4]}_{target_token}"
    )
    financial_output = financial_alias.parent / financial_name

    commands: list[list[str]] = [
        _hk_assets_command(
            platform_repo,
            "patch-hk-pit-financials",
            "--base-asset-dir",
            str(pit_alias),
            "--target-date",
            target_token,
            "--patch-start-quarter",
            pit_patch_start_quarter,
            "--patch-end-quarter",
            pit_patch_end_quarter,
            "--name",
            pit_name,
        ),
        _hk_assets_command(
            platform_repo,
            "mirror-hk-financial-details",
            "--start-quarter",
            financial_start_quarter,
            "--end-quarter",
            financial_end_quarter,
            "--date",
            target_token,
            "--symbols-file",
            str(symbol_path),
            "--fields-file",
            str(financial_fields_path),
            "--name",
            financial_name,
        ),
    ]
    if resume:
        commands[0].append("--resume")
        commands[1].append("--resume")
    if config:
        commands[0].extend(["--config", config])
        commands[1].extend(["--config", config])
    if inspect_pit:
        commands.append(
            _hk_assets_command(
                platform_repo,
                "build-hk-pit-fundamentals",
                "--asset-dir",
                str(pit_output),
                "--field-profile",
                "starter",
                "--force",
            )
        )
        commands.append(
            _hk_assets_command(
                platform_repo,
                "inspect-hk-pit-coverage",
                "--asset-dir",
                str(pit_output),
                "--field-profile",
                "starter",
                "--mode",
                "both",
                "--include-health",
                "--target-date",
                target_token,
                "--by-date-file",
                str(_platform_relative(root, "assets", "universe", "hk_all_full_by_date.csv")),
                "--format",
                "json",
                "--out",
                str(
                    _platform_relative(
                        root,
                        "reports",
                        f"hk_pit_health_{target_token}_platform_fundamentals.json",
                    )
                ),
                "--fail-on-severity",
                "none",
            )
        )

    results: list[dict[str, Any]] = []
    returncode = 0
    for command in commands:
        completed = _run_command(
            runner,
            command,
            cwd=platform_repo,
            env=env,
            dry_run=dry_run,
        )
        code = int(completed.returncode)
        results.append({"command": command, "returncode": code})
        if code != 0:
            returncode = code
            break

    aliases: list[dict[str, str]] = []
    contract_paths: dict[str, str] = {}
    if returncode == 0:
        aliases.append(_create_relative_symlink(pit_output, pit_alias, dry_run=dry_run))
        aliases.append(_create_relative_symlink(financial_output, financial_alias, dry_run=dry_run))
        if rebuild_contract and not dry_run:
            contract_paths = _rebuild_and_sync_hk_current_contract(
                root,
                target_date=target_token,
                generated_by="marketdata rqdata refresh-hk-fundamentals",
                workspace=workspace,
            )

    return {
        "artifacts_root": str(root),
        "returncode": returncode,
        "transition_links": links,
        "steps": results,
        "aliases": aliases,
        "pit_output": str(pit_output),
        "financial_details_output": str(financial_output),
        **contract_paths,
    }
