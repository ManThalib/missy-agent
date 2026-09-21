"""Orca Whirlpool pool-discovery client."""

import time
import urllib.parse
from typing import Any, Dict, List

from ..constants import ORCA_POOLS_URL
from ..filter_config import FilterConfig
from .raydium_client import RaydiumClient


class OrcaClient(RaydiumClient):
    """Fetch pools from Orca's public Whirlpool REST API."""

    def __init__(self, base_url: str = ORCA_POOLS_URL, timeout: float = 15.0) -> None:
        super().__init__(base_url=base_url, timeout=timeout)

    def fetch_pools(
        self, config: FilterConfig, max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        all_pools: List[Dict[str, Any]] = []
        cursor = ""
        page_size = max(1, min(int(config.page_size or 100), 1000))
        sort_key = (config.sort_field or "liquidity").lower()
        if sort_key.startswith("volume"):
            sort_by = f"volume{config.timeframe}"
        elif sort_key.startswith("fee"):
            sort_by = f"fees{config.timeframe}"
        elif sort_key.startswith("apr"):
            sort_by = f"yieldovertvl{config.timeframe}"
        else:
            sort_by = "tvl"
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
            pools = data.get("data", [])
            if not isinstance(pools, list):
                raise RuntimeError("Orca pool list is not an array")
            all_pools.extend(
                {**pool, "_dex": "orca"} for pool in pools if isinstance(pool, dict)
            )
            cursor = (data.get("meta") or {}).get("next") or ""
            if not cursor or not pools:
                break
            time.sleep(0.15)
        return all_pools
