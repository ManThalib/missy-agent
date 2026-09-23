"""Provider normalization and shared multi-DEX screening policy."""

import base64
from operator import attrgetter
from typing import Any, Dict, List, Optional, Tuple

from .candidate import Candidate
from .client import MeteoraClient, MultiDexClient, OrcaClient, RaydiumClient
from .config import DEFAULT_CONFIG, FilterConfig
from .normalize_utils import (
    finite_float,
    integer,
    mapping,
    token_entry,
    top_level_token_metadata,
)
from .scoring import PoolScoreInput, PoolScorer
from .whitelist import Whitelist, normalize_symbol

__all__ = [
    "RaydiumScreener",
    "MultiDexScreener",
    "MeteoraScreener",
    "Whitelist",
    "FilterConfig",
    "DEFAULT_CONFIG",
    "Candidate",
    "RaydiumClient",
    "OrcaClient",
    "MultiDexClient",
    "MeteoraClient",
    "normalize_pool",
]

_ORCA_TIMEFRAMES = {"day": "24h", "week": "7d", "month": "30d"}
_WINDOW_DAYS = {"day": 1.0, "week": 7.0, "month": 30.0}


def _decode_active_bin_id(account_data: bytes) -> int:
    """Extract activeId from Meteora DLMM LbPair account data.

    The LbPair account layout has activeId as the first u32 field after the
    8-byte discriminator, at offset 8.
    """
    if len(account_data) < 12:
        return 0
    active_id = int.from_bytes(account_data[8:12], "little")
    return active_id


def _fetch_active_bin_id(rpc: Any, pool_address: str) -> int:
    """Fetch activeId from a Meteora DLMM LbPair account via RPC.

    Returns 0 if the call fails or the field cannot be decoded.
    """
    try:
        result = rpc.get_account_info(pool_address)
        data = result.get("data", "") if isinstance(result, dict) else ""
        if isinstance(data, str):
            decoded = base64.b64decode(data)
        elif isinstance(data, bytes):
            decoded = data
        else:
            decoded = b""
        return _decode_active_bin_id(decoded)
    except Exception:
        return 0


def _volatility(price: float, price_min: float, price_max: float) -> float:
    if price > 0 and price_min > 0 and price_max > 0:
        return abs(price_max - price_min) / price * 100.0
    return 0.0


