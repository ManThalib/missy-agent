"""Token valuation and USD-threshold filtering."""

from __future__ import annotations

from typing import List

from .config import USD_THRESHOLD
from .models import TokenBalance


class BalanceCalculator:
    """Convert raw balances into USD-valued records and filter dust."""

    def __init__(self, threshold_usd: float = USD_THRESHOLD) -> None:
        self.threshold_usd = threshold_usd

    def calculate(self, amount_ui: float, price_usd: float) -> float:
        """Return the USD value of an asset holding."""
        if amount_ui <= 0 or price_usd <= 0:
            return 0.0
        return amount_ui * price_usd

    def filter_assets(self, balances: List[TokenBalance]) -> List[TokenBalance]:
        """Return only assets whose total USD value exceeds the configured threshold."""
        return [b for b in balances if b.total_value_usd > self.threshold_usd]

    def enrich(
        self, balances: List[TokenBalance], prices: dict[str, float]
    ) -> List[TokenBalance]:
        """Attach prices and recompute total_value_usd for each balance."""
        for balance in balances:
            price = prices.get(balance.mint, 0.0)
            if price <= 0 and balance.mint == "":
                continue
            balance.price_usd = price
            balance.total_value_usd = self.calculate(balance.amount_ui, price)
        return balances
