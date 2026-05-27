from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from market_data_platform.repo_paths import find_repo_root, resolve_repo_path as resolve_repo_relative_path
from market_data_platform.release_tools import package_assets as package_assets_tool
from market_data_platform.release_tools import release_assets as release_assets_tool
from market_data_platform.intraday_paths import resolve_intraday_input_groups
from market_data_platform.hk_assets.intraday_download import download_hk_intraday_cache
from .intraday_asset import build_hk_intraday_asset
from .intraday_health import inspect_hk_intraday_health


REPO_ROOT = find_repo_root(__file__)
REPORTS_ROOT = REPO_ROOT / "artifacts" / "reports"
RELEASES_ROOT = REPO_ROOT / "artifacts" / "releases"
DEFAULT_INTRADAY_CACHE_DIR = REPO_ROOT / "artifacts" / "cache" / "intraday"
DEFAULT_INTRADAY_ASSET_ALIAS = "artifacts/assets/rqdata/hk/intraday/hk_intraday_latest"
DEFAULT_INTRADAY_ASSET_OUT_ROOT = "artifacts/assets/rqdata"
DEFAULT_INTRADAY_DAILY_ASSET_DIR = "artifacts/assets/rqdata/hk/daily/hk_all_daily_clean_latest"
DEFAULT_PACKAGE_PRESET = "hk_current"
DEFAULT_PACKAGE_DAILY_SNAPSHOT = "artifacts/assets/rqdata/hk/daily/hk_all_daily_latest"
DEFAULT_PACKAGE_INSTRUMENTS_FILE = "artifacts/assets/rqdata/hk/instruments/hk_all_instruments_latest.parquet"
DEFAULT_INTRADAY_DISTRIBUTION_NAME = "hk_intraday_assets"


def _resolve_repo_path(path_text: str | Path) -> Path:
    return resolve_repo_relative_path(path_text, repo_root=REPO_ROOT)


def _resolve_repo_link_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.parent.resolve() / path.name
    candidate = REPO_ROOT / path
    return candidate.parent.resolve() / candidate.name


def _normalize_date_token(value: str, *, label: str) -> str:
    token = str(value or "").replace("-", "").strip()
    if len(token) != 8 or not token.isdigit():
        raise SystemExit(f"{label} must be YYYYMMDD or YYYY-MM-DD. Got: {value!r}")
    return token


def _default_output_path(*, frequency: str, start_date: str, end_date: str) -> Path:
    return DEFAULT_INTRADAY_CACHE_DIR / f"hk_intraday_{frequency}_{start_date}_{end_date}.parquet"


def _default_health_report_path(output_path: Path) -> Path:
    return REPORTS_ROOT / f"{output_path.stem}_health.json"


def _default_full_health_report_path(asset_dir: Path) -> Path:
    return REPORTS_ROOT / f"{asset_dir.name}_health.json"


def _default_sampled_health_report_path(asset_dir: Path, segment_count: int) -> Path:
    return REPORTS_ROOT / f"{asset_dir.name}_sampled_{segment_count}_segments_health.json"


def _default_package_dest(*, distribution_name: str, as_of: str) -> Path:
    return RELEASES_ROOT / f"{distribution_name}_{as_of}_stage"


def _default_tar_dir(*, distribution_name: str, as_of: str) -> Path:
    return RELEASES_ROOT / f"{distribution_name}_{as_of}_tarballs"


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _print_health_summary(report_path: Path) -> None:
    payload = _load_json(report_path)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    verdict = payload.get("quality_verdict") if isinstance(payload.get("quality_verdict"), dict) else {}
    severity_counts = verdict.get("severity_counts") if isinstance(verdict.get("severity_counts"), dict) else {}
    print(
        "intraday health:",
        f"overall={verdict.get('overall_severity', 'unknown')}",
        f"errors={int(severity_counts.get('error') or 0)}",
        f"warnings={int(severity_counts.get('warning') or 0)}",
        f"info={int(severity_counts.get('info') or 0)}",
        f"trade_date_max={summary.get('trade_date_max')}",
        f"rows_scanned={summary.get('rows_scanned')}",
        f"report={report_path}",
    )


