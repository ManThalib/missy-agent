"""Wallet screener orchestrator."""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from core.solana import is_valid_solana_address
from core.wallet_balances import parse_token_account

from .balance_calculator import BalanceCalculator
from .config import WRAPPED_SOL_MINT, resolve_rpc_url
from .models import TokenBalance
from .price_fetcher import TokenPriceFetcher
from .rpc_client import SolanaRpcClient


class WalletScanner:
    """Scan a Solana wallet and return USD-valued token balances."""

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        price_url: Optional[str] = None,
        threshold_usd: float = 0.0,
        timeout: float = 20.0,
        rpc_client: Optional[SolanaRpcClient] = None,
        price_fetcher: Optional[TokenPriceFetcher] = None,
        wallet_id: str = "main",
    ) -> None:
        self.wallet_id = (wallet_id or "main").strip() or "main"
        self.rpc = rpc_client or SolanaRpcClient(
            rpc_url or resolve_rpc_url(), timeout=timeout
        )
        if price_fetcher is not None:
            self.price_fetcher = price_fetcher
        elif price_url:
            self.price_fetcher = TokenPriceFetcher(base_url=price_url, timeout=timeout)
        else:
            self.price_fetcher = TokenPriceFetcher(timeout=timeout)
        # Dust threshold is a policy decision owned by Sheldon. Missy emits
        # all assets; the threshold is carried for compatibility and reported.
        self.calculator = BalanceCalculator(threshold_usd=threshold_usd)

    def scan(self, wallet: str) -> Dict[str, Any]:
        """Fetch balances, price them once, and return all assets sorted by USD value.

        Dust filtering is a policy decision owned by Sheldon. Missy emits all
        assets and reports the threshold it was configured with.
        """
        if not is_valid_solana_address(wallet):
            raise ValueError("wallet must be a 32-byte Solana base58 address")
        balances = self._fetch_balances(wallet)
        prices = self._fetch_prices(balances)
        enriched = self.calculator.enrich(balances, prices)
        enriched.sort(key=lambda b: b.total_value_usd, reverse=True)

        # Native SOL is priced under the wrapped-SOL mint in the same batch; a
        # single targeted request recovers it only when that batch omitted it.
        sol_balance = next((b for b in enriched if b.is_native_sol), None)
        sol_price_usd = sol_balance.price_usd if sol_balance is not None else 0.0
        if sol_balance is not None and sol_price_usd <= 0:
            sol_price_usd = self._fetch_sol_price()
            if sol_price_usd > 0:
                sol_balance.price_usd = sol_price_usd
                sol_balance.total_value_usd = sol_balance.amount_ui * sol_price_usd
                enriched.sort(key=lambda b: b.total_value_usd, reverse=True)

        total_usd = round(sum(b.total_value_usd for b in enriched), 4)
        return {
            "wallet": wallet,
            "wallet_id": self.wallet_id,
            "sol_price_usd": round(sol_price_usd, 8),
            "total_usd": total_usd,
            "wallet_total_usd": total_usd,
            "threshold_usd": self.calculator.threshold_usd,
            "asset_count": len(enriched),
            "assets": [b.to_dict() for b in enriched],
        }

    def _fetch_balances(self, wallet: str) -> List[TokenBalance]:
        balances: List[TokenBalance] = []
        lamports = self.rpc.get_balance_lamports(wallet)
        balances.append(TokenBalance.native_sol(lamports))
        for account in self.rpc.get_token_accounts(wallet):
            parsed = _parse_token_account(account)
            if parsed is not None:
                balances.append(parsed)
        return balances

    def _fetch_sol_price(self) -> float:
        try:
            return self.price_fetcher.fetch_prices([WRAPPED_SOL_MINT]).get(
                WRAPPED_SOL_MINT, 0.0
            )
        except Exception as exc:
            print(f"Warning: SOL price fetch failed: {exc}", file=sys.stderr)
            return 0.0

    def _fetch_prices(self, balances: List[TokenBalance]) -> Dict[str, float]:
        mints = [b.mint for b in balances]
        try:
            return self.price_fetcher.fetch_prices(mints)
        except Exception as exc:
            print(f"Warning: price fetch failed: {exc}", file=sys.stderr)
            return {}


def _parse_token_account(account: Any) -> Optional[TokenBalance]:
    """Map a Solana jsonParsed token account into a ``TokenBalance``."""
    parsed = parse_token_account(account)
    if parsed is None:
        return None
    return TokenBalance(
        mint=parsed["mint"],
        symbol=parsed["symbol"],
        decimals=parsed["decimals"],
        amount_raw=parsed["raw_amount"],
        amount_ui=parsed["ui_amount"],
    )

