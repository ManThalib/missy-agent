"""Weights and anchors used by pool scoring."""

from dataclasses import dataclass
from typing import Any, Dict, Optional


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

    @classmethod
    def from_policy(cls, policy: Optional[Dict[str, Any]] = None) -> "ScoringConfig":
        """Build a ScoringConfig from a Missy policy dict (defaults if absent)."""
        if not policy:
            return cls()
        scoring = policy.get("scoring") or {}
        weights = scoring.get("weights") or {}
        anchors = scoring.get("anchors") or {}
        projected = scoring.get("projected_apr") or {}
        return cls(
            yield_weight=float(weights.get("yield", 25.0)),
            depth_weight=float(weights.get("depth", 25.0)),
            efficiency_weight=float(weights.get("efficiency", 25.0)),
            risk_weight=float(weights.get("risk", 25.0)),
            yield_anchor_apr=float(anchors.get("yield_apr_pct", 50.0)),
            depth_anchor_usd=float(anchors.get("depth_usd", 1_000_000.0)),
            turnover_anchor_daily=float(anchors.get("turnover_daily", 2.0)),
            volatility_anchor_pct=float(anchors.get("volatility_pct", 20.0)),
            high_fee_anchor_pct=float(anchors.get("high_fee_pct", 1.0)),
            projected_apr_weight=float(projected.get("weight", 0.30)),
            projected_apr_premium_cap=float(projected.get("premium_cap", 10.0)),
            projected_apr_multiple_cap=float(projected.get("multiple_cap", 2.0)),
        )