def _build_package_argv(
    *,
    asset_dir: Path,
    package_dest: Path,
    distribution_name: str,
    as_of: str,
    preset: str,
    daily_snapshot: str,
    instruments_file: str,
) -> list[str]:
    return [
        "--preset",
        preset,
        "--dest",
        str(package_dest),
        "--name",
        distribution_name,
        "--as-of",
        as_of,
        "--overwrite",
        "--part",
        "intraday",
        "--intraday-snapshot",
        str(asset_dir),
        "--daily-snapshot",
        daily_snapshot,
        "--instruments-file",
        instruments_file,
    ]


def _build_release_argv(
    *,
    package_dest: Path,
    tar_dir: Path,
    release: bool,
    repo: str | None,
    tag: str | None,
    title: str | None,
    draft: bool,
    prerelease: bool,
    latest: bool,
    clobber: bool,
) -> list[str]:
    command = [
        "--staged-root",
        str(package_dest),
        "--tar-dir",
        str(tar_dir),
        "--part",
        "intraday",
    ]
    if not release:
        command.append("--skip-upload")
    if repo:
        command.extend(["--repo", repo])
    if tag:
        command.extend(["--tag", tag])
    if title:
        command.extend(["--title", title])
    if draft:
        command.append("--draft")
    if prerelease:
        command.append("--prerelease")
    if latest:
        command.append("--latest")
    if clobber:
        command.append("--clobber")
    return command


def _build_asset_inputs(*, asset_alias: Path, downloaded_output_path: Path) -> list[str]:
    inputs: list[str] = []
    if asset_alias.exists():
        existing_groups = resolve_intraday_input_groups([str(asset_alias)])
        for group in existing_groups:
            if group.stem == downloaded_output_path.stem:
                continue
            if group.parquet_path is not None and group.parquet_path.exists():
                inputs.append(str(group.parquet_path))
            elif group.parts_dir is not None and group.parts_dir.exists():
                inputs.append(str(group.parts_dir))
    inputs.append(str(downloaded_output_path))
    return inputs


def _sample_intraday_asset_inputs(asset_dir: Path, segment_count: int) -> list[str]:
    if segment_count <= 0:
        return []
    groups = resolve_intraday_input_groups([str(asset_dir)])
    if not groups:
        raise SystemExit(f"No intraday segments available for sampled verification: {asset_dir}")
    if segment_count >= len(groups):
        selected = groups
    elif segment_count == 1:
        selected = [groups[-1]]
    else:
        indexes = {
            round(position * (len(groups) - 1) / (segment_count - 1))
            for position in range(segment_count)
        }
        selected = [groups[index] for index in sorted(indexes)]
    return [
        str(group.parquet_path or group.parts_dir)
        for group in selected
        if group.parquet_path is not None or group.parts_dir is not None
    ]


def _inspect_intraday_input(
    *,
    input_specs: list[str],
    daily_asset_dir: Path,
    sample_limit: int,
    expected_bars_per_day: int,
    numeric_rtol: float,
    numeric_atol: float,
    intraday_adjust_type: str | None,
    daily_adjust_type: str | None,
    fail_on_severity: str,
    out_path: Path,
) -> int:
    inspect_args = SimpleNamespace(
        input=input_specs,
        daily_asset_dir=str(daily_asset_dir),
        sample_limit=sample_limit,
        expected_bars_per_day=expected_bars_per_day,
        numeric_rtol=numeric_rtol,
        numeric_atol=numeric_atol,
        intraday_adjust_type=intraday_adjust_type,
        daily_adjust_type=daily_adjust_type,
        fail_on_severity=fail_on_severity,
        format="json",
        out=str(out_path),
    )
    inspect_result = inspect_hk_intraday_health(inspect_args)
    _print_health_summary(out_path)
    return int(inspect_result)


