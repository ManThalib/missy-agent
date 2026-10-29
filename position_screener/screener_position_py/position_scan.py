"""Wallet position scan result."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .liquidity_position import LiquidityPosition


@dataclass
class PositionScan:
    """Aggregate positions, source errors, history completeness, and closures.

    ``newly_closed_keys`` lists ``dex:position_address`` keys closed since the
    previous scan; the closure state file is updated once by the scanner.
    """

    wallet: str
    wallet_id: str = "main"
    sol_balance_lamports: int = 0
    sol_price_usd: float = 0.0
    wallet_total_usd: float = 0.0
    wallet_balances: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[LiquidityPosition] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    history_complete: bool = False
    newly_closed_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wallet": self.wallet,
            "wallet_id": self.wallet_id,
            "sol_balance_lamports": self.sol_balance_lamports,
            "sol_price_usd": self.sol_price_usd,
            "wallet_total_usd": self.wallet_total_usd,
            "wallet_balances": self.wallet_balances,
            "positions": [position.to_dict() for position in self.positions],
            "errors": dict(self.errors),
            "history_complete": self.history_complete,
            "newly_closed_keys": list(self.newly_closed_keys),
        }
