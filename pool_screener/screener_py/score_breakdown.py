"""Auditable output record for provider-independent scoring."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreBreakdown:
    """Score and intermediate values used to explain a ranking."""

    total: float
    yield_score: float
    depth_score: float
    efficiency_score: float
    risk_score: float
    realized_fee_apr: float
    adjusted_apr: float
    effective_tvl: float
    daily_turnover: float
    active_liquidity_factor: float
