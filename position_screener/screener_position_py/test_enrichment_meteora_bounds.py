#!/usr/bin/env python3
"""Regression tests for Meteora bound-price scaling in enrichment.

Incident (scans 2026-10-11 23:44 / 2026-10-12 04:59 UTC+8, pool
3potGhhaNyzvCNq1CGh2wLbZLLMjpdPtG1Hpj8q2DDVR): the pool record shipped
active_bin_id=None (discovery API omitted it, RPC fallbacks failed)
while pool_price stayed correct. Enrichment coerced the unknown bin to 0
and scaled the position bounds off bin 0, emitting lower/upper_price
~228x the live price (9.97/10.07 vs 0.0438) with the bound ratio intact.

The fix resolves the active bin against the same record's pool_price_raw
and refuses to anchor on a fabricated bin 0.
"""

import math
import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from screener_position_py.enrichment import (
    _ACTIVE_BIN_DRIFT_BINS,
    _enrich_one,
    _meteora_price_implied_bin,
    _resolve_meteora_active_bin,
)
from screener_position_py.liquidity_position import LiquidityPosition

# Real fields from pool_scan-20261011-224315.json (healthy) and
# pool_scan-20261011-234314.json (incident): bin_step 4, decimals 8/9.
HEALTHY_POOL = {
    "pool_address": "3potGhhaNyzvCNq1CGh2wLbZLLMjpdPtG1Hpj8q2DDVR",
    "dex": "meteora",
    "bin_step": 4,
    "active_bin_id": 13583,
    "pool_price": 0.043738412574955685,
    "pool_price_raw": 22.863198299350103,
    "token_x_decimals": 8,
    "token_y_decimals": 9,
    "token_x_price_usd": 2505.8039421080002,
    "token_y_price_usd": 109.53804982810057,
}
INCIDENT_POOL = dict(
    HEALTHY_POOL,
    active_bin_id=None,  # the anomaly: unknown, emitted as JSON null
    pool_price=0.04382595940956388,
    pool_price_raw=22.81752672325471,
    token_x_price_usd=2526.977114847459,
    token_y_price_usd=110.63002233388143,
)
# Real bounds of position 3YjJgeybs6ScLLTGt5raqYuckFw42Rje4JvaQMkT1mvt.
BOUNDS = {"lower_bound": 13570, "upper_bound": 13596}


def _position(**kw):
    base = dict(
        dex="meteora",
        position_address="3YjJgeybs6ScLLTGt5raqYuckFw42Rje4JvaQMkT1mvt",
        pool_address=HEALTHY_POOL["pool_address"],
        status="active",
    )
    base.update(BOUNDS)
    base.update(kw)
    return LiquidityPosition(**base)


class ImpliedBinTests(unittest.TestCase):
    def test_implied_bin_matches_recorded_on_healthy_record(self):
        # Live healthy record: recorded 13583, price-implied 13583.4 -> 13583.
        self.assertEqual(
            _meteora_price_implied_bin(HEALTHY_POOL, 4), 13583
        )

    def test_implied_bin_on_incident_record(self):
        # Incident record has no recorded bin; the price implies 13578.
        self.assertEqual(
            _meteora_price_implied_bin(INCIDENT_POOL, 4), 13578
        )

    def test_implied_bin_needs_price_and_decimals(self):
        self.assertIsNone(_meteora_price_implied_bin({}, 4))
        self.assertIsNone(
            _meteora_price_implied_bin(dict(HEALTHY_POOL, pool_price_raw=0.0), 4)
        )
        self.assertIsNone(
            _meteora_price_implied_bin(dict(HEALTHY_POOL, bin_step=0), 0)
        )


class ResolveBinTests(unittest.TestCase):
    def test_missing_bin_recovered_from_price_raw(self):
        active, source = _resolve_meteora_active_bin(INCIDENT_POOL, 4)
        self.assertEqual(active, 13578)
        self.assertEqual(source, "derived_from_price_raw")

    def test_recorded_bin_consistent_is_kept(self):
        active, source = _resolve_meteora_active_bin(HEALTHY_POOL, 4)
        self.assertEqual(active, 13583)
        self.assertEqual(source, "recorded")

    def test_recorded_bin_beyond_drift_is_replaced(self):
        poisoned = dict(HEALTHY_POOL, active_bin_id=13583 - 5000)
        active, source = _resolve_meteora_active_bin(poisoned, 4)
        self.assertEqual(active, 13583)
        self.assertIn("derived_from_price_raw", source)
        self.assertIn("13583", source)

    def test_bin_zero_is_legitimate_when_consistent(self):
        # raw = (1+bs/1e4)^0 * 10^(dec_y-dec_x) => implied bin 0. A real
        # bin 0 must not be confused with the unknown sentinel.
        pool = dict(
            HEALTHY_POOL,
            active_bin_id=0,
            pool_price_raw=0.1,  # 10^(8-9)
            pool_price=10.0,
        )
        active, source = _resolve_meteora_active_bin(pool, 4)
        self.assertEqual(active, 0)
        self.assertEqual(source, "recorded")

    def test_drift_tolerance_boundary_is_honored(self):
        edge = dict(HEALTHY_POOL, active_bin_id=13583 + _ACTIVE_BIN_DRIFT_BINS)
        self.assertEqual(_resolve_meteora_active_bin(edge, 4)[1], "recorded")
        beyond = dict(
            HEALTHY_POOL, active_bin_id=13583 + _ACTIVE_BIN_DRIFT_BINS + 1
        )
        self.assertNotEqual(
            _resolve_meteora_active_bin(beyond, 4)[1], "recorded"
        )


