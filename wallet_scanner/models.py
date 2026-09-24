"""Data models for the wallet scanner."""

from dataclasses import dataclass
from typing import Any, Dict

from .config import LAMPORTS_PER_SOL, SOL_DECIMALS, WRAPPED_SOL_MINT


@dataclass
class TokenBalance:
    """A single wallet asset with its USD valuation."""

    mint: str
    symbol: str = ""
    decimals: int = 0
    amount_raw: str = "0"
    amount_ui: float = 0.0
    price_usd: float = 0.0
    total_value_usd: float = 0.0
    is_native_sol: bool = False

    @property
    def has_price(self) -> bool:
        return self.price_usd > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "amount_raw": self.amount_raw,
            "amount_ui": self.amount_ui,
            "price_usd": self.price_usd,
            "total_value_usd": self.total_value_usd,
            "is_native_sol": self.is_native_sol,
        }

    @classmethod
    def native_sol(cls, lamports: int, price_usd: float = 0.0) -> "TokenBalance":
        """Build a native SOL balance from lamports."""
        amount_ui = lamports / LAMPORTS_PER_SOL
        return cls(
            mint=WRAPPED_SOL_MINT,
            symbol="SOL",
            decimals=SOL_DECIMALS,
            amount_raw=str(lamports),
            amount_ui=amount_ui,
            price_usd=price_usd,
            total_value_usd=amount_ui * price_usd,
            is_native_sol=True,
        )
