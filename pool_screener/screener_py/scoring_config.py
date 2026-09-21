"""Weights and anchors used by pool scoring."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoringConfig:
    """Anchors and weights for the 0-100 pool quality score."""

    yield_weight: float = 25.0
    depth_weight: float = 25.0
    efficiency_weight: float = 25.0
    risk_weight: float = 25.0
    yield_anchor_apr: float = 50.0
    depth_anchor_usd: float = 1_000_000.0
    turnover_anchor_daily: float = 2.0
    volatility_anchor_pct: float = 20.0
    projected_apr_weight: float = 0.30
    projected_apr_premium_cap: float = 10.0
    projected_apr_multiple_cap: float = 2.0
    high_fee_anchor_pct: float = 1.0
