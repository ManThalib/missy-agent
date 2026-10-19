"""Shared standard-library HTTP client for JSON APIs."""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .cache import DiskCache

PAGE_DELAY_SECONDS = 0.15
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; PoolScreener/1.0)"


def _create_connection_ipv4(
    address, timeout: Optional[float] = None, source_address=None
):
    """Connect over IPv4 only.

    The screener host has a default IPv6 route that blackholes outbound
    traffic. urllib tries every AAAA address first, waiting the full socket
    timeout on each, so a single discovery request can stall for 30s and the
    hourly pipeline gets killed before it prints JSON. curl succeeds because
    it falls back to IPv4 immediately.
    """
    host, port = address
    last_error: Optional[OSError] = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"getaddrinfo returned no IPv4 addresses for {host}")


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _create_connection_ipv4(
            (self.host, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        self.sock = _create_connection_ipv4(
            (self.host, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname
        )


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_IPv4HTTPConnection, req)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _IPv4HTTPSConnection,
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


class HttpJsonClient:
    """Fetch JSON objects from a REST endpoint.

    Concrete clients supply provider-specific query parameters and pagination
    while sharing the HTTP request, error handling, and per-page delay.
    """

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.cache: Optional[DiskCache] = None
        self._opener = urllib.request.build_opener(
            _IPv4HTTPHandler(), _IPv4HTTPSHandler()
        )

    def _cache_key(self, url: str) -> str:
        return url

    def _get_json(self, url: str) -> Dict[str, Any]:
        cache = self.cache
        if cache is not None:
            cached = cache.get("http", self._cache_key(url))
            if cached is not None:
                return cached

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            raise RuntimeError(f"GET {url[:120]} failed: {exc}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"API error: {data.get('msg') or 'unknown error'}")
        if not isinstance(data, dict):
            raise RuntimeError("API returned a non-object JSON payload")
        if cache is not None:
            cache.set("http", self._cache_key(url), value=data, ttl_seconds=300)
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
