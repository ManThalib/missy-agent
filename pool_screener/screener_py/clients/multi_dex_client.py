"""Concurrent aggregation of independent DEX pool clients."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from ..filter_config import FilterConfig
from .meteora_client import MeteoraClient
from .orca_client import OrcaClient
from .raydium_client import RaydiumClient


class MultiDexClient:
    """Fetch providers concurrently while isolating individual failures."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.errors: Dict[str, str] = {}
        self.clients: Dict[str, Any] = {
            "raydium": RaydiumClient(timeout=timeout),
            "orca": OrcaClient(timeout=timeout),
            "meteora": MeteoraClient(timeout=timeout),
        }

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
        if len(names) == 1:
            name = names[0]
            try:
                provider_pools = self.clients[name].fetch_pools(
                    config, max_pages=max_pages
                )
                pools.extend(provider_pools)
                successful_providers += 1
            except Exception as exc:
                self.errors[name] = str(exc)
        else:
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
