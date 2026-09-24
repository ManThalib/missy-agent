"""RPC client for fetching wallet balances."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

from core.rpc import RpcClient
from core.wallet_balances import (
    fetch_sol_balance_lamports,
    fetch_token_accounts,
)

from .config import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
)


class SolanaRpcClient:
    """Fetch native SOL and SPL token balances for a wallet.

    Retries transient RPC failures with linear backoff and delegates the
    ``jsonParsed`` traversal to the shared ``core.wallet_balances`` helpers.
    """

    def __init__(
        self,
        url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.max_retries = max(max_retries, 1)
        self.backoff_seconds = max(backoff_seconds, 0.0)
        self._rpc = RpcClient(url, timeout=timeout)

    def get_balance_lamports(self, wallet: str) -> int:
        """Return native SOL balance in lamports."""
        return self._with_retry(
            lambda: fetch_sol_balance_lamports(self._rpc, wallet),
            "getBalance",
        )

    def get_token_accounts(self, wallet: str) -> List[Dict[str, Any]]:
        """Return parsed SPL/Token-2022 token balances as dictionaries."""
        return self._with_retry(
            lambda: fetch_token_accounts(self._rpc, wallet),
            "getTokenAccountsByOwner",
        )

    def _with_retry(self, operation: Callable[[], Any], label: str) -> Any:
        last_error: Exception = RuntimeError("no attempts made")
        for attempt in range(1, self.max_retries + 1):
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(
            f"{label} failed after {self.max_retries} attempts: {last_error}"
        ) from last_error
