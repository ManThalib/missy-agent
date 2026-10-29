#!/usr/bin/env python3
"""Regression tests for the position screener hardening changes."""

import base64
import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch

from screener_position_py import LiquidityPosition, enrichment
from screener_position_py.position_scanner import (
    ORCA_PROGRAM,
    RAYDIUM_PROGRAM,
    PositionScanner,
)
from screener_position_py.raydium_pending import _pda
from core.solana import b58decode

WALLET = "So11111111111111111111111111111111111111112"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NFT_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class _FakeRpc:
    def __init__(self, call_results=None, batch_results=None):
        self.call_results = call_results or {}
        self.batch_results = batch_results or []
        self.batch_calls = []

    def call(self, method, params):
        return self.call_results.get(method)

    def batch(self, calls):
        self.batch_calls.append(calls)
        return list(self.batch_results)


class TestOrcaPendingUsesSharedRpc(unittest.TestCase):
    def test_orca_refresh_reads_positions_in_one_batch_and_computes(self):
        account = {"value": {"data": [base64.b64encode(b"\x01" * 216).decode(), "base64"]}}
        rpc = _FakeRpc(batch_results=[account])
        scanner = PositionScanner(rpc=rpc, helius_api_key="")
        position = LiquidityPosition(
            dex="orca", position_address="PosA", pool_address="PoolA", status="active"
        )
        fake_out = {
            "fees_owed_raw": [5, 6],
            "rewards_owed_raw": [0, 0, 0],
            "reward_mints": ["", "", ""],
        }
        with patch(
            "screener_position_py.position_scanner.OrcaPendingFeesFetcher.compute_pending",
            return_value=fake_out,
        ) as compute:
            scanner._refresh_orca_pending([position])
        self.assertEqual(len(rpc.batch_calls), 1)
        self.assertEqual(compute.call_count, 1)
        self.assertEqual(position.fees_owed_raw, [5, 6])
        self.assertEqual(position.pending_fees_source, "computed")

    def test_orca_failure_keeps_checkpoint_and_records_error(self):
        rpc = _FakeRpc(batch_results=[{"value": None}])
        scanner = PositionScanner(rpc=rpc, helius_api_key="")
        position = LiquidityPosition(
            dex="orca", position_address="PosA", pool_address="PoolA", status="active",
            fees_owed_raw=[1, 2],
        )
        scanner._refresh_orca_pending([position])
        self.assertEqual(position.pending_fees_source, "raw_checkpoint")
        self.assertIn("not found", position.pool_enrichment["pending_fees_error"])
        self.assertEqual(position.fees_owed_raw, [1, 2])


class TestNftPositionDiscovery(unittest.TestCase):
    def test_position_accounts_are_read_at_derived_pda_addresses(self):
        scanner = PositionScanner(rpc=_FakeRpc(), helius_api_key="")
        captured = {}

        def fake_fetch(addresses):
            captured["addresses"] = list(addresses)
            return {address: None for address in addresses}

        with patch.object(scanner, "_fetch_account_bytes", side_effect=fake_fetch):
            scanner._fetch_nft_positions("raydium", [NFT_MINT])
        expected = _pda([b"position", b58decode(NFT_MINT)], b58decode(RAYDIUM_PROGRAM))
        self.assertEqual(captured["addresses"], [expected])

        with patch.object(scanner, "_fetch_account_bytes", side_effect=fake_fetch):
            scanner._fetch_nft_positions("orca", [NFT_MINT])
        expected_orca = _pda([b"position", b58decode(NFT_MINT)], b58decode(ORCA_PROGRAM))
        self.assertEqual(captured["addresses"], [expected_orca])

    def test_nft_mints_are_decimals_zero_amount_one(self):
        accounts = [
            {"account": {"data": {"parsed": {"info": {
                "mint": "NftMint", "tokenAmount": {"amount": "1", "decimals": 0}}}}}},
            {"account": {"data": {"parsed": {"info": {
                "mint": "Fungible", "tokenAmount": {"amount": "1", "decimals": 6}}}}}},
        ]
        self.assertEqual(PositionScanner._nft_mints(accounts), ["NftMint"])


