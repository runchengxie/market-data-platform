from __future__ import annotations

"""Stable package facade for rqdata asset helpers.

Keep only package-level defaults and thin compatibility hooks here. The heavier
public export surface lives in ``public_api.py``.
"""

from importlib import import_module
from types import ModuleType

from market_data_platform.artifacts import RQDATA_ASSETS_DIR as DEFAULT_RQDATA_ASSETS_DIR
from ._public_exports import PUBLIC_API_EXPORTS

DEFAULT_OUT_ROOT = DEFAULT_RQDATA_ASSETS_DIR.as_posix()
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIRROR_MAX_ATTEMPTS = 3
DEFAULT_MIRROR_BACKOFF_SECONDS = 1.0
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = 30.0


_PUBLIC_API_MODULE: ModuleType | None = None
__all__ = list(PUBLIC_API_EXPORTS)


def _load_public_api_module() -> ModuleType:
    global _PUBLIC_API_MODULE
    if _PUBLIC_API_MODULE is None:
        _PUBLIC_API_MODULE = import_module("market_data_platform.hk_assets.public_api")
    return _PUBLIC_API_MODULE


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = _load_public_api_module()
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
