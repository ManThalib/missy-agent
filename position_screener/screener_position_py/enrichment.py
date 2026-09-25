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


def _tick_to_price_ratio(tick: int) -> float:
    """CLMM sqrt-price ratio for a tick. Scale cancels in comparisons."""
    return math.exp(tick * math.log(1.0001) / 2.0)


def _bin_to_price_ratio(bin_id: int, bin_step: int) -> float:
    """Meteora DLMM relative price for a bin id."""
    return math.exp(bin_id * math.log(1.0 + bin_step / 10000.0))


def _clmm_amounts(liquidity_raw: int, current_tick: int, lower: int, upper: int) -> tuple:
    """Return (amount_x, amount_y) in raw units for a CLMM position."""
    if lower >= upper or liquidity_raw <= 0:
        return 0.0, 0.0
    sqrt_p = _tick_to_price_ratio(current_tick)
    sqrt_pl = _tick_to_price_ratio(lower)
    sqrt_pu = _tick_to_price_ratio(upper)
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
    """Best-effort token amounts for a Meteora DLMM position.

    Without per-bin share iteration we cannot derive exact amounts; return a
    rough estimate proportional to liquidity and pool price.
    """
    pool_price = float(pool.get("pool_price") or 0.0)
    if pool_price <= 0 or liquidity_raw <= 0:
        return 0.0, 0.0
    # Rough estimate: liquidity_raw is treated as a share-like quantity in the
    # active bin. This is intentionally approximate.
    return float(liquidity_raw) / pool_price, float(liquidity_raw)


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
    """Convert a raw on-chain amount to UI, treating sentinels as zero."""
    if value <= 0 or value >= (1 << 64) - 1 or value >= (1 << 32) - 1:
        return 0.0
    return float(value) / (10 ** decimals)


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


def enrich_positions(positions: List[LiquidityPosition]) -> None:
    """Mutate positions in place with pool-derived prices and USD values."""
    scan_path = _latest_pool_scan()
    if not scan_path:
        return
    pools = _load_pool_data(scan_path)
    now = time.time()

    for position in positions:
        pool = pools.get(position.pool_address)
        if not pool:
            continue

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
                current_tick = int(math.log(raw_price) / math.log(1.0001))

            if pool_price > 0 and current_tick != 0:
                position.current_price = pool_price
                lower_delta = (position.lower_bound or 0) - current_tick
                upper_delta = (position.upper_bound or 0) - current_tick
                position.lower_price = pool_price * math.exp(lower_delta * math.log(1.0001))
                position.upper_price = pool_price * math.exp(upper_delta * math.log(1.0001))
            else:
                position.current_price = _tick_to_price_ratio(current_tick)
                position.lower_price = _tick_to_price_ratio(position.lower_bound or 0)
                position.upper_price = _tick_to_price_ratio(position.upper_bound or 0)
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
            if bin_step > 0:
                position.current_price = _bin_to_price_ratio(active_bin, bin_step)
                position.lower_price = _bin_to_price_ratio(lower, bin_step)
                position.upper_price = _bin_to_price_ratio(upper, bin_step)
            else:
                position.current_price = float(active_bin)
                position.lower_price = float(lower)
                position.upper_price = float(upper)
            current_bin = active_bin
            position.in_range = lower <= current_bin <= upper

        position.fees_usd = _compute_fees(position, pool)
        position.current_value_usd = _compute_value(position, pool)
        _dx = int(pool.get("token_x_decimals") or 0)
        _dy = int(pool.get("token_y_decimals") or 0)
        _amount_x, _amount_y = _position_amounts(position, pool)
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
