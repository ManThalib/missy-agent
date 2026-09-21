"""Provider-neutral pool scoring implementation."""

from math import isfinite, log1p
from typing import Optional

from .pool_score_input import PoolScoreInput
from .score_breakdown import ScoreBreakdown
from .scoring_config import ScoringConfig


class PoolScorer:
    """Score pools by economic observations, not provider fields."""

    _CONCENTRATED_TYPES = ("concentr", "clmm", "whirlpool", "dlmm")

    def __init__(self, config: Optional[ScoringConfig] = None) -> None:
        self.config = config or ScoringConfig()

    @staticmethod
    def _non_negative(value: float) -> float:
        return value if isfinite(value) and value > 0.0 else 0.0

    @staticmethod
    def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
        return max(lower, min(upper, value))

    @classmethod
    def _active_liquidity_factor(cls, pool_type: str) -> float:
        normalized = pool_type.strip().lower()
        if "splash" in normalized:
            return 0.90
        if any(marker in normalized for marker in cls._CONCENTRATED_TYPES):
            return 0.75
        if any(
            marker in normalized for marker in ("standard", "constant", "cpmm", "amm")
        ):
            return 1.00
        return 0.85

    @staticmethod
    def _log_score(value: float, anchor: float, weight: float) -> float:
        if value <= 0.0 or anchor <= 0.0 or weight <= 0.0:
            return 0.0
        return weight * min(1.0, log1p(value) / log1p(anchor))

    def score(self, observation: PoolScoreInput) -> ScoreBreakdown:
        cfg = self.config
        tvl = self._non_negative(observation.tvl_usd)
        gross_fees = self._non_negative(observation.fee_usd)
        lp_fee_share = self._bounded(observation.lp_fee_share)
        fees = gross_fees * lp_fee_share
        volume = self._non_negative(observation.volume_usd)
        days = self._non_negative(observation.window_days) or 1.0
        reported_apr = self._non_negative(observation.reported_apr_pct)
        fee_tier = self._non_negative(observation.fee_tier_pct)

        daily_fees = fees / days
        daily_volume = volume / days
        realized_apr = daily_fees / tvl * 365.0 * 100.0 if tvl > 0.0 else 0.0

        adjusted_apr = realized_apr
        yield_divergence = 0.0
        if reported_apr > 0.0:
            projected_cap = max(
                realized_apr + cfg.projected_apr_premium_cap,
                realized_apr * cfg.projected_apr_multiple_cap,
            )
            trusted_projected_apr = min(reported_apr, projected_cap)
            adjusted_apr = (
                realized_apr * (1.0 - cfg.projected_apr_weight)
                + trusted_projected_apr * cfg.projected_apr_weight
            )
            yield_divergence = max(0.0, reported_apr - realized_apr) / reported_apr

        active_factor = self._active_liquidity_factor(observation.pool_type)
        effective_tvl = tvl * active_factor
        daily_turnover = daily_volume / effective_tvl if effective_tvl > 0.0 else 0.0
        yield_score = self._log_score(
            adjusted_apr, cfg.yield_anchor_apr, cfg.yield_weight
        )
        depth_score = self._log_score(
            effective_tvl, cfg.depth_anchor_usd, cfg.depth_weight
        )
        efficiency_score = self._log_score(
            daily_turnover,
            cfg.turnover_anchor_daily,
            cfg.efficiency_weight,
        )

        if observation.volatility_pct is None:
            volatility_risk = 0.35
        else:
            volatility = self._non_negative(observation.volatility_pct)
            volatility_risk = self._bounded(volatility / cfg.volatility_anchor_pct)
        concentration_risk = 1.0 - active_factor
        fee_risk = self._bounded(fee_tier / cfg.high_fee_anchor_pct)
        risk_penalty = self._bounded(
            0.50 * volatility_risk
            + 0.20 * concentration_risk
            + 0.20 * yield_divergence
            + 0.10 * fee_risk
        )
        risk_score = cfg.risk_weight * (1.0 - risk_penalty)
        total = self._bounded(
            yield_score + depth_score + efficiency_score + risk_score,
            upper=100.0,
        )
        return ScoreBreakdown(
            total=total,
            yield_score=yield_score,
            depth_score=depth_score,
            efficiency_score=efficiency_score,
            risk_score=risk_score,
            realized_fee_apr=realized_apr,
            adjusted_apr=adjusted_apr,
            effective_tvl=effective_tvl,
            daily_turnover=daily_turnover,
            active_liquidity_factor=active_factor,
        )
