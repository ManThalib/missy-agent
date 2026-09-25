"""Shared standard-library HTTP client for JSON APIs."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

PAGE_DELAY_SECONDS = 0.15
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; PoolScreener/1.0)"


class HttpJsonClient:
    """Fetch JSON objects from a REST endpoint.

    Concrete clients supply provider-specific query parameters and pagination
    while sharing the HTTP request, error handling, and per-page delay.
    """

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def _get_json(self, url: str) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            raise RuntimeError(f"GET {url[:120]} failed: {exc}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"API error: {data.get('msg') or 'unknown error'}")
        if not isinstance(data, dict):
            raise RuntimeError("API returned a non-object JSON payload")
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
