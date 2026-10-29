#!/usr/bin/env python3
"""Regression test for best-effort Meteora pending-fee refresh.

Incident: an HTTP 429 during pending-fee enrichment aborted the entire
position scan, so no fresh position_scan-*.json was published and Sheldon
rejected the cycle as stale. Enrichment failures must be recorded, not fatal.
"""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from screener_position_py.liquidity_position import LiquidityPosition
from screener_position_py.position_scanner import PositionScanner

class TestMeteoraPendingBestEffort(unittest.TestCase):
    def test_rpc_failure_keeps_checkpoint_and_records_error(self):
        scanner = PositionScanner.__new__(PositionScanner)
        scanner._fetch_account_bytes = Mock(
            side_effect=RuntimeError("RPC rate limited (HTTP 429)")
        )
        position = LiquidityPosition(
            dex="meteora",
            position_address="position-1",
            pool_address="pool-1",
            status="active",
            fees_owed_raw=[7, 8],
        )

        scanner._refresh_meteora_pending([position])

        self.assertEqual(position.fees_owed_raw, [7, 8])
        self.assertEqual(position.pending_fees_source, "raw_per_bin_sum")
        self.assertIn(
            "HTTP 429", position.pool_enrichment["meteora_pending_error"]
        )

    def test_inactive_positions_are_not_enriched(self):
        scanner = PositionScanner.__new__(PositionScanner)
        scanner._fetch_account_bytes = Mock()
        position = LiquidityPosition(
            dex="meteora", position_address="position-1", status="closed"
        )

        scanner._refresh_meteora_pending([position])

        scanner._fetch_account_bytes.assert_not_called()

if __name__ == "__main__":
    unittest.main()
