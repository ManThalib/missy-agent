"""Orca Whirlpool pool-discovery client."""

import urllib.parse
from typing import Any, Dict, List

from ..constants import ORCA_POOLS_URL
from ..filter_config import FilterConfig
from .base_client import HttpJsonClient


class OrcaClient(HttpJsonClient):
    """Fetch pools from Orca's public Whirlpool REST API."""

    def __init__(self, base_url: str = ORCA_POOLS_URL, timeout: float = 15.0) -> None:
        super().__init__(base_url, timeout=timeout)

    @staticmethod
    def _sort_by(config: FilterConfig) -> str:
        sort_key = (config.sort_field or "liquidity").lower()
        if sort_key.startswith("volume"):
            return f"volume{config.timeframe}"
        if sort_key.startswith("fee"):
            return f"fees{config.timeframe}"
        if sort_key.startswith("apr"):
            return f"yieldovertvl{config.timeframe}"
        return "tvl"

    def fetch_pools(
        self, config: FilterConfig, max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        all_pools: List[Dict[str, Any]] = []
        cursor = ""
        page_size = max(1, min(int(config.page_size or 100), 1000))
        sort_by = self._sort_by(config)
        for _ in range(max_pages):
            params: Dict[str, Any] = {
                "sortBy": sort_by,
                "sortDirection": "desc",
                "size": page_size,
                "stats": config.timeframe,
            }
            if config.min_tvl > 0:
                params["minTvl"] = config.min_tvl
            if config.min_volume_usd > 0:
                params["minVolume"] = config.min_volume_usd
            if cursor:
                params["next"] = cursor
            data = self._get_json(f"{self.base_url}?{urllib.parse.urlencode(params)}")
            all_pools.extend(self._extract_pools(data, "orca"))
            cursor = (data.get("meta") or {}).get("next") or ""
            if not cursor or not data.get("data"):
                break
            self._pause()
        return all_pools
