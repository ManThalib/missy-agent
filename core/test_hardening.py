#!/usr/bin/env python3
"""Regression tests for the shared-core hardening changes."""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.cache import DiskCache, cache_ttl_from_env
from core.constants import KNOWN_MINT_SYMBOLS, WBTC_MINT
from core.http_client import HttpJsonClient, HttpStatusError, redact_url
from core.retry import backoff_delay, call_with_retry, is_transient
from core.rpc import RpcClient, RpcRateLimited, RpcRequestError
from core.solana import is_valid_solana_address
from core.wallet_balances import parse_token_account, ui_amount


class TestRetryClassification(unittest.TestCase):
    def test_transient_statuses_and_transport_errors(self):
        self.assertTrue(is_transient(HttpStatusError("http://x", 503)))
        self.assertTrue(is_transient(HttpStatusError("http://x", 429)))
        self.assertFalse(is_transient(HttpStatusError("http://x", 404)))
        self.assertTrue(is_transient(socket.timeout("timed out")))
        self.assertTrue(is_transient(ConnectionResetError()))
        self.assertTrue(is_transient(RpcRequestError("net", transient=True)))
        self.assertFalse(is_transient(RpcRequestError("bad json")))
        self.assertFalse(is_transient(ValueError("nope")))

    def test_call_with_retry_recovers_then_stops_on_permanent_error(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise socket.timeout("slow")
            return "ok"

        with patch("core.retry.time.sleep") as sleep:
            self.assertEqual(call_with_retry(flaky, attempts=3, base_seconds=0.01), "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep.call_count, 2)

        permanent = Mock(side_effect=ValueError("bad payload"))
        with self.assertRaises(ValueError):
            call_with_retry(permanent, attempts=5)
        self.assertEqual(permanent.call_count, 1)

    def test_retry_after_is_honored_and_capped(self):
        self.assertGreaterEqual(backoff_delay(0, 1.0, retry_after=10.0), 10.0)
        self.assertLessEqual(backoff_delay(0, 0.1, retry_after=999.0), 30.0 * 1.25)


class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = DiskCache(directory=self._tmp.name)

    def test_part_boundaries_do_not_collide(self):
        self.cache.set("ns", "a|b", "c", value={"v": 1})
        self.assertIsNone(self.cache.get("ns", "a", "b|c"))
        self.assertEqual(self.cache.get("ns", "a|b", "c"), {"v": 1})

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        self.cache.set("ns", "k", value={"x": 2}, ttl_seconds=60)
        self.assertEqual(self.cache.get("ns", "k"), {"x": 2})
        leftovers = [name for name in os.listdir(self._tmp.name) if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_expired_entries_are_dropped(self):
        self.cache.set("ns", "k", value=1, ttl_seconds=-1)
        self.assertIsNone(self.cache.get("ns", "k"))

    def test_ttl_env_override(self):
        with patch.dict(os.environ, {"MISSY_CACHE_TTL_SECONDS": "42"}):
            self.assertEqual(cache_ttl_from_env(), 42.0)
        with patch.dict(os.environ, {"MISSY_CACHE_TTL_SECONDS": "bogus"}):
            self.assertEqual(cache_ttl_from_env(), 300.0)


class TestRedaction(unittest.TestCase):
    def test_api_key_is_masked_and_other_params_kept(self):
        url = "https://h.example/rpc?api-key=SECRET123&page=2"
        cleaned = redact_url(url)
        self.assertNotIn("SECRET123", cleaned)
        self.assertIn("page=2", cleaned)
        self.assertIn("api-key=%2A%2A%2A", cleaned)


class _Handler(BaseHTTPRequestHandler):
    """Serves a scripted list of (status, headers, body) responses."""

    script = []
    seen = []

    def do_GET(self):
        type(self).seen.append(self.path)
        status, headers, body = type(self).script.pop(0)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestHttpJsonClient(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/pools"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _Handler.seen = []
        _Handler.script = []
        self.sleeps = []
        patcher = patch("time.sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_429_honoring_retry_after_then_succeeds(self):
        ok = json.dumps({"success": True, "data": []}).encode()
        _Handler.script = [
            (429, {"Retry-After": "2"}, b"slow down"),
            (200, {"Content-Type": "application/json"}, ok),
        ]
        client = HttpJsonClient(self.url)
        self.assertEqual(client._get_json(self.url), {"success": True, "data": []})
        self.assertEqual(len(_Handler.seen), 2)
        self.assertGreaterEqual(self.sleeps[0], 2.0)
        client.close()

    def test_client_error_is_not_retried(self):
        _Handler.script = [(404, {}, b"nope")]
        client = HttpJsonClient(self.url)
        with self.assertRaises(RuntimeError) as ctx:
            client._get_json(self.url)
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertEqual(len(_Handler.seen), 1)
        client.close()

    def test_connection_is_reused_across_requests(self):
        ok = json.dumps({"success": True, "data": []}).encode()
        _Handler.script = [(200, {}, ok), (200, {}, ok)]
        client = HttpJsonClient(self.url)
        client._get_json(self.url)
        first = client._connections.copy()
        client._get_json(self.url + "?page=2")
        self.assertEqual(first.keys(), client._connections.keys())
        self.assertEqual(len(_Handler.seen), 2)
        client.close()

    def test_cache_ttl_is_the_clients_configured_ttl(self):
        ok = json.dumps({"success": True, "data": []}).encode()
        _Handler.script = [(200, {}, ok)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = HttpJsonClient(self.url)
        client.cache = DiskCache(directory=tmp.name)
        client.cache_ttl_seconds = 7.0
        before = time.time()
        client._get_json(self.url)
        entry_path = next(p for p in client.cache.directory.iterdir() if p.name.startswith("http_"))
        expires = json.loads(entry_path.read_text())["expires"]
        self.assertLess(expires, before + 8.0)
        client.close()


class TestRpcBatchPartial(unittest.TestCase):
    def test_per_item_errors_do_not_fail_the_batch(self):
        rpc = RpcClient("https://rpc.example")
        body = [
            {"id": 1, "result": {"value": None}},
            {"id": 2, "error": {"code": -32602, "message": "invalid"}},
        ]
        with patch.object(rpc, "_post", return_value=body):
            results = rpc.batch_partial([("getAccountInfo", ["a"]), ("getAccountInfo", ["b"])])
        self.assertEqual(results[0], ({"value": None}, None))
        self.assertIsNone(results[1][0])
        self.assertIn("invalid", results[1][1])

    def test_missing_response_item_is_reported(self):
        rpc = RpcClient("https://rpc.example")
        with patch.object(rpc, "_post", return_value=[{"id": 1, "result": 5}]):
            results = rpc.batch_partial([("getBalance", ["a"]), ("getBalance", ["b"])])
        self.assertEqual(results[0], (5, None))
        self.assertIn("missing", results[1][1])

    def test_transport_timeout_is_transient_and_429_is_rate_limited(self):
        rpc = RpcClient("https://rpc.example")
        with patch("core.rpc.urllib.request.urlopen", side_effect=socket.timeout("slow")):
            with self.assertRaises(RpcRequestError) as ctx:
                rpc.call("getBalance", ["a"])
        self.assertTrue(ctx.exception.transient)

        limited = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "3"}, None)
        with patch("core.rpc.urllib.request.urlopen", side_effect=limited):
            with self.assertRaises(RpcRateLimited) as ctx:
                rpc.call("getBalance", ["a"])
        self.assertEqual(ctx.exception.retry_after, 3.0)


class TestWalletBalances(unittest.TestCase):
    def test_ui_amount_keeps_precision_above_float_range(self):
        # 2**60 + 1 is not representable as a float; the decimal path keeps it.
        raw = str(2 ** 60 + 1)
        self.assertAlmostEqual(ui_amount(raw, 9), (2 ** 60 + 1) / 1e9, places=3)
        self.assertEqual(ui_amount("garbage", 6), 0.0)

    def test_parse_jsonparsed_account(self):
        account = {
            "pubkey": "acct",
            "account": {"data": {"parsed": {"info": {
                "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "tokenAmount": {"amount": "2500000", "decimals": 6, "uiAmount": 2.5},
            }}}},
        }
        parsed = parse_token_account(account)
        self.assertEqual(parsed["symbol"], "USDC")
        self.assertEqual(parsed["ui_amount"], 2.5)
        self.assertEqual(parsed["raw_amount"], "2500000")

    def test_raw_entry_without_info_is_skipped(self):
        self.assertIsNone(parse_token_account({"pubkey": "x"}))


class TestMintConstants(unittest.TestCase):
    def test_every_known_mint_is_a_valid_address(self):
        for mint in KNOWN_MINT_SYMBOLS:
            self.assertTrue(is_valid_solana_address(mint), mint)

    def test_wbtc_mint_matches_the_canonical_address(self):
        self.assertEqual(KNOWN_MINT_SYMBOLS[WBTC_MINT], "WBTC")
        self.assertEqual(len(WBTC_MINT), 44)


if __name__ == "__main__":
    unittest.main()
