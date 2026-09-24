"""Wallet scanner orchestrator."""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from core.solana import is_valid_solana_address

from .balance_calculator import BalanceCalculator
from .config import USD_THRESHOLD, WRAPPED_SOL_MINT, resolve_rpc_url
from .models import TokenBalance
from .price_fetcher import TokenPriceFetcher
from .rpc_client import SolanaRpcClient


class WalletScanner:
    """Scan a Solana wallet and return USD-valued token balances."""

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        price_url: Optional[str] = None,
        threshold_usd: float = USD_THRESHOLD,
        timeout: float = 20.0,
        rpc_client: Optional[SolanaRpcClient] = None,
        price_fetcher: Optional[TokenPriceFetcher] = None,
    ) -> None:
        self.rpc = rpc_client or SolanaRpcClient(
            rpc_url or resolve_rpc_url(), timeout=timeout
        )
        if price_fetcher is not None:
            self.price_fetcher = price_fetcher
        elif price_url:
            self.price_fetcher = TokenPriceFetcher(base_url=price_url, timeout=timeout)
        else:
            self.price_fetcher = TokenPriceFetcher(timeout=timeout)
        self.calculator = BalanceCalculator(threshold_usd=threshold_usd)

    def scan(self, wallet: str) -> Dict[str, Any]:
        """Fetch balances, price them, filter dust, and sort by USD value."""
        if not is_valid_solana_address(wallet):
            raise ValueError("wallet must be a 32-byte Solana base58 address")
        balances = self._fetch_balances(wallet)
        prices = self._fetch_prices(balances)
        enriched = self.calculator.enrich(balances, prices)
        filtered = self.calculator.filter_assets(enriched)
        filtered.sort(key=lambda b: b.total_value_usd, reverse=True)
        return {
            "wallet": wallet,
            "total_usd": round(sum(b.total_value_usd for b in filtered), 4),
            "asset_count": len(filtered),
            "assets": [b.to_dict() for b in filtered],
            "threshold_usd": self.calculator.threshold_usd,
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

    def _fetch_prices(self, balances: List[TokenBalance]) -> Dict[str, float]:
        mints = [b.mint for b in balances]
        try:
            return self.price_fetcher.fetch_prices(mints)
        except Exception as exc:
            print(f"Warning: price fetch failed: {exc}", file=sys.stderr)
            return {}


def _parse_token_account(account: Any) -> Optional[TokenBalance]:
    """Map a shared ``core.wallet_balances`` record into a ``TokenBalance``."""
    if not isinstance(account, dict):
        return None
    mint = account.get("mint")
    if not mint:
        return None
    try:
        decimals = int(account.get("decimals", 0) or 0)
    except (TypeError, ValueError):
        decimals = 0
    try:
        ui_amount = float(account.get("ui_amount", 0.0) or 0.0)
    except (TypeError, ValueError):
        ui_amount = 0.0
    return TokenBalance(
        mint=mint,
        symbol="wSOL" if mint == WRAPPED_SOL_MINT else account.get("symbol", ""),
        decimals=decimals,
        amount_raw=str(account.get("raw_amount", "0")),
        amount_ui=ui_amount,
    )
