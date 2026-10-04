"""Raydium pool-discovery client."""

import urllib.parse
from typing import Any, Dict, List, Optional

from ..constants import RAYDIUM_LIST_V2, RAYDIUM_POOLS_BY_MINT
from ..filter_config import FilterConfig
from .base_client import HttpJsonClient

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


class RaydiumClient(HttpJsonClient):
    """Fetch Raydium Standard and concentrated pools."""

    def __init__(
        self,
        base_url: str = RAYDIUM_LIST_V2,
        timeout: float = 15.0,
        pools_by_mint_url: str = RAYDIUM_POOLS_BY_MINT,
        target_mints: Optional[List[str]] = None,
    ) -> None:
        super().__init__(base_url=base_url, timeout=timeout)
        self.pools_by_mint_url = pools_by_mint_url
        self.target_mints = target_mints or []

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
        if self.target_mints:
            return self._fetch_by_target_mints(config)
        return self._fetch_top_pools(config, max_pages)

    def _fetch_by_target_mints(
        self, config: FilterConfig
    ) -> List[Dict[str, Any]]:
        """Fetch only pools where one side is a target mint.

        The Raydium /poolsByMint endpoint is constrained to a single mint.
        We query every target mint once and de-duplicate by pool id. To avoid
        exploding the number of requests, we do not pre-cross with every quote
        mint; instead we filter in-memory for pools whose other side is an
        configured quote asset or matches another target mint.
        """
        all_pools: List[Dict[str, Any]] = []
        seen: set = set()
        page_size = min(100, max(1, int(config.page_size or 100)))
        for mint in self.target_mints:
            # One page per target mint is enough to cover the most liquid
            # pools; the whitelist filter discards the rest anyway.
            page = 1
            pools = self.fetch_pools_by_mint(
                mint, pool_type="all", page_size=page_size, page=page
            )
            for pool in pools:
                pid = pool.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                all_pools.append(pool)
            if len(pools) == page_size and len(self.target_mints) > 1:
                self._pause()
        return [{**p, "_dex": "raydium"} for p in all_pools]

    def _fetch_top_pools(
        self, config: FilterConfig, max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        sort_field = self._sort_field(config)
        page_size = max(1, min(int(config.page_size or 100), 1000))
        all_pools: List[Dict[str, Any]] = []
        for pool_type in self._pool_types(config):
            next_page_id = ""
            for _ in range(max_pages):
                params: Dict[str, Any] = {
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
                payload = payload if isinstance(payload, dict) else {}
                all_pools.extend(self._extract_pools(payload, "raydium"))
                next_page_id = payload.get("nextPageId") or ""
                if not next_page_id or not payload.get("data"):
                    break
                self._pause()
        return all_pools

    def fetch_pools_by_mint(
        self,
        mint1: str,
        mint2: Optional[str] = None,
        pool_type: str = "all",
        page_size: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
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
        payload = payload if isinstance(payload, dict) else {}
        return [
            pool
            for pool in payload.get("data", [])
            if isinstance(pool, dict)
        ]
