"""Provider-independent record for a qualified liquidity pool."""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List


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
    bin_step: int  # CLMM tickSpacing; 0 for Standard/AMM pools (kept for CLI compat)
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
    active_bin_id: int = 0  # Active bin ID from DLMM LbPair account

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
            "token_x_address": self.token_x_address,
            "token_x_decimals": self.token_x_decimals,
            "token_x_price_usd": self.token_x_price_usd,
            "token_y_address": self.token_y_address,
            "token_y_decimals": self.token_y_decimals,
            "token_y_price_usd": self.token_y_price_usd,
            "active_bin_id": self.active_bin_id,
        }
