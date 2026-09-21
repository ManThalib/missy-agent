"""Provider-independent liquidity position record."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LiquidityPosition:
    """Current or historical state for one DEX liquidity position."""

    dex: str
    position_address: str
    pool_address: str = ""
    status: str = "inactive"
    position_mint: str = ""
    liquidity_raw: int = 0
    lower_bound: Optional[int] = None
    upper_bound: Optional[int] = None
    fees_owed_raw: List[int] = field(default_factory=list)
    rewards_owed_raw: List[int] = field(default_factory=list)
    opened_signature: str = ""
    closed_signature: str = ""
    opened_at: Optional[int] = None
    closed_at: Optional[int] = None
    source: str = "rpc"
    # Optional enrichment from a normalized pool snapshot; kept separate
    # so the core model stays protocol-agnostic.
    pool_enrichment: Optional[Dict[str, Any]] = field(default=None)

    @property
    def has_claimable(self) -> bool:
        return any(self.fees_owed_raw) or any(self.rewards_owed_raw)

    def to_dict(self, include_enrichment: bool = False) -> Dict[str, Any]:
        """Return a detached JSON-compatible representation."""
        result = asdict(self)
        result["has_claimable"] = self.has_claimable
        if not include_enrichment:
            result.pop("pool_enrichment", None)
        return result
