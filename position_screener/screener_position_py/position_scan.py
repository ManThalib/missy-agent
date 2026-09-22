"""Wallet position scan result."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .liquidity_position import LiquidityPosition


@dataclass
class PositionScan:
    """Aggregate positions, source errors, and history completeness."""

    wallet: str
    sol_balance_lamports: int = 0
    sol_price_usd: float = 0.0
    wallet_total_usd: float = 0.0
    wallet_balances: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[LiquidityPosition] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    history_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wallet": self.wallet,
            "sol_balance_lamports": self.sol_balance_lamports,
            "sol_price_usd": self.sol_price_usd,
            "wallet_total_usd": self.wallet_total_usd,
            "wallet_balances": self.wallet_balances,
            "positions": [position.to_dict() for position in self.positions],
            "errors": dict(self.errors),
            "history_complete": self.history_complete,
        }
