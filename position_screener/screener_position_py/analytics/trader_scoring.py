"""Risk-adjusted, cost-aware scoring for chronological wallet trades."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class TradePerformance:
    """One valued trade; ``network_fee`` excludes ``priority_fee``."""

    __slots__ = (
        "timestamp",
        "gross_pnl",
        "capital",
        "volume",
        "liquidity",
        "network_fee",
        "priority_fee",
        "token_fee",
        "rent_adjustment",
        "mev_suspected",
    )

    timestamp: int
    gross_pnl: float
    capital: float
    volume: float
    liquidity: float
    network_fee: float
    priority_fee: float
    token_fee: float
    rent_adjustment: float
    mev_suspected: bool

    @property
    def net_pnl(self) -> float:
        return (
            self.gross_pnl
            - self.network_fee
            - self.priority_fee
            - self.token_fee
            + self.rent_adjustment
        )


@dataclass(frozen=True)
class TraderScoreBreakdown:
    __slots__ = (
        "final_score",
        "sharpe_ratio",
        "sortino_ratio",
        "sharpe_score",
        "adjusted_pnl",
        "adjusted_pnl_score",
        "win_rate",
        "max_drawdown",
        "gross_pnl",
        "net_pnl",
        "total_costs",
        "trade_count",
        "effective_trades",
    )

    final_score: float
    sharpe_ratio: float
    sortino_ratio: float
    sharpe_score: float
    adjusted_pnl: float
    adjusted_pnl_score: float
    win_rate: float
    max_drawdown: float
    gross_pnl: float
    net_pnl: float
    total_costs: float
    trade_count: int
    effective_trades: float


class TraderPerformanceScorer:
    """Calculate the requested multi-factor score on a bounded 0-100 scale."""

    __slots__ = (
        "sharpe_weight",
        "pnl_weight",
        "win_rate_weight",
        "drawdown_weight",
        "half_life_seconds",
        "volume_anchor",
        "liquidity_anchor",
        "return_cap",
        "mev_weight",
    )

    def __init__(
        self,
        sharpe_weight: float = 0.30,
        pnl_weight: float = 0.35,
        win_rate_weight: float = 0.20,
        drawdown_weight: float = 0.15,
        half_life_days: float = 30.0,
        volume_anchor: float = 10_000.0,
        liquidity_anchor: float = 50_000.0,
        return_cap: float = 3.0,
        mev_weight: float = 0.10,
    ) -> None:
        if (
            half_life_days <= 0
            or volume_anchor <= 0
            or liquidity_anchor <= 0
            or return_cap <= 0
        ):
            raise ValueError("scoring anchors and half-life must be positive")
        if (
            min(sharpe_weight, pnl_weight, win_rate_weight, drawdown_weight, mev_weight)
            < 0
        ):
            raise ValueError("scoring weights cannot be negative")
        factor_weight = sharpe_weight + pnl_weight + win_rate_weight + drawdown_weight
        if not math.isclose(factor_weight, 1.0, abs_tol=1e-9):
            raise ValueError("factor weights must sum to 1")
        if mev_weight > 1:
            raise ValueError("mev_weight must be between 0 and 1")
        self.sharpe_weight = sharpe_weight
        self.pnl_weight = pnl_weight
        self.win_rate_weight = win_rate_weight
        self.drawdown_weight = drawdown_weight
        self.half_life_seconds = half_life_days * 86_400.0
        self.volume_anchor = volume_anchor
        self.liquidity_anchor = liquidity_anchor
        self.return_cap = return_cap
        self.mev_weight = mev_weight

    def score(
        self, trades: Iterable[TradePerformance], now: Optional[int] = None
    ) -> TraderScoreBreakdown:
        ordered = sorted(
            (trade for trade in trades if self._valid(trade)),
            key=lambda trade: trade.timestamp,
        )
        if not ordered:
            return TraderScoreBreakdown(
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0,
                0.0,
            )

        evaluated_at = int(time.time()) if now is None else now
        weighted_returns = []
        weighted_wins = 0.0
        weight_sum = 0.0
        weight_square_sum = 0.0
        adjusted_pnl = gross_pnl = net_pnl = total_costs = 0.0
        starting_capital = max(max(trade.capital for trade in ordered), 1.0)
        equity = peak = starting_capital
        max_drawdown = 0.0

        for trade in ordered:
            age = max(0, evaluated_at - trade.timestamp)
            recency = math.exp(-math.log(2.0) * age / self.half_life_seconds)
            mev_factor = (
                self.mev_weight if trade.mev_suspected and trade.net_pnl > 0 else 1.0
            )
            weight = recency * mev_factor
            confidence = math.sqrt(
                min(max(trade.volume, 0.0) / self.volume_anchor, 1.0)
                * min(max(trade.liquidity, 0.0) / self.liquidity_anchor, 1.0)
            )
            net = trade.net_pnl
            adjusted_pnl += (net * confidence if net > 0 else net) * weight
            trade_return = max(
                -self.return_cap, min(net / trade.capital, self.return_cap)
            )
            weighted_returns.append((trade_return, weight))
            weighted_wins += weight if net > 0 else 0.0
            weight_sum += weight
            weight_square_sum += weight * weight
            gross_pnl += trade.gross_pnl
            net_pnl += net
            total_costs += (
                trade.network_fee
                + trade.priority_fee
                + trade.token_fee
                - trade.rent_adjustment
            )
            equity += net
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)

        if weight_sum <= 0.0:
            return TraderScoreBreakdown(
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                round(gross_pnl, 8),
                round(net_pnl, 8),
                round(total_costs, 8),
                len(ordered),
                0.0,
            )

        mean_return = (
            sum(value * weight for value, weight in weighted_returns) / weight_sum
        )
        variance = (
            sum(
                weight * (value - mean_return) ** 2
                for value, weight in weighted_returns
            )
            / weight_sum
        )
        downside_variance = (
            sum(weight * min(value, 0.0) ** 2 for value, weight in weighted_returns)
            / weight_sum
        )
        effective_trades = (
            weight_sum * weight_sum / weight_square_sum if weight_square_sum else 0.0
        )
        period_scale = math.sqrt(max(effective_trades, 1.0))
        sharpe = (
            mean_return / math.sqrt(variance) * period_scale
            if variance > 1e-18
            else self._zero_variance_ratio(mean_return)
        )
        sortino = (
            mean_return / math.sqrt(downside_variance) * period_scale
            if downside_variance > 1e-18
            else self._zero_variance_ratio(mean_return)
        )
        sharpe = max(-10.0, min(sharpe, 10.0))
        sortino = max(-10.0, min(sortino, 10.0))
        sharpe_score = 50.0 + 50.0 * math.tanh(sharpe / 2.0)
        pnl_score = 50.0 + 50.0 * math.tanh(adjusted_pnl / starting_capital)
        win_rate = 100.0 * weighted_wins / weight_sum
        drawdown_pct = min(max_drawdown * 100.0, 100.0)
        final = (
            self.sharpe_weight * sharpe_score
            + self.pnl_weight * pnl_score
            + self.win_rate_weight * win_rate
            - self.drawdown_weight * drawdown_pct
        )
        return TraderScoreBreakdown(
            round(max(0.0, min(final, 100.0)), 4),
            round(sharpe, 6),
            round(sortino, 6),
            round(sharpe_score, 4),
            round(adjusted_pnl, 8),
            round(pnl_score, 4),
            round(win_rate, 4),
            round(drawdown_pct, 4),
            round(gross_pnl, 8),
            round(net_pnl, 8),
            round(total_costs, 8),
            len(ordered),
            round(effective_trades, 4),
        )

    @staticmethod
    def _valid(trade: TradePerformance) -> bool:
        values = (
            trade.gross_pnl,
            trade.capital,
            trade.volume,
            trade.liquidity,
            trade.network_fee,
            trade.priority_fee,
            trade.token_fee,
            trade.rent_adjustment,
        )
        return (
            isinstance(trade.timestamp, int)
            and trade.capital > 0
            and trade.volume >= 0
            and trade.liquidity >= 0
            and trade.network_fee >= 0
            and trade.priority_fee >= 0
            and trade.token_fee >= 0
            and all(math.isfinite(value) for value in values)
        )

    @staticmethod
    def _zero_variance_ratio(mean_return: float) -> float:
        return 10.0 if mean_return > 0 else (-10.0 if mean_return < 0 else 0.0)