class TestClosureStateSingleOwner(unittest.TestCase):
    def test_newly_closed_is_reported_once_across_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "closure.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({}, handle)  # existing state: not a first run
            closed = LiquidityPosition(
                dex="orca", position_address="PosX", status="closed", closed_at=1_700_000_000
            )
            kept, newly = PositionScanner._filter_closed_positions([closed], path)
            self.assertEqual(newly, {"orca:PosX"})
            self.assertEqual(len(kept), 1)

            kept_again, newly_again = PositionScanner._filter_closed_positions([closed], path)
            self.assertEqual(newly_again, set())
            self.assertEqual(kept_again, [])

    def test_first_run_omits_closed_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fresh.json")
            closed = LiquidityPosition(
                dex="orca", position_address="PosY", status="closed", closed_at=1
            )
            kept, newly = PositionScanner._filter_closed_positions([closed], path)
            self.assertEqual(kept, [])
            self.assertEqual(newly, set())


class TestTickDerivationFloors(unittest.TestCase):
    def test_negative_tick_rounds_toward_negative_infinity(self):
        pool = {"pool_price": 0.5, "token_x_decimals": 6, "token_y_decimals": 6}
        expected = math.floor(math.log(0.5) / math.log(1.0001))
        self.assertEqual(expected, -6932)
        self.assertEqual(enrichment._derive_current_tick(pool), -6932)


class TestPoolScanErrorsAreReported(unittest.TestCase):
    def test_unreadable_pool_scan_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "pool_scan-1.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            errors = {}
            position = LiquidityPosition(dex="orca", position_address="P", pool_address="Q")
            with patch.object(enrichment, "_latest_pool_scan", return_value=bad):
                enrichment.enrich_positions([position], errors)
            self.assertIn("pool_scan", errors)

    def test_missing_pool_scan_is_recorded(self):
        errors = {}
        position = LiquidityPosition(dex="orca", position_address="P", pool_address="Q")
        with patch.object(enrichment, "_latest_pool_scan", return_value=None):
            enrichment.enrich_positions([position], errors)
        self.assertIn("no pool scan", errors["pool_scan"])


class TestWalletValuationEndToEnd(unittest.TestCase):
    def test_scan_values_sol_and_token_balances(self):
        token_account = {"account": {"data": {"parsed": {"info": {
            "mint": USDC_MINT,
            "tokenAmount": {"amount": "2500000", "decimals": 6, "uiAmount": 2.5},
        }}}}}

        def call(method, params):
            if method == "getBalance":
                return {"value": 1_000_000_000}
            return None

        rpc = _FakeRpc(call_results={}, batch_results=[{"value": [token_account]}] * 3)
        rpc.call = call
        scanner = PositionScanner(rpc=rpc, helius_api_key="")
        prices = {SOL_MINT: 100.0, USDC_MINT: 1.0}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("screener_position_py.position_scanner.time.sleep"), \
                patch(
                    "screener_position_py.position_scanner.JupiterPriceClient.fetch_prices",
                    return_value=prices,
                ):
            result = scanner.scan(
                WALLET,
                dex="raydium",
                closure_state_path=os.path.join(tmp, "closure.json"),
            )
        self.assertEqual(result.sol_price_usd, 100.0)
        self.assertEqual(len(result.wallet_balances), 3)
        usdc = result.wallet_balances[0]
        self.assertEqual(usdc["symbol"], "USDC")
        self.assertAlmostEqual(usdc["usd_value"], 2.5)
        # 1 SOL at $100 plus three 2.5 USDC accounts at $1.
        self.assertAlmostEqual(result.wallet_total_usd, 100.0 + 2.5 * 3)


if __name__ == "__main__":
    unittest.main()
