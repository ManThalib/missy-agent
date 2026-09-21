"""Raydium pool-discovery client."""

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..constants import RAYDIUM_LIST_V2, RAYDIUM_POOLS_BY_MINT
from ..filter_config import FilterConfig

SORT_FIELDS = {
    "24h": {
        "liquidity": "liquidity",
        "volume": "volume24h",
        "fee": "fee24h",
        "apr": "apr24h",
    },
    "7d": {
        "liquidity": "liquidity",
        "volume": "volume7d",
        "fee": "fee7d",
        "apr": "apr7d",
    },
    "30d": {
        "liquidity": "liquidity",
        "volume": "volume30d",
        "fee": "fee30d",
        "apr": "apr30d",
    },
}


class RaydiumClient:
    """Fetch Raydium Standard and concentrated pools."""

    def __init__(
        self,
        base_url: str = RAYDIUM_LIST_V2,
        timeout: float = 15.0,
        pools_by_mint_url: str = RAYDIUM_POOLS_BY_MINT,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.pools_by_mint_url = pools_by_mint_url

    def _get_json(self, url: str) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PoolScreener/1.0)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"Pool API error: HTTP {response.status}")
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"GET {url[:120]} failed: {exc}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"Raydium API error: {data.get('msg')}")
        if not isinstance(data, dict):
            raise RuntimeError("Pool API returned a non-object JSON payload")
        return data

    @staticmethod
    def _sort_field(config: FilterConfig) -> str:
        fields = SORT_FIELDS.get(config.timeframe, SORT_FIELDS["24h"])
        requested = (config.sort_field or "liquidity").lower()
        for name in ("volume", "fee", "apr"):
            if requested.startswith(name):
                return fields[name]
        return fields["liquidity"]

    @staticmethod
    def _pool_types(config: FilterConfig) -> List[Optional[str]]:
        pool_type = (config.pool_type or "all").lower()
        if pool_type in ("concentrated", "clmm"):
            return ["Concentrated"]
        if pool_type in ("standard", "cpmm", "amm"):
            return ["Standard"]
        return ["Concentrated", "Standard"]

    def fetch_pools(
        self, config: FilterConfig, max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        sort_field = self._sort_field(config)
        page_size = max(1, min(int(config.page_size or 100), 1000))
        all_pools: List[Dict[str, Any]] = []
        for pool_type in self._pool_types(config):
            next_page_id = ""
            for _ in range(max_pages):
                params = {
                    "poolType": pool_type,
                    "sortField": sort_field,
                    "sortType": "desc",
                    "size": page_size,
                }
                if next_page_id:
                    params["nextPageId"] = next_page_id
                data = self._get_json(
                    f"{self.base_url}?{urllib.parse.urlencode(params)}"
                )
                payload = data.get("data", {})
                pools = payload.get("data", []) if isinstance(payload, dict) else []
                if not isinstance(pools, list):
                    raise RuntimeError("Raydium pool list is not an array")
                all_pools.extend(
                    {**pool, "_dex": "raydium"}
                    for pool in pools
                    if isinstance(pool, dict)
                )
                next_page_id = (
                    payload.get("nextPageId") or "" if isinstance(payload, dict) else ""
                )
                if not next_page_id or not pools:
                    break
                time.sleep(0.15)
        return all_pools

    def fetch_pools_by_mint(
        self,
        mint1: str,
        mint2: Optional[str] = None,
        pool_type: str = "all",
        page_size: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        params = {
            "mint1": mint1,
            "poolType": pool_type,
            "poolSortField": "liquidity",
            "sortType": "desc",
            "pageSize": min(max(1, page_size), 1000),
            "page": max(1, page),
        }
        if mint2:
            params["mint2"] = mint2
        data = self._get_json(
            f"{self.pools_by_mint_url}?{urllib.parse.urlencode(params)}"
        )
        payload = data.get("data", {})
        pools = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(pools, list):
            raise RuntimeError("Raydium pool list is not an array")
        return [pool for pool in pools if isinstance(pool, dict)]
