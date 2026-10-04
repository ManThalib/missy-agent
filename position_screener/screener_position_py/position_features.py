"""Versioned position feature vector — features only, no score, no verdict.

Architecture (owner decision, 2026-10-04): Missy owns discovery and the
feature vector; Sheldon owns scoring policy and portfolio decisions. Every
scanned position carries a `position_features` block so downstream
consumers can score positions without re-deriving chain facts.

Schema is versioned via FEATURES_VERSION: bump it whenever a key changes
meaning. Missing inputs never become zeros — they are recorded in `gaps`
so consumers can apply their own unknown-data policy (fail-closed).
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

FEATURES_VERSION = 1

_U64_MAX = (1 << 64) - 1
_MIN_DAYS_FOR_APR = 1.0 / 24.0  # 1h: below this, annualization is noise


def _f(value: Any) -> Optional[float]:
    """Best-effort finite float, else None."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _i(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_position_features(
    position: Any, pool: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build the normalized, versioned feature dict for one position.

    Pure function of the (already enriched) position and its pool record.
    Never raises on data problems — it records them in ``gaps``.
    """
    gaps: list = []
    pool = pool if isinstance(pool, dict) else None
    if pool is None:
        gaps.append("pool_record_missing")

    # --- Range geometry (bins/ticks; unit-agnostic) -----------------------
    lower = _i(getattr(position, "lower_bound", None))
    upper = _i(getattr(position, "upper_bound", None))
    current = _i(getattr(position, "current_bin_id", None))
    if lower is not None and upper is not None and current is not None and upper > lower:
        span = upper - lower
        bins_to_lower = current - lower
        bins_to_upper = upper - current
        edge_distance_frac = max(0.0, min(1.0, min(bins_to_lower, bins_to_upper) / (span / 2.0)))
    else:
        bins_to_lower = bins_to_upper = edge_distance_frac = None
        gaps.append("range_unknown")

    # --- Side composition (value-weighted) --------------------------------
    ui_x = _f((getattr(position, "token_x_amount", None) or {}).get("ui"))
    ui_y = _f((getattr(position, "token_y_amount", None) or {}).get("ui"))
    px = _f(getattr(position, "token_x_price_usd", None))
    py = _f(getattr(position, "token_y_price_usd", None))
    value_x = ui_x * px if ui_x is not None and px is not None else 0.0
    value_y = ui_y * py if ui_y is not None and py is not None else 0.0
    if value_x + value_y > 0:
        side_bias_x = value_x / (value_x + value_y)
        single_sided = (value_x > 0) != (value_y > 0)
    else:
        side_bias_x = single_sided = None
        gaps.append("side_unknown")

    # --- Value -------------------------------------------------------------
    value_known = bool(getattr(position, "value_known", False))
    value_usd = _f(getattr(position, "current_value_usd", None))
    if not value_known or value_usd is None:
        value_usd = None
        gaps.append("value_unknown")

    # --- Fees (sentinel-aware: u64::MAX means decode failure, not zero) ----
    raw_fees = list(getattr(position, "fees_owed_raw", None) or [])
    if any(isinstance(v, (int, float)) and v >= _U64_MAX for v in raw_fees):
        fees_usd = None
        gaps.append("fees_unknown")
    else:
        fees_usd = _f(getattr(position, "fees_usd", None))
        if fees_usd is None:
            gaps.append("fees_unknown")
    rewards_usd = _f(getattr(position, "rewards_usd", None))

    # --- Age and realized fee APR ------------------------------------------
    days_open = _f(getattr(position, "days_open", None))
    if days_open is None:
        gaps.append("days_open_unknown")
    fees_apr_pct = None
    if fees_usd is not None and value_usd is not None and days_open is not None:
        if fees_usd > 0 and value_usd > 0 and days_open >= _MIN_DAYS_FOR_APR:
            fees_apr_pct = fees_usd / value_usd / days_open * 365.0 * 100.0
    if fees_apr_pct is None:
        gaps.append("fees_apr_unknown")

    # --- Pool context (joined from the pool scan) ---------------------------
    realized_apr = volatility = tvl = bin_step = None
    pool_score = pool_score_model = pool_score_version = None
    if pool is not None:
        realized_apr = _f(pool.get("realized_fee_apr"))
        volatility = _f(pool.get("volatility"))
        tvl = _f(pool.get("tvl"))
        bin_step = _i(pool.get("bin_step") or pool.get("tick_spacing"))
        if realized_apr is None and volatility is None:
            gaps.append("no_pool_metrics")
        if pool.get("score") is not None:
            pool_score = _f(pool.get("score"))
            pool_score_model = pool.get("score_model")
            pool_score_version = _i(pool.get("score_version"))

    return {
        "version": FEATURES_VERSION,
        "dex": getattr(position, "dex", ""),
        "position_address": getattr(position, "position_address", ""),
        "pool_address": getattr(position, "pool_address", ""),
        "wallet_id": getattr(position, "wallet_id", "main"),
        "status": getattr(position, "status", ""),
        "pair_class": (pool or {}).get("pair_class") or "unknown",
        "in_range": getattr(position, "in_range", None),
        "current_bin_id": current,
        "range_lower": lower,
        "range_upper": upper,
        "range_width": (upper - lower) if lower is not None and upper is not None else None,
        "bins_to_lower": bins_to_lower,
        "bins_to_upper": bins_to_upper,
        "edge_distance_frac": edge_distance_frac,
        "side_bias_x": side_bias_x,
        "single_sided": single_sided,
        "value_usd": value_usd,
        "value_known": value_known,
        "fees_usd": fees_usd,
        "rewards_usd": rewards_usd,
        "fees_apr_pct": fees_apr_pct,
        "days_open": days_open,
        "pool_realized_fee_apr": realized_apr,
        "pool_volatility_pct": volatility,
        "pool_tvl_usd": tvl,
        "pool_bin_step": bin_step,
        "pool_score": pool_score,
        "pool_score_model": pool_score_model,
        "pool_score_version": pool_score_version,
        "gaps": gaps,
    }
