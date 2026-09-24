"""
Terminal display and table formatting utilities for pool screener results.
"""

from __future__ import annotations

from typing import Dict, List

from core.display import truncate
from .candidate import Candidate

def print_results(
    candidates: List[Candidate],
    rejects: Dict[str, int],
) -> None:
    """Prints formatted table of screened candidate pools and rejection summary."""
    sep = "-" * 144
    print(sep)
    if not candidates:
        print("No pools cleared all whitelist & filter gates this cycle.")
    else:
        print(f"FOUND {len(candidates)} QUALIFIED POOL(S):")
        header = (
            f"{'DEX':<8} | {'POOL NAME':<12} | {'TYPE':<9} | {'TVL ($)':<10} | "
            f"{'FEES ($)':<10} | {'FEE/TVL':<9} | {'APR':<7} | "
            f"{'VOL (%)':<7} | {'SCORE':<6} | "
        )
        header += "Pool Address"
        print(header)
        print(sep)
        for c in candidates:
            ptype = (c.pool_type or "")[:9].upper() or (
                "CLMM" if c.tick_spacing else "STD"
            )
            line = (
                f"{c.dex.upper():<8} | "
                f"{truncate(c.name, 12):<12} | "
                f"{ptype:<9} | "
                f"{c.tvl:<10.0f} | "
                f"{c.daily_fee_usd:<10.1f} | "
                f"{c.fee_tvl_ratio:<8.2f}% | "
                f"{c.apr:<6.1f}% | "
                f"{c.volatility:<7.1f} | "
                f"{c.score:<6.1f} | "
                f"{c.pool_address}"
            )
            print(line)
    print(sep)

    if rejects:
        print(f"Filter Rejections Summary ({len(rejects)} types):")
        sorted_rejects = sorted(rejects.items(), key=lambda item: item[1], reverse=True)
        for reason, count in sorted_rejects[:8]:
            print(f"  • {reason}: {count} pool(s)")
