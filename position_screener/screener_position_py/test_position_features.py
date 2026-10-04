"""Tests for position_features.py — versioned feature vector, gaps on missing."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener_position_py.liquidity_position import LiquidityPosition
from screener_position_py.position_features import FEATURES_VERSION, build_position_features


POOL = {
    "pair_class": "stable_bluechip",
    "realized_fee_apr": 91.3,
    "volatility": 1.48,
    "tvl": 123456.0,
    "bin_step": 10,
    "score": 78.5,
    "score_model": "missy",
    "score_version": 1,
}


def _pos(**kw):
    base = dict(
        dex="meteora",
        position_address="Pos1",
        pool_address="Pool1",
        wallet_id="main",
        status="active",
        lower_bound=100,
        upper_bound=200,
        current_bin_id=150,
        in_range=True,
        value_known=True,
        current_value_usd=288.5,
        fees_usd=0.044,
        rewards_usd=0.0,
        days_open=1.41,
        token_x_amount={"raw": "1", "ui": 1.27},
        token_y_amount={"raw": "1", "ui": 134.56},
        token_x_price_usd=121.0,
        token_y_price_usd=1.0,
        fees_owed_raw=[191282, 21043],
    )
    base.update(kw)
    return LiquidityPosition(**base)


class FeatureTests(unittest.TestCase):
    def test_centered_in_range_full_features(self):
        f = build_position_features(_pos(), POOL)
        self.assertEqual(f["version"], FEATURES_VERSION)
        self.assertEqual(f["pair_class"], "stable_bluechip")
        self.assertEqual(f["in_range"], True)
        self.assertEqual(f["range_width"], 100)
        self.assertEqual(f["bins_to_lower"], 50)
        self.assertEqual(f["bins_to_upper"], 50)
        self.assertAlmostEqual(f["edge_distance_frac"], 1.0)
        self.assertAlmostEqual(f["side_bias_x"], 1.27 * 121.0 / (1.27 * 121.0 + 134.56), places=6)
        self.assertEqual(f["single_sided"], False)
        self.assertEqual(f["value_usd"], 288.5)
        self.assertEqual(f["fees_usd"], 0.044)
        # fees_apr = 0.044 / 288.5 / 1.41 * 365 * 100
        self.assertAlmostEqual(f["fees_apr_pct"], 0.044 / 288.5 / 1.41 * 36500.0, places=6)
        self.assertEqual(f["pool_realized_fee_apr"], 91.3)
        self.assertEqual(f["pool_score"], 78.5)
        self.assertEqual(f["pool_score_version"], 1)
        self.assertEqual(f["gaps"], [])

    def test_out_of_range_edge_zero(self):
        f = build_position_features(_pos(current_bin_id=260, in_range=False), POOL)
        self.assertEqual(f["in_range"], False)
        self.assertEqual(f["bins_to_upper"], -60)
        self.assertAlmostEqual(f["edge_distance_frac"], 0.0)

    def test_unknown_amounts_value_gaps(self):
        pos = _pos(amounts_x_raw=None, amounts_y_raw=None, value_known=False,
                   current_value_usd=0.0,
                   token_x_amount={"raw": "0", "ui": 0.0},
                   token_y_amount={"raw": "0", "ui": 0.0})
        f = build_position_features(pos, POOL)
        self.assertIsNone(f["value_usd"])
        self.assertIsNone(f["side_bias_x"])
        self.assertIsNone(f["single_sided"])
        self.assertIn("value_unknown", f["gaps"])
        self.assertIn("side_unknown", f["gaps"])
        self.assertIn("fees_apr_unknown", f["gaps"])

    def test_fee_sentinel_is_unknown_not_zero(self):
        sentinel = (1 << 64) - 1
        f = build_position_features(_pos(fees_owed_raw=[sentinel, 5]), POOL)
        self.assertIsNone(f["fees_usd"])
        self.assertIn("fees_unknown", f["gaps"])
        # Sentinel raw with a stale fees_usd still wins: unknown, not zero.
        self.assertNotEqual(f["fees_usd"], 0.0)

    def test_missing_pool_record(self):
        f = build_position_features(_pos(), None)
        self.assertIn("pool_record_missing", f["gaps"])
        self.assertEqual(f["pair_class"], "unknown")
        self.assertIsNone(f["pool_tvl_usd"])
        # Position-local features still computed.
        self.assertAlmostEqual(f["edge_distance_frac"], 1.0)

    def test_degraded_position_no_bounds(self):
        f = build_position_features(_pos(lower_bound=None, upper_bound=None,
                                         current_bin_id=0, in_range=False), POOL)
        self.assertIsNone(f["edge_distance_frac"])
        self.assertIn("range_unknown", f["gaps"])

    def test_young_position_apr_gap(self):
        f = build_position_features(_pos(days_open=0.01), POOL)
        self.assertIsNone(f["fees_apr_pct"])
        self.assertIn("fees_apr_unknown", f["gaps"])

    def test_single_sided_detection(self):
        f = build_position_features(
            _pos(token_x_amount={"raw": "0", "ui": 0.0}), POOL)
        self.assertEqual(f["single_sided"], True)
        self.assertLess(f["side_bias_x"], 0.01)


if __name__ == "__main__":
    unittest.main()