class EnrichScalingTests(unittest.TestCase):
    def _enriched(self, pool):
        position = _position()
        _enrich_one(position, {pool["pool_address"]: pool}, now=0.0)
        return position

    def test_incident_bounds_stay_on_price_scale(self):
        """The 228x regression: unknown bin must never anchor on bin 0."""
        position = self._enriched(INCIDENT_POOL)
        self.assertEqual(position.current_bin_id, 13578)
        self.assertEqual(position.current_price, INCIDENT_POOL["pool_price"])
        log_bin = math.log(1.0004)
        expected_lower = position.current_price * math.exp((13570 - 13578) * log_bin)
        expected_upper = position.current_price * math.exp((13596 - 13578) * log_bin)
        self.assertAlmostEqual(position.lower_price, expected_lower, places=14)
        self.assertAlmostEqual(position.upper_price, expected_upper, places=14)
        self.assertLess(position.lower_price, position.current_price)
        self.assertLess(position.current_price, position.upper_price)
        # Old bug: bounds landed at ~9.97/10.07 (228x). Never again.
        self.assertLess(position.upper_price, 0.05)
        self.assertTrue(position.in_range)
        self.assertEqual(
            position.pool_enrichment["active_bin_source"],
            "derived_from_price_raw",
        )

    def test_healthy_record_output_unchanged(self):
        # Pin the pre-fix healthy behavior bit-for-bit (matches the live
        # row stored in position_scan-20261011-224502.json).
        position = self._enriched(HEALTHY_POOL)
        self.assertEqual(position.current_bin_id, 13583)
        self.assertAlmostEqual(
            position.lower_price, 0.04351160838922555, places=15
        )
        self.assertAlmostEqual(
            position.upper_price, 0.04396639897712343, places=15
        )
        self.assertTrue(position.in_range)
        self.assertNotIn("active_bin_source", position.pool_enrichment or {})

    def test_poisoned_recorded_bin_is_repaired(self):
        position = self._enriched(dict(HEALTHY_POOL, active_bin_id=13583 - 5000))
        self.assertEqual(position.current_bin_id, 13583)
        self.assertAlmostEqual(
            position.lower_price, 0.04351160838922555, places=15
        )
        self.assertTrue(position.in_range)

    def test_bin_zero_scales_from_zero_when_consistent(self):
        pool = dict(
            HEALTHY_POOL,
            active_bin_id=0,
            pool_price_raw=0.1,
            pool_price=10.0,
        )
        position = self._enriched(pool)
        self.assertEqual(position.current_bin_id, 0)
        self.assertEqual(position.current_price, 10.0)
        # Bounds anchor on the real bin 0 (position sits far above range).
        self.assertAlmostEqual(
            position.lower_price, 10.0 * 1.0004 ** 13570, places=8
        )
        self.assertAlmostEqual(
            position.upper_price, 10.0 * 1.0004 ** 13596, places=8
        )
        self.assertFalse(position.in_range)

    def test_unrecoverable_bin_degrades_to_unknown_not_bin_zero(self):
        pool = dict(
            INCIDENT_POOL, pool_price=0.0, pool_price_raw=0.0,
            token_x_price_usd=0.0, token_y_price_usd=0.0,
        )
        position = self._enriched(pool)
        self.assertEqual(position.current_price, 0.0)
        self.assertEqual(position.lower_price, 0.0)
        self.assertEqual(position.upper_price, 0.0)
        self.assertFalse(position.in_range)

    def test_no_price_but_recorded_bin_keeps_relative_bin_ratios(self):
        pool = dict(HEALTHY_POOL, pool_price=0.0, pool_price_raw=0.0)
        position = self._enriched(pool)
        self.assertAlmostEqual(position.current_price, 1.0004 ** 13583, places=8)
        self.assertAlmostEqual(position.lower_price, 1.0004 ** 13570, places=8)
        self.assertAlmostEqual(position.upper_price, 1.0004 ** 13596, places=8)


if __name__ == "__main__":
    unittest.main()
