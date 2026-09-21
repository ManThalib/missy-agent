"""Normalized input record for provider-independent scoring."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PoolScoreInput:
    """Economic observations required to compare a supported pool."""

    tvl_usd: float
    fee_usd: float
    volume_usd: float
    window_days: float
    reported_apr_pct: float = 0.0
    volatility_pct: Optional[float] = None
    fee_tier_pct: float = 0.0
    lp_fee_share: float = 1.0
    pool_type: str = ""
