from __future__ import annotations

"""Stable package facade for rqdata asset helpers.

Keep only package-level defaults and thin compatibility hooks here. The heavier
public export surface lives in ``public_api.py``.
"""

from market_data_platform.artifacts import RQDATA_ASSETS_DIR as DEFAULT_RQDATA_ASSETS_DIR
from . import args as _args
from .fetch_runtime import _ensure_rqdatac_hk_plugin
from .shared import _load_hk_financial_fields, _resolve_fields_with_overrides

DEFAULT_OUT_ROOT = DEFAULT_RQDATA_ASSETS_DIR.as_posix()
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIRROR_MAX_ATTEMPTS = 3
DEFAULT_MIRROR_BACKOFF_SECONDS = 1.0
DEFAULT_MIRROR_MAX_BACKOFF_SECONDS = 30.0


def _resolve_fields(args) -> tuple[list[str], dict]:
    return _resolve_fields_with_overrides(
        args,
        load_hk_financial_fields_override=_load_hk_financial_fields,
    )


from . import public_api as _public_api

for _name in getattr(_public_api, "__all__", ()):
    globals().setdefault(_name, getattr(_public_api, _name))

__all__ = getattr(
    _public_api,
    "__all__",
    [name for name in globals() if not name.startswith("__")],
)