def sync_hk_intraday(args, rqdatac) -> int:
    start_date = _normalize_date_token(args.start_date, label="--start-date")
    end_date = _normalize_date_token(args.end_date, label="--end-date")
    if start_date > end_date:
        raise SystemExit("--start-date must be <= --end-date.")
    sampled_segment_count = int(getattr(args, "verify_sampled_segments", 0) or 0)
    if sampled_segment_count < 0:
        raise SystemExit("--verify-sampled-segments must be >= 0.")

    frequency = str(getattr(args, "frequency", "5m") or "5m").strip()
    output_path = (
        _resolve_repo_path(args.output)
        if getattr(args, "output", None)
        else _default_output_path(frequency=frequency, start_date=start_date, end_date=end_date)
    )
    meta_output = _resolve_repo_path(args.meta_output) if getattr(args, "meta_output", None) else None
    parts_dir = _resolve_repo_path(args.parts_dir) if getattr(args, "parts_dir", None) else None

    download_args = SimpleNamespace(**vars(args))
    download_args.start_date = start_date
    download_args.end_date = end_date
    download_args.frequency = frequency
    download_args.output = str(output_path)
    download_args.meta_output = str(meta_output) if meta_output is not None else None
    download_args.parts_dir = str(parts_dir) if parts_dir is not None else None
    download_result = download_hk_intraday_cache(download_args, rqdatac)

    daily_asset_dir = _resolve_repo_path(
        getattr(args, "daily_asset_dir", None) or DEFAULT_INTRADAY_DAILY_ASSET_DIR
    )
    sample_limit = int(getattr(args, "sample_limit", 5) or 5)
    expected_bars_per_day = int(getattr(args, "expected_bars_per_day", 66) or 66)
    numeric_rtol = float(getattr(args, "numeric_rtol", 1e-6) or 1e-6)
    numeric_atol = float(getattr(args, "numeric_atol", 1e-8) or 1e-8)
    daily_adjust_type = getattr(args, "daily_adjust_type", None)

    patch_health_report_path = (
        _resolve_repo_path(args.health_out)
        if getattr(args, "health_out", None)
        else _default_health_report_path(download_result["output_path"])
    )
    if not getattr(args, "skip_inspect", False):
        inspect_result = _inspect_intraday_input(
            input_specs=[str(download_result["output_path"])],
            daily_asset_dir=daily_asset_dir,
            sample_limit=sample_limit,
            expected_bars_per_day=expected_bars_per_day,
            numeric_rtol=numeric_rtol,
            numeric_atol=numeric_atol,
            intraday_adjust_type=str(getattr(args, "adjust_type", "") or "") or None,
            daily_adjust_type=str(daily_adjust_type or "") or None,
            fail_on_severity=str(getattr(args, "inspect_fail_on_severity", "warning") or "warning"),
            out_path=patch_health_report_path,
        )
        if inspect_result != 0:
            return int(inspect_result)

    asset_alias = _resolve_repo_link_path(getattr(args, "asset_alias", None) or DEFAULT_INTRADAY_ASSET_ALIAS)
    asset_build_result = build_hk_intraday_asset(
        SimpleNamespace(
            input=_build_asset_inputs(
                asset_alias=asset_alias,
                downloaded_output_path=download_result["output_path"],
            ),
            out_root=str(
                _resolve_repo_path(
                    getattr(args, "out_root", None) or DEFAULT_INTRADAY_ASSET_OUT_ROOT
                )
            ),
            name=getattr(args, "asset_name", None),
            alias=str(asset_alias),
        )
    )
    if asset_build_result != 0:
        return int(asset_build_result)
    if not asset_alias.exists():
        raise SystemExit(f"Expected intraday asset alias not found after build: {asset_alias}")
    asset_dir = asset_alias.resolve()
    print(f"intraday asset alias: {asset_alias} -> {asset_dir.name}")

    if sampled_segment_count:
        sampled_inputs = _sample_intraday_asset_inputs(asset_dir, sampled_segment_count)
        sampled_health_report_path = (
            _resolve_repo_path(args.sampled_health_out)
            if getattr(args, "sampled_health_out", None)
            else _default_sampled_health_report_path(asset_dir, sampled_segment_count)
        )
        sampled_inspect_result = _inspect_intraday_input(
            input_specs=sampled_inputs,
            daily_asset_dir=daily_asset_dir,
            sample_limit=sample_limit,
            expected_bars_per_day=expected_bars_per_day,
            numeric_rtol=numeric_rtol,
            numeric_atol=numeric_atol,
            intraday_adjust_type=str(getattr(args, "adjust_type", "") or "") or None,
            daily_adjust_type=str(daily_adjust_type or "") or None,
            fail_on_severity=str(
                getattr(args, "sampled_inspect_fail_on_severity", "warning") or "warning"
            ),
            out_path=sampled_health_report_path,
        )
        if sampled_inspect_result != 0:
            return int(sampled_inspect_result)

    if getattr(args, "verify_full_asset", False):
        full_health_report_path = (
            _resolve_repo_path(args.full_health_out)
            if getattr(args, "full_health_out", None)
            else _default_full_health_report_path(asset_dir)
        )
        full_inspect_result = _inspect_intraday_input(
            input_specs=[str(asset_dir)],
            daily_asset_dir=daily_asset_dir,
            sample_limit=sample_limit,
            expected_bars_per_day=expected_bars_per_day,
            numeric_rtol=numeric_rtol,
            numeric_atol=numeric_atol,
            intraday_adjust_type=str(getattr(args, "adjust_type", "") or "") or None,
            daily_adjust_type=str(daily_adjust_type or "") or None,
            fail_on_severity=str(
                getattr(args, "full_inspect_fail_on_severity", "warning") or "warning"
            ),
            out_path=full_health_report_path,
        )
        if full_inspect_result != 0:
            return int(full_inspect_result)
    else:
        print("full-asset verify skipped; use --verify-full-asset to scan hk_intraday_latest explicitly")

    should_package = bool(getattr(args, "package", False) or getattr(args, "release", False))
    if not should_package:
        return 0

    distribution_name = str(
        getattr(args, "distribution_name", None) or DEFAULT_INTRADAY_DISTRIBUTION_NAME
    ).strip()
    package_dest = (
        _resolve_repo_path(args.package_dest)
        if getattr(args, "package_dest", None)
        else _default_package_dest(distribution_name=distribution_name, as_of=end_date)
    )
    tar_dir = (
        _resolve_repo_path(args.tar_dir)
        if getattr(args, "tar_dir", None)
        else _default_tar_dir(distribution_name=distribution_name, as_of=end_date)
    )
    package_result = package_assets_tool.main(
        _build_package_argv(
            asset_dir=asset_dir,
            package_dest=package_dest,
            distribution_name=distribution_name,
            as_of=end_date,
            preset=str(getattr(args, "preset", DEFAULT_PACKAGE_PRESET) or DEFAULT_PACKAGE_PRESET),
            daily_snapshot=str(
                getattr(args, "package_daily_snapshot", None) or DEFAULT_PACKAGE_DAILY_SNAPSHOT
            ),
            instruments_file=str(
                getattr(args, "package_instruments_file", None) or DEFAULT_PACKAGE_INSTRUMENTS_FILE
            ),
        )
    )
    if package_result != 0:
        return int(package_result)

    release_result = release_assets_tool.main(
        _build_release_argv(
            package_dest=package_dest,
            tar_dir=tar_dir,
            release=bool(getattr(args, "release", False)),
            repo=getattr(args, "repo", None),
            tag=getattr(args, "tag", None),
            title=getattr(args, "title", None),
            draft=bool(getattr(args, "draft", False)),
            prerelease=bool(getattr(args, "prerelease", False)),
            latest=bool(getattr(args, "latest", False)),
            clobber=bool(getattr(args, "clobber", False)),
        )
    )
    return int(release_result or 0)


__all__ = [
    "DEFAULT_INTRADAY_ASSET_ALIAS",
    "DEFAULT_INTRADAY_DAILY_ASSET_DIR",
    "DEFAULT_INTRADAY_DISTRIBUTION_NAME",
    "DEFAULT_PACKAGE_DAILY_SNAPSHOT",
    "DEFAULT_PACKAGE_INSTRUMENTS_FILE",
    "DEFAULT_PACKAGE_PRESET",
    "sync_hk_intraday",
]
