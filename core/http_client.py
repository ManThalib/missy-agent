"""Shared standard-library HTTP client for JSON APIs.

Connections are kept alive per host and retries are limited to transient
failures (timeouts, resets, HTTP 429 and 5xx) with a capped Retry-After.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .cache import DiskCache, cache_ttl_from_env
from .retry import call_with_retry

PAGE_DELAY_SECONDS = 0.15
REQUEST_ATTEMPTS = 3
REQUEST_BACKOFF_SECONDS = 0.5
MAX_REDIRECTS = 3
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; PoolScreener/1.0)"
_REDACTED_QUERY_KEYS = frozenset({"api-key", "api_key", "apikey", "key", "token"})
_CONNECTION_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    BrokenPipeError,
    ConnectionResetError,
)


def redact_url(url: str) -> str:
    """Return *url* with credential-bearing query values replaced by ``***``."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    cleaned = [
        (key, "***" if key.lower() in _REDACTED_QUERY_KEYS else value)
        for key, value in pairs
    ]
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(cleaned))
    )


class HttpStatusError(RuntimeError):
    """A non-success HTTP response; carries the status and Retry-After hint."""

    def __init__(
        self, url: str, status_code: int, retry_after: Optional[float] = None
    ) -> None:
        super().__init__(f"GET {redact_url(url)[:120]} failed: HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds from a Retry-After header (numeric form only); None if absent."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


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


class HttpJsonClient:
    """Fetch JSON objects from a REST endpoint.

    Concrete clients supply provider-specific query parameters and pagination
    while sharing the HTTP request, retry, caching, and per-page delay logic.
    """

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.cache: Optional[DiskCache] = None
        self.cache_ttl_seconds: float = cache_ttl_from_env()
        self._connections: Dict[Tuple[str, str, int], http.client.HTTPConnection] = {}

    def _cache_key(self, url: str) -> str:
        return url

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()

    def _connection(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        connection = self._connections.get(key)
        if connection is None:
            cls = _IPv4HTTPSConnection if scheme == "https" else _IPv4HTTPConnection
            connection = cls(host, port, timeout=self.timeout)
            self._connections[key] = connection
        return connection

    def _send(self, url: str) -> Tuple[int, Dict[str, str], bytes]:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme or "https"
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        headers = {"User-Agent": _DEFAULT_USER_AGENT, "Accept": "application/json"}
        for attempt in range(2):
            connection = self._connection(scheme, parts.hostname or "", port)
            try:
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                body = response.read()
                return response.status, dict(response.getheaders()), body
            except _CONNECTION_ERRORS:
                # Idle keep-alive sockets get closed by the server; reconnect once.
                connection.close()
                self._connections.pop((scheme, parts.hostname or "", port), None)
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def _request(self, url: str) -> bytes:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            status, headers, body = self._send(current)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location") or headers.get("location")
                if not location:
                    raise HttpStatusError(current, status)
                current = urllib.parse.urljoin(current, location)
                continue
            if status >= 400:
                raise HttpStatusError(
                    current,
                    status,
                    parse_retry_after(headers.get("Retry-After")),
                )
            return body
        raise RuntimeError(f"GET {redact_url(url)[:120]} exceeded redirect limit")

    def _get_json(self, url: str) -> Dict[str, Any]:
        cache = self.cache
        if cache is not None:
            cached = cache.get("http", self._cache_key(url))
            if cached is not None:
                return cached

        try:
            body = call_with_retry(
                lambda: self._request(url),
                attempts=REQUEST_ATTEMPTS,
                base_seconds=REQUEST_BACKOFF_SECONDS,
                retry_after_of=lambda exc: getattr(exc, "retry_after", None),
            )
        except HttpStatusError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"GET {redact_url(url)[:120]} failed: {exc}") from exc

        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError(f"GET {redact_url(url)[:120]} failed: {exc}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"API error: {data.get('msg') or 'unknown error'}")
        if not isinstance(data, dict):
            raise RuntimeError("API returned a non-object JSON payload")
        if cache is not None:
            cache.set("http", self._cache_key(url), value=data, ttl_seconds=self.cache_ttl_seconds)
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
