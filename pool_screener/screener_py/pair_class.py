"""Pair-classification helper used by Missy scoring and emitted as a fact."""

from typing import Any, Dict, Optional, Sequence


def _set(values: Optional[Sequence[str]] = None) -> set:
    if values is None:
        return set()
    return set(values)


def classify_pair(
    sym_x: str,
    sym_y: str,
    stablecoins: Optional[Sequence[str]] = None,
    high_caps: Optional[Sequence[str]] = None,
) -> str:
    """Classify a canonical pair into one of the scoring cohorts.

    Returns one of: stable_stable, stable_bluechip, bluechip_bluechip,
    off_universe. Symbol aliases are upper-cased and compared to the provided
    universe lists.
    """
    stables = _set(stablecoins)
    caps = _set(high_caps)

    sx = (sym_x or "").upper()
    sy = (sym_y or "").upper()

    x_in = sx in stables or sx in caps
    y_in = sy in stables or sy in caps

    if not x_in or not y_in:
        return "off_universe"

    x_stable = sx in stables
    y_stable = sy in stables

    if x_stable and y_stable:
        return "stable_stable"
    if x_stable or y_stable:
        return "stable_bluechip"
    return "bluechip_bluechip"


def classify_pair_from_policy(sym_x: str, sym_y: str, policy: Dict[str, Any]) -> str:
    """Convenience wrapper that reads universe lists from a Missy policy."""
    universe = policy.get("universe") or {}
    return classify_pair(
        sym_x,
        sym_y,
        stablecoins=universe.get("stablecoins"),
        high_caps=universe.get("high_caps"),
    )
