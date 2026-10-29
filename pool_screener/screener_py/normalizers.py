"""Provider payload normalization: DEX-specific pool dicts -> common shape.

``normalize_pool()`` is the only place provider payloads are converted. The
screener filters and scores the normalized shape, so filtering stays
provider-independent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .normalize_utils import (
    finite_float,
    integer,
    mapping,
    token_entry,
    top_level_token_metadata,
)


_ORCA_TIMEFRAMES = {"day": "24h", "week": "7d", "month": "30d"}
_WINDOW_DAYS = {"day": 1.0, "week": 7.0, "month": 30.0}

def _yield_over_tvl_apr(fee_usd: float, tvl_usd: float, window: str) -> float:
    """Annualize fee/tvl the same way Orca does for 24h/7d/30d windows."""
    if tvl_usd <= 0.0 or fee_usd <= 0.0:
        return 0.0
    days = _WINDOW_DAYS.get(window, 1.0)
    return (fee_usd / tvl_usd) * 100.0 * (365.0 / days)


def _apply_price_map(token_data: Mapping[str, Any], price_map: Mapping[str, float]) -> Dict[str, Any]:
    address = token_data.get("address")
    if address and address in price_map:
        token_data = dict(token_data)
        token_data["price_usd"] = price_map[address]
    return token_data


def _range_volatility(price: float, price_min: float, price_max: float) -> Tuple[float, bool]:
    """Common range volatility = (max - min) / price * 100."""
    if price > 0 and price_min > 0 and price_max > 0:
        return (price_max - price_min) / price * 100.0, True
    return 0.0, False


# Orca's priceHistory7d is sampled every 12 hours (14 points over 7 days).
# A day estimate must span the full last 24h: 3 samples (two 12h intervals)
# plus the live price. Two samples span a single 12h interval and understate
# a day range ~5x (2026-10-14 Czfq3xZZ incident: SOL-USDC read 0.137% vs a
# 6.63% true day range, opening a 20-tick Whirlpool range).
_MIN_DAY_HISTORY_SAMPLES = 3
# Any range estimate needs at least two distinct points.
_MIN_HISTORY_SAMPLES = 2


def _min_price_history_samples(window: str) -> int:
    """Minimum history samples required for a trustworthy window estimate."""
    return _MIN_DAY_HISTORY_SAMPLES if window == "day" else _MIN_HISTORY_SAMPLES


def _price_history_volatility(current_price: float, prices: List[float], window: str) -> Tuple[float, bool]:
    """Range volatility from Orca's 12h-sampled 7-day price history.

    Same estimator semantics as _range_volatility — (max - min) / current
    * 100 — over the samples covering the requested window:
      day:   last 3 samples (two 12h intervals = 24h) + the live price.
      week:  all samples (7d).
      month: all samples (no 30d history exists; 7d is the best available).

    Fail-closed: when the series is too short for the requested window,
    return available=False instead of a silently narrow number. Callers
    can distinguish this case via _min_price_history_samples(window).
    """
    if current_price <= 0 or not prices:
        return 0.0, False
    if window == "day":
        if len(prices) < _MIN_DAY_HISTORY_SAMPLES:
            return 0.0, False
        window_prices = list(prices[-_MIN_DAY_HISTORY_SAMPLES:])
    else:
        if len(prices) < _MIN_HISTORY_SAMPLES:
            return 0.0, False
        window_prices = list(prices)
    window_prices.append(current_price)
    return (max(window_prices) - min(window_prices)) / current_price * 100.0, True


def _common_apr(dex: str, fee_usd: float, tvl_usd: float, window: str, reported_apr: float) -> Tuple[float, str]:
    """Return APR using a common fee/tvl annualisation, with provider fallback."""
    if tvl_usd > 0 and fee_usd > 0:
        apr = _yield_over_tvl_apr(fee_usd, tvl_usd, window)
        return apr, f"{dex}_fee_over_tvl_{window}"
    if reported_apr:
        return reported_apr, "provider_reported"
    return 0.0, "unavailable"


def normalize_pool(
    pool: Dict[str, Any],
    window: str = "day",
    price_map: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """Converts a provider pool into the common shape used by the screener."""
    if not isinstance(pool, dict):
        raise ValueError("pool payload must be an object")

    if "mintA" in pool or "mintB" in pool:
        mint_a = _apply_price_map(mapping(pool.get("mintA")), price_map or {})
        mint_b = _apply_price_map(mapping(pool.get("mintB")), price_map or {})
        token_x = token_entry(mint_a)
        token_y = token_entry(mint_b)
        name = f"{mint_a.get('symbol', '?')}-{mint_b.get('symbol', '?')}"
        pool_address = pool.get("id", "")
        pool_type = pool.get("type", "")
        tvl = finite_float(pool.get("tvl"), "TVL")
        fee_rate = finite_float(pool.get("feeRate"), "fee rate")

        period = mapping(pool.get(window) or pool.get("day"))
        volume = finite_float(period.get("volume"), "volume")
        volume_fee = finite_float(period.get("volumeFee"), "fees")
        reported_apr = finite_float(period.get("apr"), "APR") or finite_float(
            period.get("feeApr"), "fee APR"
        )
        apr, apr_source = _common_apr(
            "raydium", volume_fee, tvl, window, reported_apr
        )
        price = finite_float(pool.get("price"), "price")
        price_min = finite_float(period.get("priceMin"), "minimum price")
        price_max = finite_float(period.get("priceMax"), "maximum price")
        volatility, volatility_available = _range_volatility(price, price_min, price_max)

        fee_tvl_ratio = (volume_fee / tvl * 100.0) if tvl > 0 and volume_fee else 0.0

        provider_config = mapping(pool.get("config"))
        tick_spacing = integer(provider_config.get("tickSpacing"), "tick spacing")

        return {
            "dex": pool.get("_dex", "raydium"),
            "pool_address": pool_address,
            "name": name,
            "provider_name": name,
            "pool_type": pool_type,
            "tvl": tvl,
            "fee_tvl_ratio": fee_tvl_ratio,
            "fee": volume_fee,
            "volume": volume,
            "volatility": volatility,
            "volatility_available": volatility_available,
            "volatility_source": f"raydium_{window}_range" if volatility_available else "raydium_unavailable",
            "volatility_window": window,
            "apr": apr,
            "apr_source": apr_source,
            "apr_window": window,
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            # Orca/Raydium are tick-based, not bin-based; keep bin_step honest.
            "bin_step": 0,
            "token_x": token_x,
            "token_y": token_y,
            "pool_price": price,
            "active_bin_id": 0,
            "current_tick_index": 0,
            **top_level_token_metadata(token_x, token_y),
        }

    if pool.get("_dex") == "orca" or "tvlUsdc" in pool:
        token_a = token_entry(
            _apply_price_map(mapping(pool.get("tokenA")), price_map or {}),
            default_address=pool.get("tokenMintA", ""),
        )
        token_b = token_entry(
            _apply_price_map(mapping(pool.get("tokenB")), price_map or {}),
            default_address=pool.get("tokenMintB", ""),
        )
        timeframe = _ORCA_TIMEFRAMES.get(window, "24h")
        stats = mapping(mapping(pool.get("stats")).get(timeframe))
        tvl = finite_float(pool.get("tvlUsdc"), "TVL")
        fees = finite_float(stats.get("fees"), "fees")
        price_history = pool.get("priceHistory7d") or []
        if not isinstance(price_history, list):
            raise ValueError("invalid price history: expected a list")
        prices = [
            finite_float(value, "price history value")
            for value in price_history
            if value is not None
        ]
        current_price = finite_float(pool.get("price"), "price")
        volatility, volatility_available = _price_history_volatility(
            current_price, prices, window
        )
        if volatility_available:
            volatility_source = f"orca_price_history_7d_{window}"
        elif len(prices) < _min_price_history_samples(window):
            # Too few history samples for the window; the estimate was
            # withheld rather than narrowed (see _price_history_volatility).
            volatility_source = "orca_price_history_insufficient"
        else:
            volatility_source = "orca_unavailable"
        orca_name = f"{token_a['symbol']}-{token_b['symbol']}"
        fee_rate = finite_float(pool.get("feeRate"), "fee rate") / 1_000_000.0
        tick_spacing = integer(pool.get("tickSpacing"), "tick spacing")
        current_tick_index = integer(
            pool.get("tickCurrentIndex")
            or pool.get("currentTickIndex")
            or pool.get("current_tick_index")
            or 0,
            "tickCurrentIndex",
        )
        window_days = _WINDOW_DAYS.get(window, 1.0)
        reported_apr = (
            finite_float(stats.get("yieldOverTvl"), "yield over TVL")
            * 100.0
            * 365.0
            / window_days
        )
        apr, apr_source = _common_apr("orca", fees, tvl, window, reported_apr)
        return {
            "dex": "orca",
            "pool_address": pool.get("address", ""),
            "name": orca_name,
            "provider_name": orca_name,
            "pool_type": "Splash" if tick_spacing == 32896 else "Whirlpool",
            "tvl": tvl,
            "fee_tvl_ratio": (fees / tvl * 100.0) if tvl > 0 else 0.0,
            "fee": fees,
            "volume": finite_float(stats.get("volume"), "volume"),
            "volatility": volatility,
            "volatility_available": volatility_available,
            "volatility_source": volatility_source,
            "volatility_window": window,
            "apr": apr,
            "apr_source": apr_source,
            "apr_window": window,
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            "bin_step": 0,
            "token_x": token_a,
            "token_y": token_b,
            "current_tick_index": current_tick_index,
            **top_level_token_metadata(token_a, token_b),
            "pool_price": current_price,
            # Orca uses ticks; bin_step is a Meteora-only concept.
            "active_bin_id": 0,
        }

    if pool.get("_dex") == "meteora" or "dlmm_params" in pool:
        normalized = dict(pool)
        normalized["dex"] = "meteora"
        # Discovery payloads may carry the pool id as `address` (as Orca does).
        normalized.setdefault("pool_address", pool.get("address", ""))
        normalized["pool_type"] = "DLMM"
        tick_spacing = integer(
            mapping(pool.get("dlmm_params")).get("bin_step"), "bin step"
        )
        normalized["tick_spacing"] = tick_spacing
        normalized["bin_step"] = tick_spacing
        for field in ("tvl", "fee", "volume", "apr", "fee_pct"):
            normalized[field] = finite_float(pool.get(field), field)
        normalized.setdefault("fee_rate", normalized["fee_pct"] / 100.0)
        current_price = finite_float(
            pool.get("pool_price", pool.get("price")), "price"
        )
        price_min = finite_float(pool.get("min_price"), "minimum price")
        price_max = finite_float(pool.get("max_price"), "maximum price")
        volatility, volatility_available = _range_volatility(current_price, price_min, price_max)
        if "volatility" in pool and volatility == 0.0:
            volatility = finite_float(pool.get("volatility"), "volatility")
            volatility_available = True
        normalized["volatility"] = volatility
        normalized["volatility_available"] = volatility_available
        normalized["volatility_source"] = f"meteora_{window}_range" if volatility_available else "meteora_unavailable"
        normalized["volatility_window"] = window
        reported_apr = finite_float(pool.get("apr"), "APR")
        apr, apr_source = _common_apr("meteora", normalized["fee"], normalized["tvl"], window, reported_apr)
        normalized["apr"] = apr
        normalized["apr_source"] = apr_source
        normalized["apr_window"] = window
        token_x = token_entry(_apply_price_map(mapping(pool.get("token_x")), price_map or {}))
        token_y = token_entry(_apply_price_map(mapping(pool.get("token_y")), price_map or {}))
        normalized["token_x"] = token_x
        normalized["token_y"] = token_y
        normalized.update(top_level_token_metadata(token_x, token_y))
        meteora_name = f"{token_x['symbol']}-{token_y['symbol']}"
        normalized["name"] = meteora_name
        normalized["provider_name"] = meteora_name
        normalized["active_bin_id"] = integer(
            pool.get("activeId") or pool.get("active_bin_id") or 0, "activeId"
        )
        return normalized

    # Already normalized (or legacy Meteora shape); pass it through.
    return dict(pool)
