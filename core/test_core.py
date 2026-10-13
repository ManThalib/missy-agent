#!/usr/bin/env python3
"""Tests for shared core utilities."""

import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.display import truncate
from core.normalize import finite_number
from core.prices import JupiterPriceClient
from core.rpc import RpcClient, RpcRateLimited
from core.solana import b58decode, b58encode, discriminator, is_valid_solana_address


class TestSolanaHelpers(unittest.TestCase):
    def test_base58_round_trip(self):
        value = bytes(range(32))
        self.assertEqual(b58decode(b58encode(value)), value)

    def test_base58_leading_zero_bytes(self):
        value = b"\0\0\x01\x02"
        self.assertEqual(b58decode(b58encode(value)), value)

    def test_discriminator_is_eight_bytes(self):
        self.assertEqual(len(discriminator("Position")), 8)

    def test_valid_address(self):
        self.assertTrue(
            is_valid_solana_address("So11111111111111111111111111111111111111112")
        )

    def test_invalid_addresses(self):
        for value in ("", "not-a-wallet", "0OIl", None):
            self.assertFalse(is_valid_solana_address(value))


class TestNormalize(unittest.TestCase):
    def test_finite_number_accepts_numeric_strings(self):
        self.assertEqual(finite_number("1.5"), 1.5)

    def test_finite_number_rejects_non_finite_and_invalid(self):
        self.assertIsNone(finite_number("nan"))
        self.assertIsNone(finite_number("inf"))
        self.assertIsNone(finite_number("abc"))
        self.assertIsNone(finite_number(None))


class TestDisplay(unittest.TestCase):
    def test_truncate_short(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_truncate_long(self):
        self.assertEqual(truncate("abcdef", 5), "ab...")

    def test_truncate_tiny_width(self):
        self.assertEqual(truncate("abcdef", 3), "...")


class TestJupiterPriceClient(unittest.TestCase):
    def test_parse_prices_filters_non_positive(self):
        data = {
            "data": {
                "mint-a": {"price": "1.5"},
                "mint-b": {"price": "0"},
                "mint-c": {"price": "-2"},
                "mint-d": {"price": None},
            }
        }
        self.assertEqual(JupiterPriceClient._parse_prices(data), {"mint-a": 1.5})

    def test_parse_prices_list_shape(self):
        data = {"data": [{"id": "mint-a", "price": "3.0"}]}
        self.assertEqual(JupiterPriceClient._parse_prices(data), {"mint-a": 3.0})


class _StubRPCHandler(BaseHTTPRequestHandler):
    """Reply per self.server.responses: list of (status, headers, body)."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        status, headers, body = self.server.responses.pop(0)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestRpcClient(unittest.TestCase):
    def setUp(self):
        # The loopback test server must bypass any ambient HTTP(S)_PROXY.
        env_patcher = patch.dict(
            os.environ,
            {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"},
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _serve(self, responses):
        server = HTTPServer(("127.0.0.1", 0), _StubRPCHandler)
        server.responses = list(responses)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup is LIFO: shutdown first, then join, then close socket.
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/"

    def test_429_raises_rate_limited_with_retry_after(self):
        url = self._serve([
            (429, {"Retry-After": "7"}, b'{"error": "rate limited"}'),
        ])
        with self.assertRaises(RpcRateLimited) as ctx:
            RpcClient(url).call("getAccountInfo", ["addr"])
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_429_with_http_date_retry_after_is_parsed(self):
        from email.utils import formatdate

        url = self._serve([
            (429, {"Retry-After": formatdate(time.time() + 60, usegmt=True)}, b"{}"),
        ])
        with self.assertRaises(RpcRateLimited) as ctx:
            RpcClient(url).call("getAccountInfo", ["addr"])
        self.assertIsNotNone(ctx.exception.retry_after)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_other_http_errors_stay_runtime_error(self):
        url = self._serve([
            (500, {}, b'{"error": "boom"}'),
        ])
        with self.assertRaises(RuntimeError) as ctx:
            RpcClient(url).call("getAccountInfo", ["addr"])
        self.assertNotIsInstance(ctx.exception, RpcRateLimited)

    def test_success_returns_result(self):
        url = self._serve([
            (200, {}, b'{"jsonrpc": "2.0", "id": 1, "result": {"value": null}}'),
        ])
        result = RpcClient(url).get_account_info("addr")
        self.assertEqual(result, {"value": None})


if __name__ == "__main__":
    unittest.main()
