"""
Terminal display and table formatting utilities for pool screener results.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.display import truncate
from .candidate import Candidate

_TABLE_HEADER = (
    f"{'DEX':<8} | {'POOL NAME':<12} | {'TYPE':<9} | {'TVL ($)':<10} | "
    f"{'FEES ($)':<10} | {'FEE/TVL':<9} | {'APR':<7} | "
    f"{'VOLAT (%)':<9} | {'SCORE':<6} | Pool Address"
)


def _row(c: Candidate) -> str:
    ptype = (c.pool_type or "")[:9].upper() or ("CLMM" if c.tick_spacing else "STD")
    return (
        f"{c.dex.upper():<8} | "
        f"{truncate(c.name, 12):<12} | "
        f"{ptype:<9} | "
        f"{c.tvl:<10.0f} | "
        f"{c.daily_fee_usd:<10.1f} | "
        f"{c.fee_tvl_ratio:<8.2f}% | "
        f"{c.apr:<6.1f}% | "
        f"{c.volatility:<9.1f} | "
        f"{c.score:<6.1f} | "
        f"{c.pool_address}"
    )


def _policy_reason_counts(candidates: List[Candidate]) -> Dict[str, int]:
    """Count failed policy gates by gate name (text before the first space)."""
    counts: Counter = Counter()
    for c in candidates:
        for clause in (c.rejected_reason or "").split("; "):
            if clause:
                counts[clause.split(" ")[0]] += 1
    return dict(counts)


def print_results(
    candidates: List[Candidate],
    rejects: Dict[str, int],
) -> None:
    """Print qualified pools, a policy-rejection summary, and filter rejections.

    Only ``eligible`` candidates are listed as qualified. Candidates that matched
    the whitelist but failed a policy gate are summarized separately.
    """
    sep = "-" * 144
    qualified = [c for c in candidates if c.eligible]
    policy_failed = [c for c in candidates if not c.eligible]

    print(sep)
    if not qualified:
        print("No pools cleared all whitelist & policy gates this cycle.")
    else:
        print(f"FOUND {len(qualified)} QUALIFIED POOL(S):")
        print(_TABLE_HEADER)
        print(sep)
        for c in qualified:
            print(_row(c))
    print(sep)

    if policy_failed:
        print(
            f"{len(policy_failed)} pool(s) matched the whitelist but failed policy "
            "gates (emitted with eligible=false, not qualified):"
        )
        for gate, count in sorted(
            _policy_reason_counts(policy_failed).items(), key=lambda item: -item[1]
        )[:8]:
            print(f"  • {gate}: {count} pool(s)")
        print(sep)

    if rejects:
        print(f"Filter Rejections Summary ({len(rejects)} types):")
        sorted_rejects = sorted(rejects.items(), key=lambda item: item[1], reverse=True)
        for reason, count in sorted_rejects[:8]:
            print(f"  • {reason}: {count} pool(s)")
