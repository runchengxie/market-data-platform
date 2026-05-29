from __future__ import annotations

from pathlib import Path

from market_data_platform.hk_workflows import import_cross_platform_artifacts


def run_import_cross_artifacts(
    artifacts_root: str | Path | None = None,
    *,
    cross_artifacts_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
) -> dict[str, object]:
    """Run the archived cross-sectional-trees artifact import helper."""
    return import_cross_platform_artifacts(
        artifacts_root,
        cross_artifacts_root=cross_artifacts_root,
        workspace_root=workspace_root,
        dry_run=dry_run,
        overwrite=overwrite,
    )
