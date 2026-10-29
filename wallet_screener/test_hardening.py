#!/usr/bin/env python3
"""Regression tests for the wallet screener hardening changes."""

import argparse
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)

from core.rpc import RpcRequestError
from wallet_screener import main as wallet_main
from wallet_screener.rpc_client import SolanaRpcClient
from wallet_screener.wallet_screener import WalletScanner

VALID_WALLET = "So11111111111111111111111111111111111111112"


class TestScanOutput(unittest.TestCase):
    def test_threshold_is_reported_in_scan_output(self):
        scanner = WalletScanner(
            rpc_client=Mock(get_balance_lamports=Mock(return_value=0), get_token_accounts=Mock(return_value=[])),
            price_fetcher=Mock(fetch_prices=Mock(return_value={})),
            threshold_usd=0.25,
        )
        result = scanner.scan(VALID_WALLET)
        self.assertEqual(result["threshold_usd"], 0.25)


class TestOutputFileFailureIsNotFatal(unittest.TestCase):
    def test_unwritable_output_dir_returns_none_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "not-a-dir")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            args = argparse.Namespace(no_output_file=False, output_dir=os.path.join(blocker, "sub"))
            with patch("sys.stderr"):
                self.assertIsNone(wallet_main._write_output_file(args, {"assets": []}))

    def test_writes_file_when_possible(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(no_output_file=False, output_dir=tmp)
            path = wallet_main._write_output_file(args, {"assets": []})
            self.assertTrue(path and os.path.exists(path))


class TestRpcRetryClassification(unittest.TestCase):
    def test_transient_failure_is_retried_then_succeeds(self):
        rpc = SolanaRpcClient("https://rpc.example", max_retries=3, backoff_seconds=0.0)
        attempts = {"n": 0}

        def op(_rpc, _wallet):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RpcRequestError("timeout", transient=True)
            return 42

        with patch("wallet_screener.rpc_client.fetch_sol_balance_lamports", side_effect=op), \
                patch("core.retry.time.sleep"):
            self.assertEqual(rpc.get_balance_lamports(VALID_WALLET), 42)
        self.assertEqual(attempts["n"], 2)

    def test_permanent_failure_is_not_retried(self):
        rpc = SolanaRpcClient("https://rpc.example", max_retries=3, backoff_seconds=0.0)
        calls = {"n": 0}

        def op(_rpc, _wallet):
            calls["n"] += 1
            raise RuntimeError("invalid params")

        with patch("wallet_screener.rpc_client.fetch_sol_balance_lamports", side_effect=op):
            with self.assertRaises(RuntimeError):
                rpc.get_balance_lamports(VALID_WALLET)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
