#!/usr/bin/env python3
"""Tests for shared core utilities."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.display import truncate
from core.normalize import finite_number
from core.prices import JupiterPriceClient
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


if __name__ == "__main__":
    unittest.main()
