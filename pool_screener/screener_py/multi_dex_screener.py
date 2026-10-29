"""Shared multi-DEX screening policy: gating, tick enrichment, and scoring.

Provider payload conversion lives in ``normalizers.py`` and RPC tick lookups
in ``tick_fetch.py``. This module assembles candidates from those pieces.
"""

import math
from operator import attrgetter
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.cache import cache_ttl_from_env
from core.prices import JupiterPriceClient

from .candidate import Candidate
from .client import MeteoraClient, MultiDexClient, OrcaClient, RaydiumClient
from .config import DEFAULT_CONFIG, FilterConfig
from .normalizers import _WINDOW_DAYS, normalize_pool
from .pair_class import classify_pair_from_policy
from .policy_loader import eligibility_gates, load_policy
from .scoring import PoolScoreInput, PoolScorer
from .scoring_config import ScoringConfig
from .tick_fetch import (
    UNKNOWN_TICK,
    _batch_fetch_ticks,
    _fetch_tick,
    _fetch_tick_with_retry,
)
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

_TICK_UNKNOWN_VALUES = (None, 0, "0", "")


class MultiDexScreener:
    """Evaluate normalized DEX pools against token and economic policies."""

    def __init__(
        self,
        whitelist: Whitelist,
        config: Optional[FilterConfig] = None,
        client: Optional[Any] = None,
        rpc: Optional[Any] = None,
        cache_ttl_seconds: Optional[float] = None,
    ):
        self.whitelist = whitelist
        self.config = config or FilterConfig()
        self.cache_ttl_seconds = (
            cache_ttl_seconds if cache_ttl_seconds is not None else cache_ttl_from_env()
        )
        if client is None:
            target_mints = list(whitelist.targets.addresses)
            client = MultiDexClient(
                timeout=self.config.timeout,
                target_mints=target_mints,
                cache_ttl_seconds=self.cache_ttl_seconds,
            )
        self.client = client
        self.rpc = rpc
        self.policy = load_policy()
        self.scorer = PoolScorer(ScoringConfig.from_policy(self.policy))
        self._jupiter_client: Optional[JupiterPriceClient] = None
        # Non-fatal problems for the current cycle, printed as warnings.
        self.warnings: List[str] = []
        # True when this cycle's batched RPC lookups all failed; per-pool
        # fallbacks are then skipped instead of multiplying the failure.
        self._rpc_unavailable = False

    def _fetch_pool_prices(self, normalized: List[Dict[str, Any]]) -> Dict[str, float]:
        """Fetch Jupiter USD prices for the token mints of already-normalized pools."""
        mints: set = set()
        for n in normalized:
            for key in ("token_x", "token_y"):
                address = (n.get(key) or {}).get("address")
                if address:
                    mints.add(address)
        if not mints:
            return {}
        try:
            if self._jupiter_client is None:
                self._jupiter_client = JupiterPriceClient()
                cache = getattr(self.client, "cache", None)
                if cache is not None:
                    self._jupiter_client.cache = cache
                self._jupiter_client.cache_ttl_seconds = self.cache_ttl_seconds
            return self._jupiter_client.fetch_prices(mints)
        except Exception as exc:
            self.warnings.append(f"token price lookup failed; USD values unavailable: {exc}")
            return {}

    def _resolve_live_tick(
        self,
        dex: str,
        api_value: Any,
        pool_address: str,
        tick_map: Optional[Mapping[str, Optional[int]]],
    ) -> Tuple[Optional[int], str]:
        """Return ``(tick_or_bin, source)``; discovery API, batch RPC, then single RPC.

        0 from the discovery API is a placeholder (treated as missing); 0 from
        RPC is a real on-chain value.
        """
        if api_value not in _TICK_UNKNOWN_VALUES:
            return int(api_value), "discovery_api"
        prefetched = (tick_map or {}).get(pool_address)
        if prefetched is not UNKNOWN_TICK:
            return prefetched, "rpc_batched"
        if self.rpc is None or self._rpc_unavailable:
            return UNKNOWN_TICK, "none"
        raw = _fetch_tick_with_retry(lambda: _fetch_tick(self.rpc, dex, pool_address))
        if raw is None:
            return UNKNOWN_TICK, "none"
        return int(raw), "rpc_single"

    def screen_pool(
        self,
        p: Dict[str, Any],
        price_map: Optional[Mapping[str, float]] = None,
        tick_map: Optional[Mapping[str, Optional[int]]] = None,
    ) -> Tuple[Optional[Candidate], str]:
        """Normalize, enrich, and score a raw pool.

        The whitelist gate is still enforced here; pools that do not match
        the configured universe are rejected. Pools that pass are emitted as
        facts, including a default score and eligibility flag.
        """
        return self._screen_pool_internal(p, price_map=price_map, tick_map=tick_map)

    def _screen_pool_internal(
        self,
        p: Dict[str, Any],
        price_map: Optional[Mapping[str, float]] = None,
        tick_map: Optional[Mapping[str, Optional[int]]] = None,
        n: Optional[Dict[str, Any]] = None,
        target: Optional[Dict[str, Any]] = None,
        paired: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Candidate], str]:
        if n is None:
            n = normalize_pool(p, window=self.config.window, price_map=price_map)

        token_x = n.get("token_x") or {}
        token_y = n.get("token_y") or {}

        if target is None or paired is None:
            ok, target, paired, reason = self.whitelist.validate_pair(token_x, token_y)
            if not ok:
                return None, reason

        whitelisted_symbol = normalize_symbol(target.get("symbol", ""))
        paired_symbol = normalize_symbol(paired.get("symbol", ""))
        canonical_name = f"{whitelisted_symbol}-{paired_symbol}"
        provider_name = n.get("provider_name", n.get("name", ""))

        pool_price_raw = float(n.get("pool_price") or 0.0)
        token_x_address = n.get("token_x_address", "")
        target_address = target.get("address", "")
        if target_address and token_x_address and target_address == token_x_address:
            # target is token_x; provider price is token_y/token_x = paired/target.
            oriented_price = 1.0 / pool_price_raw if pool_price_raw > 0.0 else 0.0
        else:
            # target is token_y; provider price is already target/paired.
            oriented_price = pool_price_raw

        tvl = float(n.get("tvl") or 0.0)
        volume_window = float(n.get("volume") or 0.0)
        window_days = _WINDOW_DAYS.get(self.config.window, 1.0)
        gross_fee_window = float(n.get("fee") or 0.0)
        if gross_fee_window <= 0.0:
            gross_fee_window = tvl * (float(n.get("fee_tvl_ratio") or 0.0) / 100.0)
        lp_fee_share = max(0.0, min(1.0, float(n.get("lp_fee_share", 1.0))))
        daily_fee_usd = gross_fee_window * lp_fee_share / window_days
        fee_tvl_ratio = (daily_fee_usd / tvl * 100.0) if tvl > 0.0 else 0.0
        tick_spacing = int(n.get("tick_spacing") or n.get("bin_step") or 0)
        pool_type = str(n.get("pool_type") or "")
        fee_pct = float(n.get("fee_pct") or 0.0)
        volatility = float(n.get("volatility") or 0.0)
        apr = float(n.get("apr") or 0.0)

        dex_name = str(n.get("dex") or "")
        pool_address = n.get("pool_address", "")
        active_bin_id: Optional[int] = UNKNOWN_TICK
        current_tick_index: Optional[int] = UNKNOWN_TICK
        tick_source = "none"

        if pool_type.lower() == "standard":
            # Standard AMM pools have no meaningful tick/bin.
            active_bin_id = 0
            current_tick_index = 0
        elif dex_name == "meteora" and pool_type == "DLMM":
            active_bin_id, tick_source = self._resolve_live_tick(
                "meteora", n.get("active_bin_id"), pool_address, tick_map
            )
        elif dex_name in ("orca", "raydium"):
            current_tick_index, tick_source = self._resolve_live_tick(
                dex_name, n.get("current_tick_index"), pool_address, tick_map
            )

        token_x_price_usd = float(n.get("token_x_price_usd") or 0.0)
        token_y_price_usd = float(n.get("token_y_price_usd") or 0.0)

        # Derive a missing token price from the provider-raw pool price.
        if token_x_price_usd == 0.0 and token_y_price_usd > 0.0 and pool_price_raw > 0.0:
            token_x_price_usd = token_y_price_usd * pool_price_raw
        elif token_y_price_usd == 0.0 and token_x_price_usd > 0.0 and pool_price_raw > 0.0:
            token_y_price_usd = token_x_price_usd / pool_price_raw

        # `bin_step` is strictly a Meteora DLMM concept (basis points).
        bin_step = tick_spacing if dex_name == "meteora" else 0

        if pool_type.lower() == "standard":
            tick_unit = "none"
        elif dex_name == "meteora":
            tick_unit = "bin"
        elif dex_name in ("orca", "raydium"):
            tick_unit = "tick"
        else:
            tick_unit = "none"

        current_price_ratio = 0.0
        current_tick_equivalent: Optional[int] = UNKNOWN_TICK
        try:
            if tick_unit == "tick" and current_tick_index is not None:
                current_price_ratio = 1.0001 ** current_tick_index
                current_tick_equivalent = current_tick_index
            elif tick_unit == "bin" and active_bin_id is not None and tick_spacing > 0:
                current_price_ratio = (1.0 + tick_spacing / 10000.0) ** active_bin_id
                current_tick_equivalent = int(
                    round(math.log(current_price_ratio) / math.log(1.0001))
                )
        except (OverflowError, ValueError):
            current_price_ratio = 0.0
            current_tick_equivalent = UNKNOWN_TICK

        price_source = "jupiter_v3" if (token_x_price_usd > 0.0 or token_y_price_usd > 0.0) else "unavailable"

        pair_class = classify_pair_from_policy(whitelisted_symbol, paired_symbol, self.policy)

        # Default score. reported_apr_pct stays 0: `apr` is already fee/TVL annualized.
        volatility_available = bool(n.get("volatility_available"))
        score_input = PoolScoreInput(
            tvl_usd=tvl,
            fee_usd=gross_fee_window,
            volume_usd=volume_window,
            window_days=window_days,
            reported_apr_pct=0.0,
            volatility_pct=volatility if volatility_available else None,
            fee_tier_pct=fee_pct,
            lp_fee_share=lp_fee_share,
            pool_type=pool_type,
            pair_class=pair_class,
            window_label=self.config.window,
        )
        breakdown = self.scorer.score(score_input)
        new_breakdown = self.scorer.score_fee_capture(score_input)

        # Pre-scoring eligibility gates. Failing pools are still emitted, marked ineligible.
        elig = eligibility_gates(self.policy)
        turnover = volume_window / tvl if tvl > 0.0 else 0.0
        rejection_reasons: List[str] = []
        if tvl < elig["min_tvl_usd"]:
            rejection_reasons.append(f"tvl ${tvl:,.0f} < policy ${elig['min_tvl_usd']:,.0f}")
        if volume_window < elig["min_volume_usd"]:
            rejection_reasons.append(
                f"volume ${volume_window:,.0f} < policy ${elig['min_volume_usd']:,.0f}"
            )
        if fee_tvl_ratio < elig["min_fee_tvl_ratio_pct"]:
            rejection_reasons.append(
                f"fee_tvl_ratio {fee_tvl_ratio:.4f}% < policy {elig['min_fee_tvl_ratio_pct']}%"
            )
        if volatility_available and volatility > elig["max_volatility_pct"]:
            rejection_reasons.append(
                f"volatility {volatility:.2f}% > policy {elig['max_volatility_pct']}%"
            )
        if turnover > elig["max_turnover_ratio"]:
            rejection_reasons.append(
                f"turnover {turnover:.2f} > policy {elig['max_turnover_ratio']}"
            )
        eligible = not rejection_reasons
        rejected_reason = "; ".join(rejection_reasons)

        scoring_meta = self.policy.get("scoring") or {}
        score_model = str(scoring_meta.get("model", "missy-default"))
        score_version = int(scoring_meta.get("version", 1))

        candidate = Candidate(
            pool_address=n.get("pool_address", ""),
            name=canonical_name,
            provider_name=provider_name,
            whitelisted_token_symbol=whitelisted_symbol,
            whitelisted_token_address=target.get("address", ""),
            paired_token_symbol=paired_symbol,
            tvl=tvl,
            fee_tvl_ratio=fee_tvl_ratio,
            daily_fee_usd=daily_fee_usd,
            volume_window=volume_window,
            bin_step=bin_step,
            fee_pct=fee_pct,
            volatility=volatility,
            score=breakdown.total,
            dex=str(n.get("dex") or "raydium"),
            pool_type=pool_type,
            apr=apr,
            apr_source=n.get("apr_source", ""),
            apr_window=n.get("apr_window", ""),
            fee_rate=float(n.get("fee_rate") or 0.0),
            tick_spacing=tick_spacing,
            active_bin_id=active_bin_id,
            current_tick_index=current_tick_index,
            tick_unit=tick_unit,
            tick_source=tick_source,
            current_price_ratio=current_price_ratio,
            current_tick_equivalent=current_tick_equivalent,
            effective_tvl=breakdown.effective_tvl,
            realized_fee_apr=breakdown.realized_fee_apr,
            adjusted_apr=breakdown.adjusted_apr,
            yield_score=breakdown.yield_score,
            depth_score=breakdown.depth_score,
            efficiency_score=breakdown.efficiency_score,
            risk_score=breakdown.risk_score,
            lp_fee_share=lp_fee_share,
            pool_price=oriented_price,
            pool_price_raw=pool_price_raw,
            token_x_address=n.get("token_x_address", ""),
            token_x_decimals=int(n.get("token_x_decimals") or 0),
            token_x_price_usd=token_x_price_usd,
            token_y_address=n.get("token_y_address", ""),
            token_y_decimals=int(n.get("token_y_decimals") or 0),
            token_y_price_usd=token_y_price_usd,
            volatility_source=n.get("volatility_source", ""),
            volatility_window=n.get("volatility_window", ""),
            price_source=price_source,
            pending_fees_source="",
            eligible=eligible,
            rejected_reason=rejected_reason,
            pair_class=pair_class,
            score_model=score_model,
            score_version=score_version,
            score_breakdown={
                "yield_score": round(breakdown.yield_score, 4),
                "depth_score": round(breakdown.depth_score, 4),
                "efficiency_score": round(breakdown.efficiency_score, 4),
                "risk_score": round(breakdown.risk_score, 4),
            },
            daily_turnover=breakdown.daily_turnover,
            active_liquidity_factor=breakdown.active_liquidity_factor,
        )
        candidate.new_fc_score = new_breakdown["fc_score"]
        candidate.new_score_breakdown = {
            "fee_yield_score": new_breakdown["fee_yield_score"],
            "absolute_fee_score": new_breakdown["absolute_fee_score"],
            "liquidity_effectiveness": new_breakdown["liquidity_effectiveness"],
            "turnover_penalty_score": new_breakdown["turnover_penalty_score"],
            "lp_share_adjustment": new_breakdown["lp_share_adjustment"],
        }
        candidate.new_score_model = "new_scoring_model_for_pool"
        return candidate, ""

    def screen_all(
        self, pools: List[Dict[str, Any]]
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        self.warnings = []
        candidates: List[Candidate] = []
        rejects: Dict[str, int] = {}

        # Phase 1: normalize once and enforce the whitelist before any
        # price or RPC spend.
        kept: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
        for pool in pools:
            try:
                n = normalize_pool(pool, window=self.config.window)
            except (TypeError, ValueError, OverflowError) as exc:
                key = f"invalid provider payload: {exc}"
                rejects[key] = rejects.get(key, 0) + 1
                continue
            token_x = n.get("token_x") or {}
            token_y = n.get("token_y") or {}
            ok, target, paired, reason = self.whitelist.validate_pair(token_x, token_y)
            if not ok:
                rejects[reason] = rejects.get(reason, 0) + 1
                continue
            kept.append((pool, n, target, paired))

        price_map = self._fetch_pool_prices([n for _, n, _, _ in kept])
        tick_map = self._prefetch_current_ticks(kept)

        for pool, _, target, paired in kept:
            try:
                # Re-normalize with prices; tick fields do not depend on prices.
                n = normalize_pool(pool, window=self.config.window, price_map=price_map)
                candidate, reason = self._screen_pool_internal(
                    pool, price_map=price_map, tick_map=tick_map,
                    n=n, target=target, paired=paired,
                )
            except (TypeError, ValueError, OverflowError) as exc:
                reason = f"invalid provider payload: {exc}"
                candidate = None
            if candidate:
                candidates.append(candidate)
            elif reason:
                rejects[reason] = rejects.get(reason, 0) + 1
        candidates.sort(key=attrgetter("score"), reverse=True)
        return candidates, rejects

    def _prefetch_current_ticks(
        self, kept: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]]
    ) -> Dict[str, Optional[int]]:
        """Map pool_address -> tick/bin, fetched via batched RPC where the API lacks it."""
        self._rpc_unavailable = False
        if not self.rpc:
            return {}

        requests: List[Tuple[str, str]] = []
        for _, n, _, _ in kept:
            dex = str(n.get("dex") or "").lower()
            pool_type = str(n.get("pool_type") or "").lower()
            pool_address = n.get("pool_address", "")
            if not dex or not pool_address or pool_type == "standard":
                continue
            if dex == "meteora":
                if n.get("active_bin_id") not in _TICK_UNKNOWN_VALUES:
                    continue
            elif n.get("current_tick_index") not in _TICK_UNKNOWN_VALUES:
                continue
            requests.append((dex, pool_address))

        tick_map = _batch_fetch_ticks(self.rpc, requests)
        self._rpc_unavailable = bool(tick_map) and all(
            value is UNKNOWN_TICK for value in tick_map.values()
        )
        if self._rpc_unavailable:
            self.warnings.append(
                f"RPC tick lookup failed for all {len(tick_map)} pool(s); "
                "active tick/bin left unknown"
            )
        return tick_map

    def fetch_and_screen(
        self, max_pages: int = 5
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        pools = self.client.fetch_pools(self.config, max_pages=max_pages)
        return self.screen_all(pools)


# Compatibility aliases for existing integrations.
RaydiumScreener = MultiDexScreener
MeteoraScreener = MultiDexScreener
