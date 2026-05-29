from __future__ import annotations

from market_data_platform.cli import *  # noqa: F403
from market_data_platform.cli import main as _marketdata_main
from market_data_platform.deprecations import warn_deprecated_command


def main(argv: list[str] | None = None) -> int:
    warn_deprecated_command("hkdata", "marketdata")
    return _marketdata_main(argv)
