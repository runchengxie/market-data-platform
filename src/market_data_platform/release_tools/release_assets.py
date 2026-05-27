#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

from market_data_platform.repo_paths import find_repo_root

REPO_ROOT = find_repo_root(__file__)
PACKAGE_MODULE = "market_data_platform.release_tools.package_assets"


def _resolve_path(path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def _run(cmd: list[str], *, dry_run: bool, capture: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(shlex.quote(part) for part in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, check=False, capture_output=capture, text=True)


def _parse_staged_root(output: str) -> Path | None:
    for line in output.splitlines():
        if line.startswith("Staged asset parts at:"):
            path_text = line.split(":", 1)[1].strip()
            if path_text:
                return Path(path_text).expanduser().resolve()
    return None


def _load_manifest(staged_root: Path) -> dict:
    manifest_path = staged_root / "manifest.yml"
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}


def _manifest_distribution(manifest: dict) -> dict:
    node = manifest.get("distribution")
    return node if isinstance(node, dict) else {}


def _manifest_parts(manifest: dict) -> dict[str, dict]:
    node = manifest.get("parts")
    if not isinstance(node, dict):
        return {}
    return {str(key): value for key, value in node.items() if isinstance(value, dict)}


def _selected_parts(manifest: dict, requested_parts: list[str]) -> list[str]:
    available = _manifest_parts(manifest)
    if not available:
        raise SystemExit("No parts found in staged manifest.")
    selected = list(dict.fromkeys(requested_parts or list(available.keys())))
    missing = [part for part in selected if part not in available]
    if missing:
        raise SystemExit(f"Requested parts are not available in staged manifest: {missing}")
    return selected


def _format_readme(manifest: dict, selected_parts: list[str]) -> str:
    distribution = _manifest_distribution(manifest)
    name = distribution.get("name") or "assets"
    as_of = distribution.get("as_of") or "unknown"
    generated_at = distribution.get("generated_at") or "unknown"
    source_repo = distribution.get("source_repo") or str(REPO_ROOT)
    mode = distribution.get("mode") or "copy"
    parts = _manifest_parts(manifest)

    lines = [
        "# CSTree HK Asset Release Parts",
        "",
        "This release splits reusable HK assets into independent upload parts.",
        "",
        f"Distribution: {name}",
        f"As of: {as_of}",
        f"Generated at: {generated_at}",
        f"Source repo: {source_repo}",
        f"Mode: {mode}",
        "",
        "Included parts:",
    ]
    for part_name in selected_parts:
        part = parts[part_name]
        description = part.get("description") or ""
        lines.append(f"- {part_name}: {description}".rstrip())
        summary = part.get("summary")
        if isinstance(summary, dict):
            for key, value in summary.items():
                lines.append(f"  - {key}: {value}")
    lines.extend(
        [
            "",
            "Each uploaded tarball contains one independent asset part plus its manifest.yml.",
            "",
        ]
    )
    return "\n".join(lines)


def _format_release_notes(manifest: dict, selected_parts: list[str], tar_paths: list[Path]) -> str:
    distribution = _manifest_distribution(manifest)
    name = distribution.get("name") or "assets"
    as_of = distribution.get("as_of") or "unknown"
    generated_at = distribution.get("generated_at") or "unknown"
    parts = _manifest_parts(manifest)

    lines = [
        f"Distribution: {name}",
        f"As of: {as_of}",
        f"Generated at: {generated_at}",
        "",
        "Uploaded parts:",
    ]
    for part_name, tar_path in zip(selected_parts, tar_paths, strict=True):
        part = parts[part_name]
        summary = part.get("summary")
        lines.append(f"- {part_name}: {tar_path.name}")
        if isinstance(summary, dict):
            for key, value in summary.items():
                lines.append(f"  - {key}: {value}")
    return "\n".join(lines) + "\n"


def _ensure_gh() -> None:
    if shutil.which("gh") is None:
        raise SystemExit("GitHub CLI (gh) not found in PATH.")


def _default_tag(manifest: dict) -> str:
    distribution = _manifest_distribution(manifest)
    name = distribution.get("name") or "assets"
    as_of = distribution.get("as_of")
    if as_of:
        return f"assets-{name}-{as_of}"
    return f"assets-{name}"


def _default_title(manifest: dict) -> str:
    distribution = _manifest_distribution(manifest)
    name = distribution.get("name") or "assets"
    as_of = distribution.get("as_of")
    return f"Assets {name}{' ' + as_of if as_of else ''}"


def _asset_tar_name(manifest: dict, part_name: str) -> str:
    distribution = _manifest_distribution(manifest)
    name = distribution.get("name") or "assets"
    as_of = distribution.get("as_of") or "unknown"
    return f"assets-{name}-{as_of}-{part_name}.tar.gz"


def _build_tar(part_dir: Path, tar_path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if tar_path.exists():
        tar_path.unlink()
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(part_dir, arcname=part_dir.name, recursive=True)


def _build_tars(
    *,
    staged_root: Path,
    manifest: dict,
    selected_parts: list[str],
    tar_dir: Path,
    dry_run: bool,
) -> list[Path]:
    tar_paths: list[Path] = []
    for part_name in selected_parts:
        part_dir = staged_root / part_name
        if not part_dir.exists():
            raise SystemExit(f"Staged part not found: {part_dir}")
        tar_path = tar_dir / _asset_tar_name(manifest, part_name)
        _build_tar(part_dir, tar_path, dry_run=dry_run)
        tar_paths.append(tar_path)
    return tar_paths


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage HK asset parts and upload multiple tarballs into one GitHub Release.",
    )
    parser.add_argument("--staged-root", help="Existing staged asset-parts root to upload.")
    parser.add_argument("--tar-dir", help="Output directory for per-part tarballs.")
    parser.add_argument("--tag", help="Release tag (default derived from manifest).")
    parser.add_argument("--title", help="Release title (default derived from manifest).")
    parser.add_argument("--notes-file", help="Release notes file.")
    parser.add_argument("--draft", action="store_true", help="Create as draft.")
    parser.add_argument("--prerelease", action="store_true", help="Mark as prerelease.")
    parser.add_argument("--latest", action="store_true", help="Mark as latest.")
    parser.add_argument("--clobber", action="store_true", help="Overwrite assets if they exist.")
    parser.add_argument("--repo", help="Target repo in owner/name format.")
    parser.add_argument("--skip-package", action="store_true", help="Skip staging step.")
    parser.add_argument("--skip-upload", action="store_true", help="Skip release upload step.")
    parser.add_argument("--no-readme", action="store_true", help="Do not write release README.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--part",
        action="append",
        choices=(
            "daily",
            "intraday",
            "etf",
            "valuation",
            "instruments",
            "pit",
            "reference",
            "exchange_rate",
            "southbound",
            "financial_details",
            "announcement",
            "industry",
            "universe",
        ),
        default=[],
        help="Only upload selected part(s). Repeatable.",
    )
    return parser


def _resolve_staged_root(args: argparse.Namespace, package_args: list[str]) -> Path | None:
    if args.staged_root and package_args:
        print("Warning: package args ignored because --staged-root is set.", file=sys.stderr)

    if args.staged_root:
        staged_root = _resolve_path(args.staged_root)
        if not staged_root.exists():
            raise SystemExit(f"Staged root not found: {staged_root}")
        return staged_root

    if args.skip_package:
        raise SystemExit("No staged root provided and --skip-package was set.")
    package_cmd = [sys.executable, "-m", PACKAGE_MODULE, *package_args]
    for part_name in args.part:
        package_cmd.extend(["--part", part_name])
    if args.dry_run:
        package_cmd.append("--dry-run")
    result = _run(package_cmd, dry_run=False, capture=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or "")
        raise SystemExit(result.returncode)
    sys.stdout.write(result.stdout or "")
    if args.dry_run:
        print("Dry run complete.")
        return None
    staged_root = _parse_staged_root(result.stdout or "")
    if staged_root is None:
        raise SystemExit("Could not detect staged root from package_assets output.")
    return staged_root


def _write_release_readme(
    *,
    staged_root: Path,
    manifest: dict,
    selected_parts: list[str],
    args: argparse.Namespace,
) -> None:
    if args.no_readme or args.dry_run:
        return
    readme_path = staged_root / "README.md"
    readme_path.write_text(_format_readme(manifest, selected_parts), encoding="utf-8")


def _prepare_release_notes_file(
    *,
    args: argparse.Namespace,
    manifest: dict,
    selected_parts: list[str],
    tar_paths: list[Path],
    tar_dir: Path,
    tag: str,
) -> str:
    notes_file = args.notes_file
    if notes_file:
        return str(notes_file)
    notes_path = tar_dir / f"{tag}.release_notes.txt"
    if not args.dry_run:
        notes_path.write_text(
            _format_release_notes(manifest, selected_parts, tar_paths),
            encoding="utf-8",
        )
    return str(notes_path)


def _publish_release_assets(
    *,
    args: argparse.Namespace,
    manifest: dict,
    selected_parts: list[str],
    tar_paths: list[Path],
    tar_dir: Path,
) -> int:
    _ensure_gh()
    tag = args.tag or _default_tag(manifest)
    title = args.title or _default_title(manifest)
    notes_file = _prepare_release_notes_file(
        args=args,
        manifest=manifest,
        selected_parts=selected_parts,
        tar_paths=tar_paths,
        tar_dir=tar_dir,
        tag=tag,
    )
    repo_args: list[str] = ["--repo", args.repo] if args.repo else []

    view_cmd = ["gh", "release", "view", tag, *repo_args]
    view_result = _run(view_cmd, dry_run=args.dry_run, capture=True)
    tar_args = [str(path) for path in tar_paths]
    if view_result.returncode == 0:
        upload_cmd = ["gh", "release", "upload", tag, *tar_args, *repo_args]
        if args.clobber:
            upload_cmd.append("--clobber")
        _run(upload_cmd, dry_run=args.dry_run)
        return 0

    create_cmd = [
        "gh",
        "release",
        "create",
        tag,
        *tar_args,
        "--title",
        title,
        "--notes-file",
        notes_file,
        *repo_args,
    ]
    if args.draft:
        create_cmd.append("--draft")
    if args.prerelease:
        create_cmd.append("--prerelease")
    if args.latest:
        create_cmd.append("--latest")
    _run(create_cmd, dry_run=args.dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, package_args = parser.parse_known_args(argv)
    staged_root = _resolve_staged_root(args, package_args)
    if staged_root is None:
        return 0
    manifest = _load_manifest(staged_root)
    selected_parts = _selected_parts(manifest, args.part)
    _write_release_readme(
        staged_root=staged_root,
        manifest=manifest,
        selected_parts=selected_parts,
        args=args,
    )

    tar_dir = (
        _resolve_path(args.tar_dir)
        if args.tar_dir
        else staged_root.parent / f"{staged_root.name}_tarballs"
    )
    if not args.dry_run:
        tar_dir.mkdir(parents=True, exist_ok=True)
    tar_paths = _build_tars(
        staged_root=staged_root,
        manifest=manifest,
        selected_parts=selected_parts,
        tar_dir=tar_dir,
        dry_run=args.dry_run,
    )

    if args.skip_upload:
        print(f"Staged root: {staged_root}")
        for tar_path in tar_paths:
            print(f"Tarball: {tar_path}")
        return 0

    return _publish_release_assets(
        args=args,
        manifest=manifest,
        selected_parts=selected_parts,
        tar_paths=tar_paths,
        tar_dir=tar_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