def normalize_pool(pool: Dict[str, Any], window: str = "day") -> Dict[str, Any]:
    """Converts a provider pool into the common shape used by the screener."""
    if not isinstance(pool, dict):
        raise ValueError("pool payload must be an object")

    if "mintA" in pool or "mintB" in pool:
        mint_a = mapping(pool.get("mintA"))
        mint_b = mapping(pool.get("mintB"))
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
        apr = finite_float(period.get("apr"), "APR")
        if not apr:
            apr = finite_float(period.get("feeApr"), "fee APR")
        price = finite_float(pool.get("price"), "price")
        price_min = finite_float(period.get("priceMin"), "minimum price")
        price_max = finite_float(period.get("priceMax"), "maximum price")
        volatility = _volatility(price, price_min, price_max)

        fee_tvl_ratio = (volume_fee / tvl * 100.0) if tvl > 0 and volume_fee else 0.0

        provider_config = mapping(pool.get("config"))
        tick_spacing = integer(provider_config.get("tickSpacing"), "tick spacing")

        return {
            "dex": pool.get("_dex", "raydium"),
            "pool_address": pool_address,
            "name": name,
            "pool_type": pool_type,
            "tvl": tvl,
            "fee_tvl_ratio": fee_tvl_ratio,
            "fee": volume_fee,
            "volume": volume,
            "volatility": volatility,
            "volatility_available": (price > 0 and price_min > 0 and price_max > 0),
            "apr": apr,
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            "bin_step": tick_spacing,
            "token_x": token_x,
            "token_y": token_y,
            "pool_price": price,
            "active_bin_id": 0,
            "current_tick_index": 0,
            **top_level_token_metadata(token_x, token_y),
        }

    if pool.get("_dex") == "orca" or "tvlUsdc" in pool:
        token_a = token_entry(
            pool.get("tokenA"), default_address=pool.get("tokenMintA", "")
        )
        token_b = token_entry(
            pool.get("tokenB"), default_address=pool.get("tokenMintB", "")
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
        volatility = 0.0
        volatility_available = timeframe == "7d" and current_price > 0 and bool(prices)
        if volatility_available:
            volatility = (max(prices) - min(prices)) / current_price * 100.0
        fee_rate = finite_float(pool.get("feeRate"), "fee rate") / 1_000_000.0
        tick_spacing = integer(pool.get("tickSpacing"), "tick spacing")
        current_tick_index = (
            finite_float(pool.get("tickCurrentIndex"), "tickCurrentIndex") or 0
        )
        return {
            "dex": "orca",
            "pool_address": pool.get("address", ""),
            "name": f"{token_a['symbol']}-{token_b['symbol']}",
            "pool_type": "Splash" if tick_spacing == 32896 else "Whirlpool",
            "tvl": tvl,
            "fee_tvl_ratio": (fees / tvl * 100.0) if tvl > 0 else 0.0,
            "fee": fees,
            "volume": finite_float(stats.get("volume"), "volume"),
            "volatility": volatility,
            "volatility_available": volatility_available,
            "apr": finite_float(stats.get("yieldOverTvl"), "yield over TVL")
            * 100.0
            * (
                365.0
                if timeframe == "24h"
                else 365.0 / (7.0 if timeframe == "7d" else 30.0)
            ),
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            "bin_step": tick_spacing,
            "token_x": token_a,
            "token_y": token_b,
            "current_tick_index": current_tick_index,
            **top_level_token_metadata(token_a, token_b),
            "pool_price": current_price,
            "active_bin_id": 0,
        }

    if pool.get("_dex") == "meteora" or "dlmm_params" in pool:
        normalized = dict(pool)
        normalized["dex"] = "meteora"
        normalized["pool_type"] = "DLMM"
        normalized["tick_spacing"] = integer(
            mapping(pool.get("dlmm_params")).get("bin_step"), "bin step"
        )
        normalized["bin_step"] = normalized["tick_spacing"]
        for field in ("tvl", "fee", "volume", "apr", "fee_pct"):
            normalized[field] = finite_float(pool.get(field), field)
        normalized.setdefault("fee_rate", normalized["fee_pct"] / 100.0)
        current_price = finite_float(
            pool.get("pool_price", pool.get("price")), "price"
        )
        price_min = finite_float(pool.get("min_price"), "minimum price")
        price_max = finite_float(pool.get("max_price"), "maximum price")
        normalized["volatility"] = _volatility(current_price, price_min, price_max)
        normalized["volatility_available"] = (
            current_price > 0 and price_min > 0 and price_max > 0
        ) or "volatility" in pool
        if "volatility" in pool and normalized["volatility"] == 0.0:
            normalized["volatility"] = finite_float(pool.get("volatility"), "volatility")
        token_x = token_entry(pool.get("tokenX"))
        token_y = token_entry(pool.get("tokenY"))
        normalized["token_x"] = token_x
        normalized["token_y"] = token_y
        normalized.update(top_level_token_metadata(token_x, token_y))
        normalized["active_bin_id"] = finite_float(pool.get("activeId"), "activeId") or 0
        normalized["current_tick_index"] = normalized["active_bin_id"]
        return normalized

    # Already normalized (or legacy Meteora shape); pass it through.
    return dict(pool)


class MultiDexScreener:
    """Evaluate normalized DEX pools against token and economic policies."""

    def __init__(
        self,
        whitelist: Whitelist,
        config: Optional[FilterConfig] = None,
        client: Optional[Any] = None,
        rpc: Optional[Any] = None,
    ):
        self.whitelist = whitelist
        self.config = config or FilterConfig()
        self.client = client or MultiDexClient(timeout=self.config.timeout)
        self.rpc = rpc
        self.scorer = PoolScorer()

    def screen_pool(self, p: Dict[str, Any]) -> Tuple[Optional[Candidate], str]:
        n = normalize_pool(p, window=self.config.window)
        token_x = n.get("token_x") or {}
        token_y = n.get("token_y") or {}

        # 1. Whitelist check
        is_valid_pair, target_token, paired_token, pair_reason = (
            self.whitelist.validate_pair(token_x, token_y)
        )
        if not is_valid_pair:
            return None, pair_reason

        cfg = self.config

        # 2. Liquidity & volume gates
        tvl = float(n.get("tvl") or 0.0)
        if cfg.min_tvl > 0 and tvl < cfg.min_tvl:
            return None, f"TVL ${tvl:.0f} < min ${cfg.min_tvl:.0f}"
        if cfg.max_tvl > 0 and tvl > cfg.max_tvl:
            return None, f"TVL ${tvl:.0f} > max ${cfg.max_tvl:.0f}"

        window_days = _WINDOW_DAYS.get(cfg.window, 1.0)
        gross_fee_window = float(n.get("fee") or 0.0)
        if gross_fee_window <= 0.0:
            gross_fee_window = tvl * (float(n.get("fee_tvl_ratio") or 0.0) / 100.0)
        lp_fee_share = max(0.0, min(1.0, float(n.get("lp_fee_share", 1.0))))
        daily_fee_usd = gross_fee_window * lp_fee_share / window_days
        fee_tvl_ratio = (daily_fee_usd / tvl * 100.0) if tvl > 0.0 else 0.0
        if cfg.min_fee_tvl > 0 and fee_tvl_ratio < cfg.min_fee_tvl:
            return None, f"fee/TVL {fee_tvl_ratio:.2f}% < min {cfg.min_fee_tvl:.2f}%"

        if cfg.min_daily_fee > 0 and daily_fee_usd < cfg.min_daily_fee:
            return (
                None,
                f"daily fees ${daily_fee_usd:.1f} < min ${cfg.min_daily_fee:.1f}",
            )

        volume_window = float(n.get("volume") or 0.0)
        if cfg.min_volume_usd > 0 and volume_window < cfg.min_volume_usd:
            return None, f"volume ${volume_window:.0f} < min ${cfg.min_volume_usd:.0f}"

        # Wash-trading / JIT gate
        turnover_ratio = volume_window / window_days / tvl if tvl > 0 else 0.0
        if turnover_ratio > cfg.max_turnover_ratio:
            return (
                None,
                f"turnover ratio {turnover_ratio:.1f}x > max "
                f"{cfg.max_turnover_ratio}x (wash trading)",
            )

        apr = float(n.get("apr") or 0.0)

        # 3. CLMM tick-spacing band (Standard pools report 0 and skip the gate)
        tick_spacing = int(n.get("tick_spacing") or n.get("bin_step") or 0)
        pool_type = str(n.get("pool_type") or "")
        is_clmm = pool_type.lower().startswith("concentr") or tick_spacing > 0
        if is_clmm:
            if cfg.min_bin_step > 0 and tick_spacing < cfg.min_bin_step:
                return None, f"tick spacing {tick_spacing} < min {cfg.min_bin_step}"
            if cfg.max_bin_step > 0 and tick_spacing > cfg.max_bin_step:
                return None, f"tick spacing {tick_spacing} > max {cfg.max_bin_step}"

        # Fee-tier band
        fee_pct = float(n.get("fee_pct") or 0.0)
        if cfg.min_fee_pct > 0 and fee_pct < cfg.min_fee_pct:
            return None, f"fee {fee_pct:.2f}% < min {cfg.min_fee_pct:.2f}%"
        if cfg.max_fee_pct > 0 and fee_pct > cfg.max_fee_pct:
            return None, f"fee {fee_pct:.2f}% > max {cfg.max_fee_pct:.2f}%"

        # 4. Volatility gate (price excursion % over the window)
        volatility = float(n.get("volatility") or 0.0)
        if cfg.max_volatility > 0 and volatility > cfg.max_volatility:
            return (
                None,
                f"volatility {volatility:.1f}% > max {cfg.max_volatility:.1f}% (IL risk)",
            )

        # 5. Fetch active bin ID via RPC for DLMM pools
        active_bin_id = 0
        if self.rpc and pool_type.lower().startswith("concentr"):
            active_bin_id = _fetch_active_bin_id(self.rpc, n.get("pool_address", ""))

        # 6. Protocol-neutral score, normalized to daily observations.
        breakdown = self.scorer.score(
            PoolScoreInput(
                tvl_usd=tvl,
                fee_usd=gross_fee_window,
                volume_usd=volume_window,
                window_days=window_days,
                reported_apr_pct=apr,
                volatility_pct=(
                    volatility
                    if n.get("volatility_available", "volatility" in n)
                    else None
                ),
                fee_tier_pct=fee_pct,
                lp_fee_share=lp_fee_share,
                pool_type=pool_type,
            )
        )
        if cfg.min_apr > 0 and breakdown.adjusted_apr < cfg.min_apr:
            return (
                None,
                f"adjusted APR {breakdown.adjusted_apr:.1f}% < min {cfg.min_apr:.1f}%",
            )

        candidate = Candidate(
            pool_address=n.get("pool_address", ""),
            name=n.get("name", ""),
            whitelisted_token_symbol=normalize_symbol(target_token.get("symbol", "")),
            whitelisted_token_address=target_token.get("address", ""),
            paired_token_symbol=normalize_symbol(paired_token.get("symbol", "")),
            tvl=tvl,
            fee_tvl_ratio=fee_tvl_ratio,
            daily_fee_usd=daily_fee_usd,
            volume_window=volume_window,
            bin_step=tick_spacing,
            fee_pct=fee_pct,
            volatility=volatility,
            score=breakdown.total,
            dex=str(n.get("dex") or "raydium"),
            pool_type=pool_type,
            apr=apr,
            fee_rate=float(n.get("fee_rate") or 0.0),
            tick_spacing=tick_spacing,
            active_bin_id=active_bin_id,
            effective_tvl=breakdown.effective_tvl,
            realized_fee_apr=breakdown.realized_fee_apr,
            adjusted_apr=breakdown.adjusted_apr,
            yield_score=breakdown.yield_score,
            depth_score=breakdown.depth_score,
            efficiency_score=breakdown.efficiency_score,
            risk_score=breakdown.risk_score,
            lp_fee_share=lp_fee_share,
        )
        return candidate, ""

    def screen_all(
        self, pools: List[Dict[str, Any]]
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        candidates: List[Candidate] = []
        rejects: Dict[str, int] = {}
        for pool in pools:
            try:
                candidate, reason = self.screen_pool(pool)
            except (TypeError, ValueError, OverflowError) as exc:
                reason = f"invalid provider payload: {exc}"
                candidate = None
            if candidate:
                candidates.append(candidate)
            elif reason:
                rejects[reason] = rejects.get(reason, 0) + 1
        candidates.sort(key=attrgetter("score"), reverse=True)
        return candidates, rejects

    def fetch_and_screen(
        self, max_pages: int = 5
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        pools = self.client.fetch_pools(self.config, max_pages=max_pages)
        return self.screen_all(pools)


# Compatibility aliases for existing integrations.
RaydiumScreener = MultiDexScreener
MeteoraScreener = MultiDexScreener
