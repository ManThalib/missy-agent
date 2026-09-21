import math
from typing import Any, Dict, Optional, Set

def truncate(s: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    if max_len <= 3:
        return "." * max_len
    return s[: max_len - 3] + "..."

def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None

def _fee_apr_pct(enrichment: Dict[str, Any]) -> Optional[float]:
    position_value = _finite_number(enrichment.get("position_value_quote"))
    fee_delta = _finite_number(enrichment.get("fee_value_delta_quote"))
    window_seconds = _finite_number(enrichment.get("observation_window_secs", 3600))
    if (
        position_value is None
        or fee_delta is None
        or window_seconds is None
        or position_value <= 0
        or fee_delta < 0
        or window_seconds <= 0
    ):
        return None
    return fee_delta / position_value * (365 * 24 * 3600 / window_seconds) * 100

def _score_action(
    status: str,
    range_label: str,
    boundary_distance: Optional[float],
    score: float,
    input_count: int,
) -> str:
    if status == "closed":
        return "Closed"
    if status == "inactive":
        return "Inactive"
    if input_count == 0:
        return "Needs enrichment"
    if range_label.startswith("Out"):
        return "Rebalance range"
    if boundary_distance is not None and boundary_distance < 2.0:
        return "Near boundary"
    if score >= 75.0:
        return "Optimal"
    if score >= 40.0:
        return "Monitor"
    return "Review"

def print_positions(
    scan: Any,
    show_inactive: bool = False,
    newly_closed: Optional[Set[str]] = None,
) -> None:
    if newly_closed is None:
        newly_closed = set()

    separator = "-" * 166
    print("\nWALLET LIQUIDITY POSITIONS")
    print(separator)
    if not scan.positions:
        print("No positions discovered.")
    else:
        positions_to_show = []
        for position in scan.positions:
            if position.status == "active":
                positions_to_show.append(position)
            elif position.status in {"inactive", "closed"} and show_inactive:
                positions_to_show.append(position)
            elif position.status == "closed":
                key = f"{position.dex}:{position.position_address}"
                if key in newly_closed:
                    positions_to_show.append(position)

        if not positions_to_show:
            if not show_inactive:
                print("No active positions found.")
            else:
                print("No positions discovered.")
        else:
            try:
                from screener_position_py.analytics.scoring import (
                    PositionScorer,
                    distance_to_boundary_pct,
                    from_liquidity_position,
                    range_status,
                )

                _scorer = PositionScorer()
            except ImportError:
                _scorer = None
            print(
                f"{'DEX':<8} | {'STATUS':<8} | {'POSITION':<16} | {'POOL':<16} | "
                f"{'RANGE':<12} | {'EDGE':<7} | {'FEE APR':<10} | "
                f"{'R/F/B':<11} | {'DATA':<5} | {'SCORE':<6} | ACTION"
            )
            print(separator)
            for position in positions_to_show:
                enrichment = getattr(position, "pool_enrichment", None) or {}
                current_price = enrichment.get("current_price")
                lower_price = enrichment.get("lower_price")
                upper_price = enrichment.get("upper_price")
                range_label = (
                    range_status(lower_price, upper_price, current_price)
                    if _scorer
                    else "-"
                )
                boundary_distance = (
                    distance_to_boundary_pct(lower_price, upper_price, current_price)
                    if _scorer
                    else None
                )
                boundary_text = (
                    "-" if boundary_distance is None else f"{boundary_distance:.1f}%"
                )
                fee_apr = _fee_apr_pct(enrichment)
                fee_apr_text = "-" if fee_apr is None else f"{fee_apr:.1f}%"
                normalized_current = _finite_number(current_price)
                normalized_lower = _finite_number(lower_price)
                normalized_upper = _finite_number(upper_price)
                price_data_available = (
                    normalized_current is not None
                    and normalized_lower is not None
                    and normalized_upper is not None
                    and normalized_upper > normalized_lower
                )
                input_count = int(price_data_available) + int(fee_apr is not None)
                if _scorer is not None:
                    try:
                        score_input = from_liquidity_position(position, enrichment)
                        breakdown, total = _scorer.score(score_input)
                        score_text = f"{total:.1f}"
                        drivers = (
                            f"{breakdown.range_efficiency * 100:.0f}/"
                            f"{breakdown.fee_yield_rate * 100:.0f}/"
                            f"{breakdown.boundary_risk * 100:.0f}"
                        )
                        action = _score_action(
                            position.status,
                            range_label,
                            boundary_distance,
                            total,
                            input_count,
                        )
                    except (ArithmeticError, TypeError, ValueError):
                        score_text = "-"
                        drivers = "-"
                        action = "Invalid data"
                else:
                    score_text = "-"
                    drivers = "-"
                    action = "Unavailable"
                key = f"{position.dex}:{position.position_address}"
                is_newly_closed = position.status == "closed" and key in newly_closed
                if is_newly_closed:
                    action = f"{action} *"
                print(
                    f"{position.dex.upper():<8} | {position.status.upper():<8} | "
                    f"{truncate(position.position_address, 16):<16} | "
                    f"{truncate(position.pool_address or '-', 16):<16} | "
                    f"{truncate(range_label, 12):<12} | {boundary_text:<7} | "
                    f"{fee_apr_text:<10} | {drivers:<11} | "
                    f"{input_count}/2  | {score_text:<6} | {action}"
                )
            print(
                "R/F/B = range, fee-yield, and boundary component scores. "
                "DATA = available price/fee input groups."
            )
            print(
                "A score with incomplete DATA includes neutral defaults. "
                "Fee APR requires quote-valued fee delta and position value."
            )
    print(separator)

    active_positions = [p for p in scan.positions if p.status == "active"]
    inactive_positions = [p for p in scan.positions if p.status == "inactive"]
    closed_positions = [p for p in scan.positions if p.status == "closed"]
    claimable_positions = sum(1 for p in active_positions if p.has_claimable)
    price_enriched = 0
    fee_enriched = 0
    out_of_range = 0
    near_boundary = 0
    for position in active_positions:
        enrichment = getattr(position, "pool_enrichment", None) or {}
        current_price = _finite_number(enrichment.get("current_price"))
        lower_price = _finite_number(enrichment.get("lower_price"))
        upper_price = _finite_number(enrichment.get("upper_price"))
        if (
            current_price is not None
            and lower_price is not None
            and upper_price is not None
            and upper_price > lower_price
        ):
            price_enriched += 1
            if not lower_price <= current_price <= upper_price:
                out_of_range += 1
            boundary_distance = (
                min(
                    abs(current_price - lower_price),
                    abs(upper_price - current_price),
                )
                / (upper_price - lower_price)
                * 100
            )
            if boundary_distance < 2.0:
                near_boundary += 1
        if _fee_apr_pct(enrichment) is not None:
            fee_enriched += 1

    newly_closed_count = len(
        [p for p in closed_positions if f"{p.dex}:{p.position_address}" in newly_closed]
    )

    previously_closed_count = len(closed_positions) - newly_closed_count

    print()
    print("=" * 80)
    print("  POSITION HEALTH SUMMARY")
    print("=" * 80)
    print(f"  Total positions: {len(scan.positions)}")
    print(
        f"  Active: {len(active_positions)} | Inactive: {len(inactive_positions)} | "
        f"Closed: {len(closed_positions)}"
    )
    print(
        f"  Active risk: {out_of_range} out of range | "
        f"{near_boundary} near boundary"
    )
    print(
        f"  Score inputs: price {price_enriched}/{len(active_positions)} | "
        f"fee value {fee_enriched}/{len(active_positions)}"
    )
    print(f"  Active positions with claimable balances: {claimable_positions}")
    print(
        f"  Closed since last scan: {newly_closed_count} | "
        f"Previously known: {previously_closed_count}"
    )
    if newly_closed_count > 0:
        print(f"  ATTENTION: {newly_closed_count} position(s) closed since last scan")
    print("=" * 80)
    print()

