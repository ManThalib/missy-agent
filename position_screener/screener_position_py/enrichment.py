"""Enrich on-chain position records with pool-derived prices and USD values."""

from __future__ import annotations

import glob
import json
import math
import os
import time
from typing import Any, Dict, List, Optional

from .liquidity_position import LiquidityPosition


def _latest_pool_scan() -> Optional[str]:
    """Return the most recent pool scan JSON path, or None."""
    outdir = "/data/missy-data/pool_screens"
    paths = sorted(
        p for p in glob.glob(os.path.join(outdir, "pool_scan-*.json"))
        if not p.endswith((".failed", ".invalid"))
    )
    return paths[-1] if paths else None


def _load_pool_data(path: str) -> Dict[str, Dict[str, Any]]:
    """Load pool scan and return pool_address -> pool dict."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            pools = json.load(fh)
        if isinstance(pools, dict):
            pools = pools.get("pools", [])
        if not isinstance(pools, list):
            return {}
        return {
            p["pool_address"]: p
            for p in pools
            if isinstance(p, dict) and p.get("pool_address")
        }
    except Exception:
        return {}


def _token_prices(pool: Dict[str, Any]) -> tuple:
    """Return (price_x, price_y, dec_x, dec_y) from a pool record."""
    px = float(pool.get("token_x_price_usd") or 0.0)
    py = float(pool.get("token_y_price_usd") or 0.0)
    dx = int(pool.get("token_x_decimals") or 0)
    dy = int(pool.get("token_y_decimals") or 0)
    return px, py, dx, dy


def _safe_exp(value: float) -> float:
    """math.exp without OverflowError: clamp to float exp bounds.

    Arguments beyond ~709.78 overflow and below ~-745 underflow; absurd
    tick/bin values from anomalous pool records must degrade to inf/0.0
    instead of raising and killing the scan.
    """
    if value >= 709.78:
        return math.inf
    if value <= -745.0:
        return 0.0
    return math.exp(value)


def _finite_or_zero(value: float) -> float:
    """Map non-finite enrichment values (inf/nan) to 0.0 for valid JSON."""
    return value if math.isfinite(value) else 0.0


def _tick_to_price_ratio(tick: int) -> float:
    """CLMM sqrt-price ratio for a tick. Scale cancels in comparisons."""
    return _safe_exp(tick * math.log(1.0001) / 2.0)


def _bin_to_price_ratio(bin_id: int, bin_step: int) -> float:
    """Meteora DLMM relative price for a bin id."""
    return _safe_exp(bin_id * math.log(1.0 + bin_step / 10000.0))


def _clmm_amounts(liquidity_raw: int, current_tick: int, lower: int, upper: int) -> tuple:
    """Return (amount_x, amount_y) in raw units for a CLMM position."""
    if lower >= upper or liquidity_raw <= 0:
        return 0.0, 0.0
    sqrt_p = _tick_to_price_ratio(current_tick)
    sqrt_pl = _tick_to_price_ratio(lower)
    sqrt_pu = _tick_to_price_ratio(upper)
    if not all(math.isfinite(r) for r in (sqrt_p, sqrt_pl, sqrt_pu)):
        # Garbage tick data (overflowed ratios): amounts are meaningless.
        return 0.0, 0.0
    L = liquidity_raw

    if current_tick <= lower:
        # All in token X
        amount_x = L * (sqrt_pu - sqrt_pl) / (sqrt_pu * sqrt_pl)
        return amount_x, 0.0
    if current_tick >= upper:
        # All in token Y
        amount_y = L * (sqrt_pu - sqrt_pl)
        return 0.0, amount_y

    amount_x = L * (sqrt_pu - sqrt_p) / (sqrt_pu * sqrt_p)
    amount_y = L * (sqrt_p - sqrt_pl)
    return amount_x, amount_y


def _meteora_amounts(liquidity_raw: int, pool: Dict[str, Any]) -> tuple:
    """Meteora DLMM token amounts cannot be derived from liquidity_raw alone.

    Per-bin share * bin reserves / bin liquidity_supply is required. Until
    the scanner computes this from bin arrays, enrichment returns zero and
    flags the value as unknown rather than emitting a bogus estimate.
    """
    return 0.0, 0.0


def _derive_current_tick(pool: Dict[str, Any]) -> int:
    """Return best-effort current tick from pool scan fields."""
    tick = int(pool.get("current_tick_index") or pool.get("active_bin_id") or 0)
    if tick != 0:
        return tick
    pool_price = float(pool.get("pool_price") or 0.0)
    dec_x = int(pool.get("token_x_decimals") or 0)
    dec_y = int(pool.get("token_y_decimals") or 0)
    if pool_price > 0 and dec_x > 0 and dec_y > 0:
        raw_price = pool_price * (10 ** (dec_y - dec_x))
        if raw_price > 0:
            return int(math.log(raw_price) / math.log(1.0001))
    return 0


def _position_amounts(
    position: LiquidityPosition, pool: Dict[str, Any]
) -> tuple:
    """Return (amount_x, amount_y) in raw integer units."""
    dex = position.dex
    if dex in ("orca", "raydium"):
        current_tick = _derive_current_tick(pool)
        return _clmm_amounts(
            position.liquidity_raw,
            current_tick,
            position.lower_bound or 0,
            position.upper_bound or 0,
        )
    if dex == "meteora":
        return _meteora_amounts(position.liquidity_raw, pool)
    return 0.0, 0.0


def _compute_value(
    position: LiquidityPosition, pool: Dict[str, Any]
) -> float:
    """Estimate current position USD value from liquidity and pool state."""
    px, py, dx, dy = _token_prices(pool)
    if px <= 0 or py <= 0 or dx <= 0 or dy <= 0:
        return 0.0

    amount_x, amount_y = _position_amounts(position, pool)
    real_x = amount_x / (10 ** dx)
    real_y = amount_y / (10 ** dy)
    return real_x * px + real_y * py


def _sanitize_raw_amount(value: int, decimals: int = 9) -> float:
    """Convert a raw on-chain amount to UI, treating only u64-max as sentinel."""
    if value <= 0 or value >= (1 << 64) - 1:
        return 0.0
    return float(value) / (10 ** decimals)


def _compute_rewards(
    position: LiquidityPosition,
    pool: Dict[str, Any],
    extra_prices: Optional[Dict[str, float]] = None,
) -> float:
    """Convert raw pending reward amounts to USD.

    Reward mints/decimals come from the Raydium pending-fees recomputation.
    Pair-token rewards price from the pool record; third-party rewards (e.g.
    RAY) price from ``extra_prices`` (Jupiter, fetched once per scan). Slots
    with no price data contribute 0. The system-program placeholder mint
    marks an inactive slot: raw amount is already 0, skip pricing.
    """
    rewards = position.rewards_owed_raw or []
    mints = getattr(position, "reward_mints", None) or []
    decimals = getattr(position, "reward_decimals", None) or []
    if not rewards or len(mints) != len(rewards):
        return 0.0
    px, py, _, _ = _token_prices(pool)
    mint_x = str(pool.get("token_x_address") or "")
    mint_y = str(pool.get("token_y_address") or "")
    extra_prices = extra_prices or {}
    total = 0.0
    for i, raw in enumerate(rewards):
        raw = int(raw)
        if raw <= 0:
            continue
        mint = mints[i]
        dec = int(decimals[i]) if i < len(decimals) and decimals[i] is not None else 0
        if dec <= 0:
            continue
        if mint == mint_x and px > 0:
            price = px
        elif mint == mint_y and py > 0:
            price = py
        else:
            price = float(extra_prices.get(mint) or 0.0)
        if price <= 0:
            continue
        total += raw / (10 ** dec) * price
    return total


def _compute_fees(position: LiquidityPosition, pool: Dict[str, Any]) -> float:
    """Convert raw fee amounts to USD using pool token prices."""
    px, py, dx, dy = _token_prices(pool)
    fees = position.fees_owed_raw or []
    if len(fees) < 2:
        return 0.0
    if px <= 0 or py <= 0 or dx <= 0 or dy <= 0:
        return 0.0
    fx = _sanitize_raw_amount(int(fees[0]), dx) * px
    fy = _sanitize_raw_amount(int(fees[1]), dy) * py
    return fx + fy


def _enrich_one(
    position: LiquidityPosition,
    pools: Dict[str, Dict[str, Any]],
    now: float,
    rewards_cache: Optional[Dict[str, Any]] = None,
) -> None:
    """Enrich a single position; raises so the caller can isolate failures."""
    pool = pools.get(position.pool_address)
    if not pool:
        return

    px, py, _, _ = _token_prices(pool)
    position.token_x_price_usd = px
    position.token_y_price_usd = py
    position.token_x_amount = {"raw": str(position.liquidity_raw), "ui": 0.0}
    position.token_y_amount = {"raw": str(position.liquidity_raw), "ui": 0.0}

    if position.dex in ("orca", "raydium"):
        pool_price = float(pool.get("pool_price") or 0.0)
        dec_x = int(pool.get("token_x_decimals") or 0)
        dec_y = int(pool.get("token_y_decimals") or 0)
        current_tick = int(
            pool.get("current_tick_index")
            or pool.get("active_bin_id")
            or 0
        )
        # Orca API does not always expose current_tick_index; derive it from
        # the human pool price so bounds are on the same scale as the price.
        if current_tick == 0 and pool_price > 0 and dec_x > 0 and dec_y > 0:
            raw_price = pool_price * (10 ** (dec_y - dec_x))
            if raw_price > 0:
                current_tick = int(math.log(raw_price) / math.log(1.0001))

        if pool_price > 0 and current_tick != 0:
            position.current_price = pool_price
            lower_delta = (position.lower_bound or 0) - current_tick
            upper_delta = (position.upper_bound or 0) - current_tick
            position.lower_price = _finite_or_zero(
                pool_price * _safe_exp(lower_delta * math.log(1.0001))
            )
            position.upper_price = _finite_or_zero(
                pool_price * _safe_exp(upper_delta * math.log(1.0001))
            )
        else:
            position.current_price = _finite_or_zero(
                _tick_to_price_ratio(current_tick)
            )
            position.lower_price = _finite_or_zero(
                _tick_to_price_ratio(position.lower_bound or 0)
            )
            position.upper_price = _finite_or_zero(
                _tick_to_price_ratio(position.upper_bound or 0)
            )
        position.current_bin_id = current_tick
        position.in_range = (
            position.lower_bound is not None
            and position.upper_bound is not None
            and (position.lower_bound <= current_tick <= position.upper_bound)
        )
    elif position.dex == "meteora":
        bin_step = int(pool.get("bin_step") or pool.get("tick_spacing") or 0)
        active_bin = int(pool.get("active_bin_id") or 0)
        lower = position.lower_bound or 0
        upper = position.upper_bound or 0
        pool_price = float(pool.get("pool_price") or 0.0)
        if bin_step > 0:
            if pool_price > 0:
                # Prefer the human pool price: it already carries the
                # decimal adjustment, and recorded bin ids can be
                # anomalous (a bad active_bin_id must not poison price
                # math). Bound prices scale relative to the active bin.
                log_bin = math.log(1.0 + bin_step / 10000.0)
                position.current_price = pool_price
                position.lower_price = _finite_or_zero(
                    pool_price * _safe_exp((lower - active_bin) * log_bin)
                )
                position.upper_price = _finite_or_zero(
                    pool_price * _safe_exp((upper - active_bin) * log_bin)
                )
            else:
                position.current_price = _finite_or_zero(
                    _bin_to_price_ratio(active_bin, bin_step)
                )
                position.lower_price = _finite_or_zero(
                    _bin_to_price_ratio(lower, bin_step)
                )
                position.upper_price = _finite_or_zero(
                    _bin_to_price_ratio(upper, bin_step)
                )
        else:
            position.current_price = float(active_bin)
            position.lower_price = float(lower)
            position.upper_price = float(upper)
        position.current_bin_id = active_bin
        position.in_range = lower <= active_bin <= upper

    position.fees_usd = _compute_fees(position, pool)
    position.rewards_usd = _compute_rewards(
        position, pool, rewards_cache.get("prices") if rewards_cache else None
    )
    _dx = int(pool.get("token_x_decimals") or 0)
    _dy = int(pool.get("token_y_decimals") or 0)
    _amount_x, _amount_y = _position_amounts(position, pool)
    position.current_value_usd = _compute_value(position, pool)

    # Meteora DLMM value cannot be derived from liquidity_raw alone; flag it
    # as unknown instead of trusting the zero/placeholder value.
    if position.dex == "meteora":
        position.current_value_usd = 0.0
        position.value_known = False
        _amount_x, _amount_y = 0.0, 0.0
        if position.pool_enrichment is None:
            position.pool_enrichment = {}
        position.pool_enrichment["value_known"] = False
        position.pool_enrichment["value_source"] = "meteora_amounts_not_implemented"

    position.token_x_amount = {
        "raw": str(int(_amount_x)) if _amount_x > 0 else "0",
        "ui": _amount_x / (10 ** _dx) if _dx > 0 and _amount_x > 0 else 0.0,
    }
    position.token_y_amount = {
        "raw": str(int(_amount_y)) if _amount_y > 0 else "0",
        "ui": _amount_y / (10 ** _dy) if _dy > 0 and _amount_y > 0 else 0.0,
    }
    position.days_open = 0.0
    if position.opened_at:
        position.days_open = max(0.0, (now - position.opened_at) / 86400.0)

    # Store token prices in pool_enrichment for consumers that look there.
    if position.pool_enrichment is None:
        position.pool_enrichment = {}
    position.pool_enrichment.update(
        {
            "token_x_price_usd": px,
            "token_y_price_usd": py,
            "position_value_usd": position.current_value_usd,
            "current_price": position.current_price,
            "lower_price": position.lower_price,
            "upper_price": position.upper_price,
            "fees_usd": position.fees_usd,
            "current_value_usd": position.current_value_usd,
            "days_open": position.days_open,
        }
    )


def enrich_positions(
    positions: List[LiquidityPosition],
    errors: Optional[Dict[str, str]] = None,
) -> None:
    """Mutate positions in place with pool-derived prices and USD values.

    A failure while enriching one position is recorded (into ``errors`` when
    provided, and on the position itself) and must not abort the scan.
    """
    scan_path = _latest_pool_scan()
    if not scan_path:
        return
    pools = _load_pool_data(scan_path)
    now = time.time()

    # Third-party reward tokens (e.g. RAY) price via Jupiter once per scan.
    reward_mints = {
        mint
        for position in positions
        for mint in (getattr(position, "reward_mints", None) or [])
        if mint and not mint.startswith("1111")
    }
    rewards_cache: Dict[str, Any] = {"prices": {}}
    if reward_mints:
        try:
            from core.prices import JupiterPriceClient

            rewards_cache["prices"] = JupiterPriceClient().fetch_prices(
                sorted(reward_mints)
            )
        except Exception as exc:
            if errors is not None:
                errors["reward_prices"] = str(exc)

    for position in positions:
        try:
            _enrich_one(position, pools, now, rewards_cache)
        except Exception as exc:
            # One bad pool record must not kill the whole scan.
            if position.pool_enrichment is None:
                position.pool_enrichment = {}
            position.pool_enrichment["enrichment_error"] = str(exc)
            if errors is not None:
                key = position.pool_address or position.position_address
                errors[f"enrichment:{key}"] = str(exc)
