"""Conservative analytics for wallet liquidity positions.

Raw tick/bin coordinates and token amounts are never treated as prices or a
common quote currency. Components remain neutral until callers provide the
required normalized enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..liquidity_position import LiquidityPosition


@dataclass
class PositionSnapshot:
    """Immutable snapshot of one position observed at a scan instant.

    The canonical implementation keeps only fields available from on-chain
    account data; pool-creation timestamps or external price oracles are
    optional and default to ``None`` so that scoring logic never has to guard
    against missing data.
    """

    dex: str
    position_address: str
    pool_address: str
    status: str  # "active" | "closed" | "inactive"
    lower_bound: Optional[int]
    upper_bound: Optional[int]
    liquidity_raw: int
    fees_owed_raw: List[int]
    rewards_owed_raw: List[int]
    opened_at: Optional[int] = None
    closed_at: Optional[int] = None
    # Optional externally provided pool data for price/fee-rate enrichment
    current_price: Optional[float] = None
    lower_price: Optional[float] = None
    upper_price: Optional[float] = None
    pool_fee_rate: Optional[float] = None
    # Arbitrary user-supplied metadata that persisted across scans
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class PositionScoreInput:
    """Input for ``PositionScorer.score`` with raw and normalized fields."""

    dex: str
    position_address: str
    liquidity_raw: int
    lower_bound: Optional[int]
    upper_bound: Optional[int]
    fees_owed_raw: List[int]
    rewards_owed_raw: List[int]
    status: str
    opened_at: Optional[int] = None
    closed_at: Optional[int] = None
    current_price: Optional[float] = None
    lower_price: Optional[float] = None
    upper_price: Optional[float] = None
    pool_fee_rate: Optional[float] = None
    position_value_quote: Optional[float] = None
    fee_value_delta_quote: Optional[float] = None
    # Time-window in seconds across which range-efficiency is measured
    observation_window_secs: int = 3600


@dataclass
class PositionScoreBreakdown:
    """Unweighted component scores in ``[0, 1]`` and total in ``[0, 100]``."""

    range_efficiency: float = 0.0
    fee_yield_rate: float = 0.0
    pnl_vs_hodl: float = 0.0
    boundary_risk: float = 0.0
    total: float = 0.0


@dataclass
class PositionScorer:
    """Rate positions on a 0-100 scale using four weighted criteria.

    The scorer consumes ``PositionScoreInput`` and returns a
    ``PositionScoreBreakdown`` plus the float total. Fields that cannot be
    derived from the source data are left as neutral (neither penalizing nor
    rewarding) contributions.
    """

    _history: Dict[Tuple[str, str], List[PositionSnapshot]] = field(
        default_factory=dict,
        repr=False,
    )  # (dex, position_address) -> List[PositionSnapshot]

    # ── public API ────────────────────────────────────────────────────

    def score(
        self, score_input: PositionScoreInput
    ) -> Tuple[PositionScoreBreakdown, float]:
        """Score one position and return (breakdown, total_0_100)."""
        range_efficiency, _ = self._range_efficiency(score_input)
        fee_yield_rate, _ = self._fee_yield_rate(score_input)
        pnl_vs_hodl, _ = self._pnl_vs_hold(score_input)
        boundary_risk, _ = self._boundary_risk(score_input)
        total = (
            0.35 * range_efficiency
            + 0.30 * fee_yield_rate
            + 0.20 * pnl_vs_hodl
            + 0.15 * boundary_risk
        ) * 100.0
        total = round(total, 2)

        breakdown = PositionScoreBreakdown(
            range_efficiency=range_efficiency,
            fee_yield_rate=fee_yield_rate,
            pnl_vs_hodl=pnl_vs_hodl,
            boundary_risk=boundary_risk,
            total=total,
        )
        return breakdown, total

    # ── internal helpers ────────────────────────────────────────────

    def _range_efficiency(self, score_input: PositionScoreInput) -> Tuple[float, int]:
        """Return current normalized-price range status, or neutral."""
        key = (score_input.dex, score_input.position_address)
        if self._history.get(key):
            return (
                self._range_efficiency_from_history(
                    key, score_input.observation_window_secs
                ),
                len(self._history[key]),
            )
        if (
            score_input.current_price is None
            or score_input.lower_price is None
            or score_input.upper_price is None
        ):
            return 0.5, 0
        if (
            score_input.lower_price
            <= score_input.current_price
            <= score_input.upper_price
        ):
            return 1.0, 1
        return 0.0, 1

    def _fee_yield_rate(self, score_input: PositionScoreInput) -> Tuple[float, int]:
        """Annualize a quote-valued fee delta, or return neutral."""
        position_value = score_input.position_value_quote
        fee_delta = score_input.fee_value_delta_quote
        if (
            position_value is None
            or fee_delta is None
            or position_value <= 0
            or fee_delta < 0
        ):
            return 0.5, 0
        seconds_per_year = 365 * 24 * 3600
        annual_fee = (
            fee_delta * seconds_per_year / max(score_input.observation_window_secs, 1)
        )
        raw_apr = annual_fee / position_value
        capped = max(0.0, min(raw_apr / 100.0, 1.0))
        return capped, 1

    def _pnl_vs_hold(self, score_input: PositionScoreInput) -> Tuple[float, int]:
        """Position performance vs simply holding the underlying tokens,
        accounting for impermanent loss.

        Without a current_price we cannot compute IL, so we return neutral 0.5."""
        return 0.5, 0

    def _boundary_risk(self, score_input: PositionScoreInput) -> Tuple[float, int]:
        """Penalty if current price is within 2% of either boundary.

        Returns a score in [0, 1] where 1 = safe (far from boundaries),
        0 = imminent OOR risk."""
        if (
            score_input.current_price is None
            or score_input.lower_price is None
            or score_input.upper_price is None
        ):
            return 0.5, 0
        price = score_input.current_price
        lower = score_input.lower_price
        upper = score_input.upper_price
        range_width = upper - lower
        if range_width <= 0:
            return 0.5, 0
        dist_to_lower = (price - lower) / range_width
        dist_to_upper = (upper - price) / range_width
        if dist_to_lower < 0.02 or dist_to_upper < 0.02:
            return 0.0, 1
        min_dist = min(dist_to_lower, dist_to_upper)
        score = max(0.0, 1.0 - (0.02 / min_dist))
        return score, 1

    # ── history management for range-efficiency accumulation ────────

    def record_snapshot(self, snap: PositionSnapshot) -> None:
        """Persist a snapshot so that repeated scans can accumulate
        time-in-range statistics for range-efficiency."""
        key = (snap.dex, snap.position_address)
        self._history.setdefault(key, []).append(snap)
        history = self._history[key]
        if len(history) > 64:
            self._history[key] = history[-64:]

    def _range_efficiency_from_history(
        self, key: Tuple[str, str], window_secs: int
    ) -> float:
        """Compute range efficiency from accumulated snapshots.

        Returns the fraction of stored snapshots where the price was
        inside [lower_bound, upper_bound].  If no snapshots exist we return
        neutral 0.5."""
        history = self._history.get(key, [])
        if not history:
            return 0.5
        valid_snapshots = [
            snap
            for snap in history
            if snap.lower_price is not None
            and snap.upper_price is not None
            and snap.current_price is not None
        ]
        if not valid_snapshots:
            return 0.5
        in_range = sum(
            1
            for snap in valid_snapshots
            if snap.lower_price <= snap.current_price <= snap.upper_price
        )
        return in_range / len(valid_snapshots)


def score_tag(total: float) -> str:
    """Map 0-100 total to Optimal / Underperforming / Action Needed."""
    if total >= 75.0:
        return "Optimal"
    if total >= 40.0:
        return "Underperforming"
    return "Action Needed"


def range_status(
    lower_price: Optional[float],
    upper_price: Optional[float],
    current_price: Optional[float],
) -> str:
    """In Range / Out of Range (OOR) / Unknown without price data."""
    if lower_price is None or upper_price is None or current_price is None:
        return "Unknown"
    if lower_price <= current_price <= upper_price:
        return "In Range"
    return "Out of Range (OOR)"


def distance_to_boundary_pct(
    lower_price: Optional[float],
    upper_price: Optional[float],
    current_price: Optional[float],
) -> Optional[float]:
    """Distance to nearest tick boundary as % of range width."""
    if lower_price is None or upper_price is None or current_price is None:
        return None
    width = upper_price - lower_price
    if width <= 0:
        return None
    return (
        min(
            abs(current_price - lower_price),
            abs(upper_price - current_price),
        )
        / width
        * 100.0
    )


def format_duration(
    opened_at: Optional[int], closed_at: Optional[int], now: Optional[int] = None
) -> str:
    """Human duration between open and close/now; '-' when open unknown."""
    import time as _time

    if opened_at is None:
        return "-"
    end = (
        closed_at
        if closed_at is not None
        else (now if now is not None else int(_time.time()))
    )
    secs = max(0, int(end) - int(opened_at))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {rem // 60}m"
    return f"{rem // 60}m {rem % 60}s"


# ── convenience factory ──────────────────────────────────────────────


def from_liquidity_position(
    pos: LiquidityPosition,
    pool_extra: Optional[Dict[str, object]] = None,
) -> PositionScoreInput:
    """Build a ``PositionScoreInput`` from an existing ``LiquidityPosition``.

    ``pool_extra`` may contain normalized price bounds, current price, quote
    values, and a decimal pool fee rate."""
    enrichment = pool_extra or {}
    return PositionScoreInput(
        dex=pos.dex,
        position_address=pos.position_address,
        liquidity_raw=pos.liquidity_raw,
        lower_bound=pos.lower_bound,
        upper_bound=pos.upper_bound,
        fees_owed_raw=list(pos.fees_owed_raw),
        rewards_owed_raw=list(pos.rewards_owed_raw),
        status=pos.status,
        opened_at=pos.opened_at,
        closed_at=pos.closed_at,
        current_price=enrichment.get("current_price"),
        lower_price=enrichment.get("lower_price"),
        upper_price=enrichment.get("upper_price"),
        pool_fee_rate=enrichment.get("pool_fee_rate"),
        position_value_quote=enrichment.get("position_value_quote"),
        fee_value_delta_quote=enrichment.get("fee_value_delta_quote"),
        observation_window_secs=int(enrichment.get("observation_window_secs", 3600)),
    )
