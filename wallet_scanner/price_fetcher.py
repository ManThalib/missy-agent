"""Live USD price fetcher backed by the Jupiter Price API v2."""

from __future__ import annotations

from typing import Iterable

from core.constants import JUPITER_PRICE_V2_URL
from core.prices import JupiterPriceClient

from .config import DEFAULT_TIMEOUT_SECONDS, PRICE_BATCH_SIZE


class TokenPriceFetcher:
    """Batch-resolve SPL mint USD prices via Jupiter Price API v2."""

    def __init__(
        self,
        base_url: str = JUPITER_PRICE_V2_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        batch_size: int = PRICE_BATCH_SIZE,
    ) -> None:
        self._client = JupiterPriceClient(
            base_url=base_url, timeout=timeout, batch_size=batch_size
        )

    def fetch_prices(self, mints: Iterable[str]) -> dict[str, float]:
        """Return ``{mint: usd_price}`` for all requested mints."""
        return self._client.fetch_prices(mints)
