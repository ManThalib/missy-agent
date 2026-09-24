"""Jupiter Price API v2 client (keyless)."""

from __future__ import annotations

import math
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .constants import JUPITER_PRICE_V2_URL
from .http import HttpJsonClient

DEFAULT_BATCH_SIZE = 50
DEFAULT_PAUSE_SECONDS = 0.15


class JupiterPriceClient(HttpJsonClient):
    """Resolve USD prices for SPL mints via Jupiter Price API v2."""

    def __init__(
        self,
        base_url: str = JUPITER_PRICE_V2_URL,
        timeout: float = 15.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
        pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    ) -> None:
        super().__init__(base_url, timeout=timeout)
        self.batch_size = max(1, batch_size)
        self.pause_seconds = max(0.0, pause_seconds)

    def fetch_prices(self, mints: Iterable[str]) -> Dict[str, float]:
        """Return ``{mint: usd_price}`` for mints Jupiter can price."""
        unique: List[str] = []
        seen = set()
        for mint in mints:
            if not mint or mint in seen:
                continue
            seen.add(mint)
            unique.append(mint)

        prices: Dict[str, float] = {}
        for start in range(0, len(unique), self.batch_size):
            batch = unique[start : start + self.batch_size]
            params = {"ids": ",".join(batch)}
            data = self._get_json(
                f"{self.base_url}?{urllib.parse.urlencode(params)}"
            )
            prices.update(self._parse_prices(data))
            if start + self.batch_size < len(unique):
                if self.pause_seconds > 0:
                    time.sleep(self.pause_seconds)
        return prices

    @staticmethod
    def _parse_prices(data: Mapping[str, Any]) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        if not isinstance(data, Mapping):
            return prices
        # Jupiter Price API v3 returns a flat mapping of mint -> price entry.
        if "data" not in data:
            for mint, entry in data.items():
                if not isinstance(entry, Mapping):
                    continue
                price = _entry_price(entry)
                if price is not None:
                    prices[mint] = price
            return prices
        payload = data.get("data", {})
        if isinstance(payload, Mapping):
            for mint, entry in payload.items():
                price = _entry_price(entry)
                if price is not None:
                    prices[mint] = price
        elif isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, Mapping):
                    continue
                mint = entry.get("id") or entry.get("mint")
                price = _entry_price(entry)
                if mint and price is not None:
                    prices[mint] = price
        return prices


def _entry_price(entry: Any) -> Optional[float]:
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("price", entry.get("usdPrice"))
    if raw is None:
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price
