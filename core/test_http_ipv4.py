#!/usr/bin/env python3
"""Regression tests for the IPv4-only HTTP client.

The screener host blackholes IPv6, so urllib must never resolve or try an
AAAA address. These tests lock in the resolver family and the opener wiring.
"""

import json
import os
import socket
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.http_client import (
    HttpJsonClient,
    _IPv4HTTPHandler,
    _IPv4HTTPSHandler,
    _create_connection_ipv4,
)

class TestIPv4Resolver(unittest.TestCase):
    def test_resolver_requests_only_ipv4(self):
        calls = []
        fake_sock = unittest.mock.Mock()

        def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            calls.append((host, port, family, type))
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", port),
                )
            ]

        with patch(
            "core.http_client.socket.getaddrinfo", side_effect=fake_getaddrinfo
        ), patch("core.http_client.socket.socket", return_value=fake_sock):
            conn = _create_connection_ipv4(("example.com", 443), timeout=5)

        self.assertIs(conn, fake_sock)
        self.assertEqual(
            calls,
            [("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)],
        )
        fake_sock.settimeout.assert_called_once_with(5)
        fake_sock.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_resolver_raises_when_no_ipv4_address(self):
        with patch("core.http_client.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(OSError):
                _create_connection_ipv4(("example.com", 80), timeout=5)

class TestOpenerWiring(unittest.TestCase):
    def test_opener_uses_ipv4_handlers_only(self):
        client = HttpJsonClient("https://example.com")
        handlers = client._opener.handlers

        self.assertTrue(any(isinstance(h, _IPv4HTTPHandler) for h in handlers))
        self.assertTrue(any(isinstance(h, _IPv4HTTPSHandler) for h in handlers))
        # The stock handlers must be replaced, not stacked after the IPv4 ones.
        self.assertFalse(
            any(type(h) is urllib.request.HTTPHandler for h in handlers)
        )
        self.assertFalse(
            any(type(h) is urllib.request.HTTPSHandler for h in handlers)
        )

class _JsonHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"success": True, "data": [{"pool": "x"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

class TestIPv4ClientFetch(unittest.TestCase):
    def test_get_json_over_custom_http_handler(self):
        server = HTTPServer(("127.0.0.1", 0), _JsonHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HttpJsonClient("http://127.0.0.1")
            url = "http://127.0.0.1:%d/pools" % server.server_port
            self.assertEqual(client._get_json(url), {"success": True, "data": [{"pool": "x"}]})
        finally:
            server.shutdown()
            server.server_close()

if __name__ == "__main__":
    unittest.main()
