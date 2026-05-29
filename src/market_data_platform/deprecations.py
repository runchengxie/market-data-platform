from __future__ import annotations

import sys
import warnings
from collections.abc import Iterable
from pathlib import Path

DEFAULT_REMOVAL_CONDITION = "after downstream usage has been audited and migrated"


def warn_deprecated_import(
    old: str,
    new: str,
    *,
    removal_condition: str = DEFAULT_REMOVAL_CONDITION,
    stacklevel: int = 3,
) -> None:
    warnings.warn(
        f"{old} is deprecated; use {new} instead. It will be removed "
        f"{removal_condition}.",
        DeprecationWarning,
        stacklevel=stacklevel,
    )


def warn_deprecated_command(
    old: str,
    new: str,
    *,
    removal_condition: str = DEFAULT_REMOVAL_CONDITION,
    stacklevel: int = 3,
) -> None:
    warnings.warn(
        f"{old} is deprecated; use {new} instead. It will be removed "
        f"{removal_condition}.",
        FutureWarning,
        stacklevel=stacklevel,
    )


def warn_if_legacy_console_script(
    legacy_names: Iterable[str],
    replacement: str,
    *,
    removal_condition: str = DEFAULT_REMOVAL_CONDITION,
    stacklevel: int = 3,
) -> None:
    invoked_name = Path(sys.argv[0]).name
    if invoked_name in set(legacy_names):
        warn_deprecated_command(
            invoked_name,
            replacement,
            removal_condition=removal_condition,
            stacklevel=stacklevel,
        )
