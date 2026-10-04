#!/usr/bin/env python3
"""Tests for Meteora DLMM per-bin token-amount math (meteora_pending).

Covers the SDK processPosition amounts loop: positionXAmount =
posShare * bin.xAmount / binSupply (BN truncating div), zero-supply
bins skipped, missing bin arrays contribute nothing, and negative
bin ids resolve across bin-array boundaries.
"""

import os
import struct
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from screener_position_py.meteora_pending import (
    SYSTEM_PROGRAM,
    LbPairState,
    _address_resolver,
    _trunc_div,
    bin_row_from_array,
    compute_fees_and_rewards,
    decode_position,
)

# Valid 32-byte base58 address (the live SOL-USDC DLMM pool); only used
# for PDA derivation, never fetched.
POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"

_BINS_BASE = 56
_BIN_STRIDE = 144
_SHARES_BASE = 72


def _bin_array_bytes(index: int, rows: dict) -> bytes:
    """Build a BinArray account image: rows maps bin_id -> (ax, ay, supply)."""
    data = bytearray(_BINS_BASE + 70 * _BIN_STRIDE)
    struct.pack_into("<q", data, 8, index)
    lower = index * 70
    for bin_id, (ax, ay, supply) in rows.items():
        off = _BINS_BASE + (bin_id - lower) * _BIN_STRIDE
        struct.pack_into("<Q", data, off, ax)
        struct.pack_into("<Q", data, off + 8, ay)
        struct.pack_into("<Q", data, off + 32, supply)
        struct.pack_into("<Q", data, off + 40, supply >> 64)
    return bytes(data)


def _position_bytes(lower: int, upper: int, shares: list) -> bytes:
    """Build a PositionV2 image with the given per-relative-bin shares."""
    size = 8 + 72 + 70 * 16 + 70 * 48 + 70 * 48 + 8
    data = bytearray(size)
    for i, share in enumerate(shares):
        struct.pack_into("<Q", data, _SHARES_BASE + i * 16, share)
        struct.pack_into("<Q", data, _SHARES_BASE + i * 16 + 8, share >> 64)
    bounds = 72 + 70 * 16 + 70 * 48 + 70 * 48
    struct.pack_into("<i", data, bounds, lower)
    struct.pack_into("<i", data, bounds + 4, upper)
    return bytes(data)


def _pair() -> LbPairState:
    return LbPairState(
        active_id=0,
        bin_step=10,
        function_type=1,
        reward_mints=[SYSTEM_PROGRAM, SYSTEM_PROGRAM],
        reward_rates=[0, 0],
        reward_duration_ends=[0, 0],
        reward_last_update_times=[0, 0],
    )


def _resolve(address_for, arrays: dict):
    """Wrap a resolver so it reads from the supplied {addr: bytes} map."""
    return lambda bin_id: address_for(bin_id)


class MeteoraAmountsTest(unittest.TestCase):
    def _compute(self, lower, upper, shares, arrays_by_index):
        data = _position_bytes(lower, upper, shares)
        position = decode_position(data)
        self.assertEqual(position.lower_bin_id, lower)
        self.assertEqual(position.upper_bin_id, upper)
        self.assertEqual(position.liquidity_shares[: len(shares)], shares)
        address_for = _address_resolver(POOL, lower, upper)
        arrays = {
            addr: arrays_by_index[idx]
            for idx, addr in _bin_array_addresses(POOL, lower, upper)
            if idx in arrays_by_index
        }
        return compute_fees_and_rewards(_pair(), position, arrays, address_for, 0)

    def test_in_range_split_across_bins(self):
        # Bins -2..2 (array -1 for negatives, array 0 for 0..2).
        shares = [10, 20, 30, 40, 50]  # relative to lower=-2
        rows = {
            -2: (100, 0, 10),   # all x, supply == share*10
            -1: (0, 200, 20),   # all y
            0: (300, 400, 30),
            1: (0, 0, 5),       # zero supply: skipped
            2: (500, 600, 50),
        }
        arrays = {-1: _bin_array_bytes(-1, {k: v for k, v in rows.items() if k < 0}),
                  0: _bin_array_bytes(0, {k: v for k, v in rows.items() if k >= 0})}
        out = self._compute(-2, 2, shares, arrays)
        ax = out["amount_x_raw"]
        ay = out["amount_y_raw"]
        expected_x = (
            _trunc_div(10 * 100, 10)
            + _trunc_div(30 * 300, 30)
            + _trunc_div(50 * 500, 50)
        )
        expected_y = (
            _trunc_div(20 * 200, 20)
            + _trunc_div(30 * 400, 30)
            + _trunc_div(50 * 600, 50)
        )
        self.assertEqual(ax, expected_x)
        self.assertEqual(ay, expected_y)

    def test_truncating_division_per_bin(self):
        # share=3, reserve=10, supply=4 -> trunc(30/4)=7 per bin.
        rows = {0: (10, 0, 4), 1: (10, 0, 4)}
        arrays = {0: _bin_array_bytes(0, rows)}
        out = self._compute(0, 1, [3, 3], arrays)
        self.assertEqual(out["amount_x_raw"], 14)
        self.assertEqual(out["amount_y_raw"], 0)

    def test_missing_bin_array_contributes_zero(self):
        arrays = {}  # resolver maps to addresses absent from the map
        out = self._compute(0, 1, [5, 5], arrays)
        self.assertEqual(out["amount_x_raw"], 0)
        self.assertEqual(out["amount_y_raw"], 0)

    def test_negative_bins_across_array_boundary(self):
        # Bins -72 and -70 live in arrays -2 (-140..-71) and -1 (-70..-1).
        # shares[0] -> bin -72, shares[1] -> bin -71, shares[2] -> bin -70.
        shares = [7, 0, 9]
        rows = {-72: (70, 0, 7), -70: (0, 80, 9)}
        arrays = {
            -2: _bin_array_bytes(-2, {-72: rows[-72]}),
            -1: _bin_array_bytes(-1, {-70: rows[-70]}),
        }
        out = self._compute(-72, -70, shares, arrays)
        self.assertEqual(out["amount_x_raw"], _trunc_div(7 * 70, 7))
        self.assertEqual(out["amount_y_raw"], _trunc_div(9 * 80, 9))

    def test_bin_row_round_trip(self):
        data = _bin_array_bytes(-1, {-3: (11, 22, 33)})
        row = bin_row_from_array(data, -3)
        self.assertIsNotNone(row)
        self.assertEqual(row.bin_id, -3)
        self.assertEqual(row.amount_x, 11)
        self.assertEqual(row.amount_y, 22)
        self.assertEqual(row.liquidity_supply, 33)
        # Out-of-range bin id -> no row.
        self.assertIsNone(bin_row_from_array(data, 5))


def _bin_array_addresses(pool, lower, upper):
    """Distinct (bin_array_index, derived_address) pairs for a bin range."""
    from screener_position_py.meteora_pending import (
        bin_id_to_bin_array_index,
        derive_bin_array,
    )

    seen = {}
    for b in range(lower, upper + 1):
        idx = bin_id_to_bin_array_index(b)
        if idx not in seen:
            seen[idx] = derive_bin_array(pool, idx)
    return list(seen.items())


if __name__ == "__main__":
    unittest.main()
