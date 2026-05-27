"""Shared RQData runtime bootstrap helpers."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
from dotenv import load_dotenv


def patch_rqdatac_adjust_price_readonly(logger: logging.Logger) -> None:
    """Ensure rqdatac's in-place adjust doesn't choke on read-only arrays."""
    try:
        import rqdatac.services.detail.adjust_price as adjust_price
    except Exception as exc:  # pragma: no cover - defensive import
        logger.debug("rqdatac adjust_price import failed: %s", exc)
        return
    if getattr(adjust_price, "_marketdata_readonly_patch", False):
        return

    original = adjust_price.adjust_price_multi_df

    def wrapped(df, order_book_ids, how, obid_slice_map, market):
        r_map_fields = {
            f: i
            for i, f in enumerate(df.columns)
            if f in adjust_price.FIELDS_NEED_TO_ADJUST
        }
        if not r_map_fields:
            return
        pre = how in ("pre", "pre_volume")
        volume_adjust_by_ex_factor = how in ("pre_volume", "post_volume")
        ex_factors = adjust_price.get_ex_factor_for(order_book_ids, market)
        volume_adjust_factors = {}
        if "volume" in r_map_fields:
            if not volume_adjust_by_ex_factor:
                volume_adjust_factors = adjust_price.get_split_factor_for(order_book_ids, market)
            else:
                volume_adjust_factors = ex_factors

        data = df.to_numpy(copy=True)
        try:
            data.setflags(write=True)
        except Exception:
            pass
        timestamps_level = df.index.get_level_values(1)
        for order_book_id, slice_ in obid_slice_map.items():
            if order_book_id not in order_book_ids:
                continue
            timestamps = timestamps_level[slice_]

            def calculate_factor(factors_map, order_book_id):
                factors = factors_map.get(order_book_id, None)
                if factors is not None:
                    factor = np.take(
                        factors.values,
                        factors.index.searchsorted(timestamps, side="right") - 1,
                    )
                    if pre:
                        factor /= factors.iloc[-1]
                    return factor

            factor = calculate_factor(ex_factors, order_book_id)
            if factor is None:
                continue

            if not volume_adjust_by_ex_factor:
                factor_volume = calculate_factor(volume_adjust_factors, order_book_id)
            else:
                factor_volume = factor

            for f, j in r_map_fields.items():
                if f in adjust_price.PRICE_FIELDS:
                    data[slice_, j] *= factor
                elif factor_volume is not None:
                    data[slice_, j] *= 1 / factor_volume

        df.iloc[:, :] = data

    wrapped.__name__ = original.__name__
    wrapped.__doc__ = original.__doc__
    adjust_price._marketdata_original_adjust_price_multi_df = original
    adjust_price.adjust_price_multi_df = wrapped
    adjust_price._marketdata_readonly_patch = True
    logger.warning(
        "Applied rqdatac read-only adjust_price patch (DataFrame copy on demand)."
    )


def resolve_rqdatac_init_kwargs(
    data_cfg: Mapping | None,
    *,
    username: str | None = None,
    password: str | None = None,
    load_env: bool = False,
) -> dict[str, Any]:
    init_kwargs: dict[str, Any] = {}
    rq_cfg = data_cfg.get("rqdata") if isinstance(data_cfg, Mapping) else None
    if isinstance(rq_cfg, Mapping) and isinstance(rq_cfg.get("init"), Mapping):
        for key, value in rq_cfg.get("init", {}).items():
            if value is None:
                continue
            init_kwargs[str(key)] = value

    if username:
        init_kwargs["username"] = username
    if password:
        init_kwargs["password"] = password

    if load_env:
        load_dotenv()

    env_username = os.getenv("RQDATA_USERNAME") or os.getenv("RQDATA_USER")
    env_password = os.getenv("RQDATA_PASSWORD")
    if env_username and "username" not in init_kwargs:
        init_kwargs["username"] = env_username
    if env_password and "password" not in init_kwargs:
        init_kwargs["password"] = env_password
    return init_kwargs


def init_rqdatac(
    *,
    data_cfg: Mapping | None = None,
    username: str | None = None,
    password: str | None = None,
    logger: logging.Logger | None = None,
    load_env: bool = False,
    error_cls: type[BaseException] = RuntimeError,
    import_error_message: str | None = None,
    patch_fn: Callable[[logging.Logger], None] = patch_rqdatac_adjust_price_readonly,
):
    try:
        import rqdatac
    except ImportError as exc:
        message = import_error_message or f"rqdatac is required ({exc})."
        raise error_cls(message) from exc

    init_kwargs = resolve_rqdatac_init_kwargs(
        data_cfg,
        username=username,
        password=password,
        load_env=load_env,
    )
    try:
        rqdatac.init(**init_kwargs)
    except Exception as exc:
        raise error_cls(f"rqdatac.init failed: {exc}") from exc

    patch_logger = logger or logging.getLogger("market_data_platform.rqdata")
    patch_fn(patch_logger)
    return rqdatac
