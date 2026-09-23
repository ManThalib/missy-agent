"""Shared HTTP plumbing for DEX pool-discovery clients."""

import json
import time
import urllib.request
from typing import Any, Dict, List

PAGE_DELAY_SECONDS = 0.15
_USER_AGENT = "Mozilla/5.0 (compatible; PoolScreener/1.0)"


class HttpJsonClient:
    """Fetch JSON objects from a provider REST endpoint.

    Concrete clients supply the provider-specific query parameters and
    pagination cursor while sharing the HTTP request, error handling, and
    per-page delay implemented here.
    """

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def _get_json(self, url: str) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
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
            raise RuntimeError(f"Pool API error: {data.get('msg')}")
        if not isinstance(data, dict):
            raise RuntimeError("Pool API returned a non-object JSON payload")
        return data

    @staticmethod
    def _extract_pools(data: Dict[str, Any], dex: str) -> List[Dict[str, Any]]:
        pools = data.get("data", [])
        if not isinstance(pools, list):
            raise RuntimeError(f"{dex.capitalize()} pool list is not an array")
        return [{**pool, "_dex": dex} for pool in pools if isinstance(pool, dict)]

    @staticmethod
    def _pause() -> None:
        time.sleep(PAGE_DELAY_SECONDS)
