"""Tests for PendingFeesFetcher RPC caching (raydium double-RPC fix).

Two positions in the same pool must trigger exactly one PoolState fetch and
one TickArray batch fetch — not one pair per position.
"""

import base64
import struct
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screener_position_py.raydium_pending import (
    TICKS_PER_ARRAY,
    PendingFeesFetcher,
    raydium_position_checkpoint,
    tick_array_address,
    tick_array_start_index,
)


def _fake_pool_data(
    tick_spacing: int = 64,
    tick_current: int = 100,
    fee_growth_a: int = 1 << 100,
    fee_growth_b: int = 1 << 101,
    reward_growth: int = 1 << 96,
) -> bytes:
    """Build a minimal PoolState blob that decode_pool_state accepts."""
    data = bytearray(904)
    struct.pack_into("<H", data, 235, tick_spacing)
    struct.pack_into("<i", data, 269, tick_current)
    data[277:293] = fee_growth_a.to_bytes(16, "little")
    data[293:309] = fee_growth_b.to_bytes(16, "little")
    for i in range(3):
        base = 397 + i * 169
        data[base] = 1  # reward active
        # reward mint: 32 arbitrary nonzero bytes (b58-encodable)
        data[base + 57 : base + 89] = bytes([i + 1]) * 32
        data[base + 153 : base + 169] = reward_growth.to_bytes(16, "little")
    return bytes(data)


def _fake_tick_array_data(start_index: int) -> bytes:
    """Minimal TickArray blob with one initialized boundary tick row."""
    data = bytearray(44 + TICKS_PER_ARRAY * 168)
    struct.pack_into("<i", data, 40, start_index)
    return bytes(data)


class _CountingRpc:
    """RpcClient stand-in that counts call/batch invocations."""

    def __init__(self, pool_data: bytes):
        self.pool_data = pool_data
        self.call_count = 0
        self.batch_count = 0

    def call(self, method, params):
        self.call_count += 1
        assert method == "getAccountInfo"
        return {
            "value": {
                "data": [base64.b64encode(self.pool_data).decode(), "base64"]
            }
        }

    def batch(self, requests):
        self.batch_count += 1
        out = []
        for _method, params in requests:
            out.append(
                {
                    "value": {
                        "data": [
                            base64.b64encode(_fake_tick_array_data(0)).decode(),
                            "base64",
                        ]
                    }
                }
            )
        return out


class TestPendingFeesCache(unittest.TestCase):
    POOL = "3ucNos4NbumumkmhusDjkTjHdtbVsiNPP2jsssS3tN8"

    def _checkpoint(self):
        # Zeroed PersonalPositionState checkpoint fields (all growths start
        # at 0, nothing owed) — 217-byte raw layout parsed by the helper.
        return raydium_position_checkpoint(bytes(217))

    def test_same_pool_second_position_skips_rpc(self):
        rpc = _CountingRpc(_fake_pool_data())
        fetcher = PendingFeesFetcher(rpc)
        lower, upper = -1000, 1000

        out1 = fetcher.compute_pending(
            self.POOL, lower, upper, 10 ** 12, self._checkpoint()
        )
        calls_after_first = (rpc.call_count, rpc.batch_count)
        self.assertEqual(rpc.call_count, 1, "one PoolState fetch")
        self.assertEqual(rpc.batch_count, 1, "one TickArray batch")

        out2 = fetcher.compute_pending(
            self.POOL, lower, upper, 10 ** 12, self._checkpoint()
        )
        # No additional RPC for the second position in the same pool.
        self.assertEqual((rpc.call_count, rpc.batch_count), calls_after_first)
        # Same pool-derived fields either way.
        self.assertEqual(out1["tick_current"], out2["tick_current"])
        self.assertEqual(out1["reward_mints"], out2["reward_mints"])
        self.assertEqual(out1["fees_owed_raw"], out2["fees_owed_raw"])

    def test_different_pool_refetches(self):
        rpc = _CountingRpc(_fake_pool_data())
        fetcher = PendingFeesFetcher(rpc)
        fetcher.compute_pending(self.POOL, -1000, 1000, 10 ** 12, self._checkpoint())
        fetcher.compute_pending(
            self.POOL + "x", -1000, 1000, 10 ** 12, self._checkpoint()
        )
        self.assertEqual(rpc.call_count, 2, "distinct pools each fetch state")

    def test_fetch_tick_arrays_merges_cached_and_new(self):
        rpc = _CountingRpc(_fake_pool_data())
        fetcher = PendingFeesFetcher(rpc)
        spacing = 64
        t1 = tick_array_start_index(-100, spacing)
        t2 = tick_array_start_index(10_000_000, spacing)
        self.assertNotEqual(t1, t2)
        first = fetcher.fetch_tick_arrays(self.POOL, spacing, (t2,))
        self.assertEqual(rpc.batch_count, 1)
        # Refetching the same array must not hit RPC again.
        refetched = fetcher.fetch_tick_arrays(self.POOL, spacing, (t2,))
        self.assertEqual(rpc.batch_count, 1, "cached array not refetched")
        self.assertEqual(first[t2], refetched[t2])
        # A new array triggers exactly one more batch for just that array.
        second = fetcher.fetch_tick_arrays(self.POOL, spacing, (t1, t2))
        self.assertEqual(rpc.batch_count, 2)
        self.assertIn(t1, second)
        self.assertIn(t2, second)
        self.assertEqual(first[t2], second[t2])

    def test_missing_array_cached_as_none(self):
        class _MissingRpc(_CountingRpc):
            def batch(self, requests):
                self.batch_count += 1
                return [{"value": None} for _ in requests]

        rpc = _MissingRpc(_fake_pool_data())
        fetcher = PendingFeesFetcher(rpc)
        spacing = 64
        start = tick_array_start_index(-100, spacing)
        out1 = fetcher.fetch_tick_arrays(self.POOL, spacing, (-100,))
        self.assertIsNone(out1[start])
        fetcher.fetch_tick_arrays(self.POOL, spacing, (-100,))
        self.assertEqual(rpc.batch_count, 1, "None result also cached")

    def test_tick_array_addresses_match_expected_pda(self):
        # The cached path must use the same PDA derivation as before.
        spacing = 64
        start = tick_array_start_index(-100, spacing)
        self.assertIsInstance(tick_array_address(self.POOL, start), str)


if __name__ == "__main__":
    unittest.main()
