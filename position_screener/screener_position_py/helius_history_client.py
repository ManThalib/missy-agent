"""Helius Parsed Events position-history client."""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class HeliusHistoryClient:
    """Fetch complete or bounded finalized wallet transaction history."""

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self.url = (
            "https://mainnet.helius-rpc.com/v1/parsed-events/"
            "transaction-history?api-key=" + urllib.parse.quote(api_key)
        )
        self.timeout = timeout

    def fetch(
        self, address: str, max_pages: int = 0
    ) -> Tuple[List[Dict[str, Any]], bool]:
        transactions: List[Dict[str, Any]] = []
        token: Optional[str] = None
        seen_tokens = set()
        page_number = 0
        while max_pages <= 0 or page_number < max_pages:
            payload: Dict[str, Any] = {
                "address": address,
                "limit": 100,
                "sortOrder": "asc",
                "commitment": "finalized",
            }
            if token:
                payload["paginationToken"] = token
            request = urllib.request.Request(
                self.url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "SolanaPositionScreener/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    page = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, ValueError) as exc:
                raise RuntimeError(f"Helius history request failed: {exc}") from exc
            if not isinstance(page, dict) or not isinstance(page.get("data", []), list):
                raise RuntimeError("Invalid Helius parsed-history response")
            transactions.extend(
                item for item in page.get("data", []) if isinstance(item, dict)
            )
            page_number += 1
            token = page.get("paginationToken")
            if not token:
                return transactions, True
            if token in seen_tokens:
                raise RuntimeError("Helius history repeated a pagination token")
            seen_tokens.add(token)
        return transactions, False
