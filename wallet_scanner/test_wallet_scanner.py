#!/usr/bin/env python3
"""Tests for the wallet scanner."""

import sys
import unittest
from unittest.mock import Mock

if __package__ in (None, ""):
    import os

    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)

from core.prices import JupiterPriceClient

VALID_WALLET = "So11111111111111111111111111111111111111112"
from wallet_scanner.balance_calculator import BalanceCalculator
from wallet_scanner.models import TokenBalance
from wallet_scanner.price_fetcher import TokenPriceFetcher
from wallet_scanner.wallet_scanner import WalletScanner


class TestBalanceCalculator(unittest.TestCase):
    def test_calculate_value(self):
        calc = BalanceCalculator()
        self.assertAlmostEqual(calc.calculate(10.0, 2.5), 25.0)

    def test_calculate_zero_amount_returns_zero(self):
        calc = BalanceCalculator()
        self.assertEqual(calc.calculate(0.0, 2.5), 0.0)

    def test_filter_assets_above_threshold(self):
        calc = BalanceCalculator(threshold_usd=0.10)
        balances = [
            TokenBalance(mint="a", amount_ui=1.0, price_usd=0.05, total_value_usd=0.05),
            TokenBalance(mint="b", amount_ui=2.0, price_usd=1.0, total_value_usd=2.0),
        ]
        result = calc.filter_assets(balances)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].mint, "b")

    def test_enrich_applies_prices(self):
        calc = BalanceCalculator()
        balances = [
            TokenBalance(mint="a", amount_ui=10.0),
            TokenBalance(mint="b", amount_ui=5.0),
        ]
        prices = {"a": 2.0, "b": 0.0}
        enriched = calc.enrich(balances, prices)
        self.assertAlmostEqual(enriched[0].total_value_usd, 20.0)
        self.assertAlmostEqual(enriched[1].total_value_usd, 0.0)


class TestTokenPriceFetcher(unittest.TestCase):
    def test_parse_prices(self):
        data = {
            "data": {
                "mint-a": {"price": "1.5"},
                "mint-b": {"price": "0.0"},
                "mint-c": {"price": "-1"},
                "mint-d": {"price": "invalid"},
            }
        }
        prices = JupiterPriceClient._parse_prices(data)
        self.assertEqual(prices, {"mint-a": 1.5})

    def test_empty_payload_returns_empty(self):
        self.assertEqual(JupiterPriceClient._parse_prices({"data": {}}), {})
        self.assertEqual(JupiterPriceClient._parse_prices({}), {})

    def test_price_fetcher_delegates_to_jupiter_client(self):
        fetcher = TokenPriceFetcher()
        fetcher._client.fetch_prices = Mock(return_value={"mint-a": 2.0})
        self.assertEqual(fetcher.fetch_prices(["mint-a"]), {"mint-a": 2.0})


class TestWalletScanner(unittest.TestCase):
    def _scanner(self, prices: dict, lamports: int, accounts: list):
        rpc = Mock(
            get_balance_lamports=Mock(return_value=lamports),
            get_token_accounts=Mock(return_value=accounts),
        )
        price_fetcher = Mock(fetch_prices=Mock(return_value=prices))
        return WalletScanner(
            rpc_client=rpc,
            price_fetcher=price_fetcher,
            threshold_usd=0.10,
        )

    def test_scan_returns_sorted_assets(self):
        scanner = self._scanner(
            prices={
                "So11111111111111111111111111111111111111112": 20.0,
                "usdc-mint": 1.0,
            },
            lamports=2_000_000_000,
            accounts=[
                {
                    "mint": "usdc-mint",
                    "token_account": "acct-1",
                    "owner": "wallet",
                    "decimals": 6,
                    "raw_amount": "500000000",
                    "ui_amount": 500.0,
                }
            ],
        )
        result = scanner.scan(VALID_WALLET)
        self.assertEqual(result["asset_count"], 2)
        self.assertGreaterEqual(
            result["assets"][0]["total_value_usd"],
            result["assets"][1]["total_value_usd"],
        )
        self.assertTrue(
            any(asset["is_native_sol"] for asset in result["assets"])
        )
        self.assertAlmostEqual(result["total_usd"], 540.0)

    def test_scan_filters_dust(self):
        scanner = self._scanner(
            prices={"dust-mint": 0.00001},
            lamports=0,
            accounts=[
                {
                    "mint": "dust-mint",
                    "token_account": "acct-2",
                    "owner": "wallet",
                    "decimals": 6,
                    "raw_amount": "100",
                    "ui_amount": 0.0001,
                }
            ],
        )
        result = scanner.scan(VALID_WALLET)
        self.assertEqual(result["asset_count"], 0)
        self.assertEqual(result["total_usd"], 0.0)

    def test_scan_rejects_invalid_wallet(self):
        scanner = self._scanner(prices={}, lamports=0, accounts=[])
        with self.assertRaisesRegex(ValueError, "32-byte"):
            scanner.scan("not-a-wallet")


if __name__ == "__main__":
    unittest.main()
