"""Meteora DLMM pool-discovery client."""

import urllib.parse
from typing import Any, Dict, List

from ..constants import METEORA_DISCOVER_URL
from ..filter_config import FilterConfig
from .base_client import HttpJsonClient


class MeteoraClient(HttpJsonClient):
    """Fetch pools from Meteora's DLMM discovery API."""

    def __init__(
        self, base_url: str = METEORA_DISCOVER_URL, timeout: float = 15.0
    ) -> None:
        super().__init__(base_url, timeout=timeout)

    @staticmethod
    def _filters(config: FilterConfig) -> List[str]:
        filters = ["pool_type=dlmm"]
        if config.min_tvl > 0:
            filters.append(f"tvl>={config.min_tvl:.0f}")
        if config.max_tvl > 0:
            filters.append(f"tvl<={config.max_tvl:.0f}")
        if config.min_bin_step > 0:
            filters.append(f"dlmm_bin_step>={config.min_bin_step}")
        if config.max_bin_step > 0:
            filters.append(f"dlmm_bin_step<={config.max_bin_step}")
        if config.min_volume_usd > 0:
            filters.append(f"volume>={config.min_volume_usd:.0f}")
        return filters

    def fetch_pools(
        self, config: FilterConfig, max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        if config.timeframe not in ("5m", "30m", "24h"):
            raise ValueError("Meteora supports only 5m, 30m, or 24h timeframes")
        filters = self._filters(config)

        all_pools: List[Dict[str, Any]] = []
        after_key = ""
        for _ in range(max_pages):
            params: Dict[str, Any] = {
                "page_size": max(1, min(int(config.page_size), 100)),
                "timeframe": config.timeframe,
                "category": "all",
                "include_unknown": "true",
                "filter_by": "&&".join(filters),
            }
            if after_key:
                params["after_key"] = after_key
            data = self._get_json(f"{self.base_url}?{urllib.parse.urlencode(params)}")
            all_pools.extend(self._extract_pools(data, "meteora"))
            after_key = data.get("after_key") or ""
            if not data.get("has_more") or not after_key or not data.get("data"):
                break
            self._pause()
        return all_pools
