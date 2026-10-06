"""Provider-independent record for a qualified liquidity pool."""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Candidate:
    """A qualified pool that crossed all whitelist and quality gates."""

    pool_address: str
    name: str
    whitelisted_token_symbol: str
    whitelisted_token_address: str
    paired_token_symbol: str
    tvl: float
    fee_tvl_ratio: float
    daily_fee_usd: float
    volume_window: float
    bin_step: int  # Meteora DLMM bin step (bps); 0 for Orca/Raydium/Standard
    fee_pct: float
    volatility: float
    score: float
    dex: str = ""
    # Raydium-native extras (defaulted so old callers still work)
    pool_type: str = ""  # "Concentrated" | "Standard"
    apr: float = 0.0  # window APR % (fee APR + reward APR)
    fee_rate: float = 0.0  # raw feeRate fraction, e.g. 0.0025
    tick_spacing: int = 0
    effective_tvl: float = 0.0
    realized_fee_apr: float = 0.0
    adjusted_apr: float = 0.0
    yield_score: float = 0.0
    depth_score: float = 0.0
    efficiency_score: float = 0.0
    risk_score: float = 0.0
    lp_fee_share: float = 1.0
    screened_at: float = field(default_factory=time.time)
    wallet_positions: List[Dict[str, Any]] = field(default_factory=list)

    # Meteora DLMM discovery API fields (preserved in output)
    pool_price: float = 0.0  # Price of token_x denominated in token_y
    token_x_address: str = ""  # Mint address of token_x
    token_x_decimals: int = 0  # Base-10 decimals of token_x
    token_x_price_usd: float = 0.0  # Token X price in USD
    token_y_address: str = ""  # Mint address of token_y
    token_y_decimals: int = 0  # Base-10 decimals of token_y
    token_y_price_usd: float = 0.0  # Token Y price in USD
    active_bin_id: Optional[int] = None  # Meteora DLMM active bin ID only; None = unknown/not DLMM
    current_tick_index: Optional[int] = None  # Live 1bp tick (orca whirlpool / raydium CLMM); None for Meteora/unknown

    # Cross-DEX comparable view of the current price (added so consumers do
    # not have to reinterpret DEX-native tick/bin fields).
    tick_unit: str = "none"  # "tick" (Orca/Raydium) | "bin" (Meteora) | "none"
    current_price_ratio: float = 0.0  # atomic token_y/token_x ratio; 0.0 = unknown
    current_tick_equivalent: Optional[int] = None  # current_price_ratio on the shared 1bp tick scale

    # Normalised orientation / provenance (P1/P2).
    pool_price_raw: float = 0.0  # provider-raw price before orientation
    provider_name: str = ""  # raw provider pool name before canonicalisation
    volatility_source: str = ""  # e.g. "raydium_range_24h"
    volatility_window: str = ""  # e.g. "day"
    apr_source: str = ""  # e.g. "fee/tvl annualized"
    apr_window: str = ""  # e.g. "day"
    tick_source: str = ""  # e.g. "rpc" | "discovery_api" | "none"
    price_source: str = ""  # e.g. "jupiter_v2"
    pending_fees_source: str = ""  # populated by position scanner

    # Phase 1: eligibility and score provenance.
    # eligible: True if the pool passes Missy's pool_eligibility gates.
    # rejected_reason: human-readable reason when eligible is False.
    # pair_class: cohort tag (stable_stable, stable_bluechip, bluechip_bluechip,
    #     off_universe) emitted so downstream consumers can cohort and backtest.
    # score_model / score_version: identify which scoring model produced the
    #     score and breakdown fields. Sheldon can pin or override these.
    eligible: bool = True
    rejected_reason: str = ""
    pair_class: str = ""
    score_model: str = ""
    score_version: int = 0
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    new_score_model: str = "missy-default"
    new_fc_score: float = 0.0
    new_score_breakdown: Dict[str, float] = field(default_factory=dict)
    daily_turnover: float = 0.0
    active_liquidity_factor: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the candidate to a JSON-compatible dictionary."""
        return {
            "pool_address": self.pool_address,
            "name": self.name,
            "whitelisted_token_symbol": self.whitelisted_token_symbol,
            "whitelisted_token_address": self.whitelisted_token_address,
            "paired_token_symbol": self.paired_token_symbol,
            "tvl": self.tvl,
            "fee_tvl_ratio": self.fee_tvl_ratio,
            "daily_fee_usd": self.daily_fee_usd,
            "volume_window": self.volume_window,
            "bin_step": self.bin_step,
            "fee_pct": self.fee_pct,
            "volatility": self.volatility,
            "score": self.score,
            "dex": self.dex,
            "pool_type": self.pool_type,
            "apr": self.apr,
            "fee_rate": self.fee_rate,
            "tick_spacing": self.tick_spacing,
            "effective_tvl": self.effective_tvl,
            "realized_fee_apr": self.realized_fee_apr,
            "adjusted_apr": self.adjusted_apr,
            "yield_score": self.yield_score,
            "depth_score": self.depth_score,
            "efficiency_score": self.efficiency_score,
            "risk_score": self.risk_score,
            "lp_fee_share": self.lp_fee_share,
            "screened_at": self.screened_at,
            "wallet_positions": [dict(position) for position in self.wallet_positions],
            # Meteora DLMM discovery API fields
            "pool_price": self.pool_price,
            "pool_price_raw": self.pool_price_raw,
            "token_x_address": self.token_x_address,
            "token_x_decimals": self.token_x_decimals,
            "token_x_price_usd": self.token_x_price_usd,
            "token_y_address": self.token_y_address,
            "token_y_decimals": self.token_y_decimals,
            "token_y_price_usd": self.token_y_price_usd,
            "active_bin_id": self.active_bin_id,
            "current_tick_index": self.current_tick_index,
            "tick_unit": self.tick_unit,
            "current_price_ratio": self.current_price_ratio,
            "current_tick_equivalent": self.current_tick_equivalent,
            # Provenance fields
            "provider_name": self.provider_name,
            "volatility_source": self.volatility_source,
            "volatility_window": self.volatility_window,
            "apr_source": self.apr_source,
            "apr_window": self.apr_window,
            "tick_source": self.tick_source,
            "price_source": self.price_source,
            "pending_fees_source": self.pending_fees_source,
            # Eligibility + score provenance (Phase 1)
            "eligible": self.eligible,
            "rejected_reason": self.rejected_reason,
            "pair_class": self.pair_class,
            "score_model": self.score_model,
            "score_version": self.score_version,
            "score_breakdown": dict(self.score_breakdown),
            "new_score_model": self.new_score_model,
            "new_fc_score": self.new_fc_score,
            "new_score_breakdown": dict(self.new_score_breakdown),
            "daily_turnover": self.daily_turnover,
            "active_liquidity_factor": self.active_liquidity_factor,
        }
