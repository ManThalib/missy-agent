"""Concurrent aggregation of independent DEX pool clients."""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from core.cache import DiskCache, cache_ttl_from_env

from ..filter_config import FilterConfig
from .meteora_client import MeteoraClient
from .orca_client import OrcaClient
from .raydium_client import RaydiumClient


class MultiDexClient:
    """Fetch providers concurrently while isolating individual failures."""

    def __init__(
        self,
        timeout: float = 15.0,
        target_mints: Optional[List[str]] = None,
        cache: Optional[DiskCache] = None,
        cache_ttl_seconds: Optional[float] = None,
    ) -> None:
        self.errors: Dict[str, str] = {}
        self.cache_ttl_seconds = (
            cache_ttl_seconds if cache_ttl_seconds is not None else cache_ttl_from_env()
        )
        if cache is None and os.environ.get("MISSY_ENABLE_CACHE", "1") != "0":
            cache = DiskCache(default_ttl_seconds=self.cache_ttl_seconds)
        self.cache = cache
        self.clients: Dict[str, Any] = {
            "raydium": RaydiumClient(timeout=timeout, target_mints=target_mints),
            "orca": OrcaClient(timeout=timeout),
            "meteora": MeteoraClient(timeout=timeout),
        }
        for client in self.clients.values():
            client.cache = self.cache
            client.cache_ttl_seconds = self.cache_ttl_seconds

    def fetch_pools(
        self, config: FilterConfig, max_pages: int = 5
    ) -> List[Dict[str, Any]]:
        names = list(self.clients) if config.dex == "all" else [config.dex]
        unknown = [name for name in names if name not in self.clients]
        if unknown:
            raise ValueError(f"Unsupported DEX: {unknown[0]}")
        pools: List[Dict[str, Any]] = []
        self.errors = {}
        successful_providers = 0
        # One path for single and multi-provider scans: providers run
        # concurrently and a failure in one never discards the others.
        with ThreadPoolExecutor(max_workers=len(names)) as executor:
            futures = {
                name: executor.submit(
                    self.clients[name].fetch_pools,
                    config,
                    max_pages=max_pages,
                )
                for name in names
            }
            for name in names:
                try:
                    pools.extend(futures[name].result())
                    successful_providers += 1
                except Exception as exc:
                    self.errors[name] = str(exc)
        if successful_providers == 0:
            failures = "; ".join(
                f"{name}: {error}" for name, error in self.errors.items()
            )
            raise RuntimeError(f"All selected providers failed: {failures}")
        return pools
