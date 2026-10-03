#!/usr/bin/env python3
"""Tests for raydium_pending: SDK-verified math, PDA derivation, layouts.

Test vectors generated 2026-10-03 from raydium-sdk-v2 PositionUtils
(GetPositionFees / GetPositionRewards / getRewardGrowthInside) via
/tmp/gen_vectors.cjs, plus live-chain PDA cross-checks against the SDK's
getPdaTickArrayAddress (22/22 matches documented in PLAN.md).
"""

import hashlib
import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from screener_position_py.raydium_pending import (
    RaydiumPoolState,
    RaydiumTickState,
    _is_on_curve,
    decode_pool_state,
    decode_tick,
    fee_growth_inside_values,
    pending_fees,
    pending_rewards,
    reward_growth_inside_values,
    tick_array_address,
    tick_array_start_index,
)

U128 = (1 << 128) - 1


def _vec(name):
    import json

    cases = json.load(open("/tmp/ray_vectors.json"))
    return next(c for c in cases if c["name"] == name)


def _apply(vec):
    """Run the SDK-math port on one generated vector."""
    pool = RaydiumPoolState(
        tick_spacing=1,
        tick_current=vec["tick_current"],
        fee_growth_global_a=int(vec["global_a"]),
        fee_growth_global_b=int(vec["global_b"]),
        reward_growth_globals=[int(v) for v in vec["reward_globals"]],
        reward_mints=["", "", ""],
        reward_states=[1, 0, 0],
    )
    lower = RaydiumTickState(
        True,
        0,  # generator's mkTick sets liquidityGross=0: uninitialized semantics
        int(vec["lower_out_a"]),
        int(vec["lower_out_b"]),
        [int(v) for v in vec["lower_out_r"]],
        tick=-200,
    )
    upper = RaydiumTickState(
        True,
        0,
        int(vec["upper_out_a"]),
        int(vec["upper_out_b"]),
        [int(v) for v in vec["upper_out_r"]],
        tick=0,
    )
    inside_a, inside_b = fee_growth_inside_values(
        pool.tick_current, -200, 0, lower, upper,
        pool.fee_growth_global_a, pool.fee_growth_global_b,
    )
    fees = pending_fees(
        int(vec["liquidity"]),
        int(vec["last_a"]),
        int(vec["last_b"]),
        inside_a,
        inside_b,
        int(vec["owed_a"]),
        int(vec["owed_b"]),
    )
    rg_inside = reward_growth_inside_values(
        pool.tick_current, lower, upper, pool
    )
    rewards = pending_rewards(
        int(vec["liquidity"]),
        [int(v) for v in vec["reward_owed"]],
        rg_inside,
        [int(v) for v in vec["reward_last"]],
    )
    return [str(fees[0]), str(fees[1])], [str(r) for r in rewards]


class SdkVectorTests(unittest.TestCase):
    """Port must reproduce raydium-sdk-v2 PositionUtils output exactly."""

    def test_in_range(self):
        vec = _vec("in_range")
        fees, rewards = _apply(vec)
        self.assertEqual(fees, vec["want_fees"])
        self.assertEqual(rewards, vec["want_rewards"])

    def test_below_range(self):
        vec = _vec("below_range")
        fees, rewards = _apply(vec)
        self.assertEqual(fees, vec["want_fees"])
        self.assertEqual(rewards, vec["want_rewards"])

    def test_above_range(self):
        vec = _vec("above_range")
        fees, rewards = _apply(vec)
        self.assertEqual(fees, vec["want_fees"])
        self.assertEqual(rewards, vec["want_rewards"])


class TickArrayStartTests(unittest.TestCase):
    def test_start_index_alignment(self):
        # SDK TickArrayUtil.getTickArrayStartIndex: floor to 60*spacing blocks
        self.assertEqual(tick_array_start_index(-22044, 1), -22080)
        self.assertEqual(tick_array_start_index(-20640, 1), -20640)
        self.assertEqual(tick_array_start_index(0, 1), 0)
        self.assertEqual(tick_array_start_index(-1, 1), -60)
        self.assertEqual(tick_array_start_index(59, 1), 0)
        self.assertEqual(tick_array_start_index(60, 1), 60)
        self.assertEqual(tick_array_start_index(-120, 10), -600)

    def test_pda_matches_sdk_live_values(self):
        # Live-verified 2026-10-03 against SDK getPdaTickArrayAddress.
        pool = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
        self.assertEqual(
            tick_array_address(pool, -22080),
            "GsSTAq6mVM18rL3VfTkj1VvPJZikT5BBmnRgSbURQGBc",
        )
        self.assertEqual(
            tick_array_address(pool, -20640),
            "EgLwEjYBpeBZEqmPoVxCA5xPRFh1ZgAVz16NYfrCjxZV",
        )

    def test_pda_bump_descends(self):
        # Same seeds with a different pool must still derive (smoke).
        pool = "3nMFwZXwY1s1M5s8vYAHqd4wGs4iSxXE4LRoUMMYqEgF"
        addr = tick_array_address(pool, 0)
        self.assertEqual(len(addr), 44)  # base58 pubkey length


class CurveCheckTests(unittest.TestCase):
    """_is_on_curve against ground-truth Edwards25519 points."""

    P = (1 << 255) - 19
    D = (-121665 * pow(121666, P - 2, P)) % P

    def _ed_add(self, P1, P2):
        x1, y1 = P1
        x2, y2 = P2
        x3 = ((x1 * y2 + x2 * y1) * pow(1 + self.D * x1 * x2 * y1 * y2, self.P - 2, self.P)) % self.P
        y3 = ((y1 * y2 + x1 * x2) * pow(1 - self.D * x1 * x2 * y1 * y2, self.P - 2, self.P)) % self.P
        return x3, y3

    def _scalarmult(self, k, point):
        result = (0, 1)
        while k:
            if k & 1:
                result = self._ed_add(result, point)
            point = self._ed_add(point, point)
            k >>= 1
        return result

    def test_real_points_on_curve(self):
        base_x = 15112221349535400772501151409588531511454012693041857206046113283949847762202
        base_y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
        for k in (1, 2, 3, 4, 5, 8, 16, 99, 255):
            x, y = self._scalarmult(k, (base_x, base_y))
            encoded = (y | ((x & 1) << 7)).to_bytes(32, "little")
            self.assertTrue(_is_on_curve(encoded), f"{k}B should be on curve")

    def test_identity_on_curve(self):
        self.assertTrue(_is_on_curve(bytes(32)))

    def test_random_hashes_split(self):
        # ~half of random 32-byte strings are off-curve (PDA candidates).
        on = sum(
            1 for i in range(100) if _is_on_curve(hashlib.sha256(bytes([i])).digest())
        )
        self.assertGreater(on, 20)
        self.assertLess(on, 80)


class DecodeTests(unittest.TestCase):
    """Layout offsets verified against live accounts + SDK layout spans."""

    def _pool_bytes(self):
        return (
            bytes(8)  # discriminator
            + bytes([255])  # bump
            + bytes(224)  # ..through start fields
            + (9).to_bytes(1, "little")  # 233 mintDecimalsA
            + (6).to_bytes(1, "little")  # 234 mintDecimalsB
            + (10).to_bytes(2, "little")  # 235 tickSpacing
            + bytes(32)  # 237 liquidity + sqrtPrice
            + (-500).to_bytes(4, "little", signed=True)  # 269 tickCurrent
            + bytes(4)  # 273..276 (recentEpoch tail)
            + (12345).to_bytes(16, "little")  # 277 fgg_a
            + (67890).to_bytes(16, "little")  # 293 fgg_b
            + bytes(88)  # to reward region @397
        )

    def test_decode_pool_state_offsets(self):
        data = bytearray(self._pool_bytes())
        data += bytes(3 * 169)  # reward region @397: 3 slots x 169 bytes
        # reward slot 0 at 397: state, mint@+57, growth@+153
        data[397] = 3
        data[397 + 57 : 397 + 89] = bytes(range(32))
        data[397 + 153 : 397 + 169] = (999).to_bytes(16, "little")
        state = decode_pool_state(bytes(data))
        self.assertEqual(state.tick_spacing, 10)
        self.assertEqual(state.tick_current, -500)
        self.assertEqual(state.fee_growth_global_a, 12345)
        self.assertEqual(state.fee_growth_global_b, 67890)
        self.assertEqual(state.reward_states, [3, 0, 0])
        self.assertEqual(state.reward_growth_globals[0], 999)
        self.assertEqual(state.reward_growth_globals[1:], [0, 0])

    def test_decode_tick_rows(self):
        start = -60
        data = bytearray(44 + 60 * 168)
        data[40:44] = start.to_bytes(4, "little", signed=True)
        row = 44 + 5 * 168  # tick index 5 => tick = start + 5
        tick_val = start + 5
        data[row : row + 4] = tick_val.to_bytes(4, "little", signed=True)
        data[row + 20 : row + 36] = (55).to_bytes(16, "little")
        data[row + 36 : row + 52] = (77).to_bytes(16, "little")
        data[row + 52 : row + 68] = (88).to_bytes(16, "little")
        data[row + 68 : row + 84] = (1).to_bytes(16, "little")
        tick = decode_tick(bytes(data), tick_val)
        self.assertTrue(tick.initialized)
        self.assertEqual(tick.liquidity_gross, 55)
        self.assertEqual(tick.fee_growth_outside_a, 77)
        self.assertEqual(tick.fee_growth_outside_b, 88)
        self.assertEqual(tick.reward_growths_outside[0], 1)
        missing = decode_tick(bytes(data), tick_val + 1000)
        self.assertFalse(missing.initialized)
        self.assertEqual(missing.fee_growth_outside_a, 0)

    def test_reward_no_wrap_when_growth_unchanged(self):
        # delta 0 + owed 5 => 5. SDK keeps BN sums unmasked; only the on-chain
        # program masks into u64 storage at claim time.
        rewards = pending_rewards(1000, [5, 0, 0], [42, 0, 0], [42, 0, 0])
        self.assertEqual(rewards, [5, 0, 0])

    def test_reward_below_uses_liquidity_gross(self):
        # SDK: liquidity_gross == 0 on the lower tick => below = growth_global.
        pool = RaydiumPoolState(
            tick_spacing=1,
            tick_current=10,
            fee_growth_global_a=0,
            fee_growth_global_b=0,
            reward_growth_globals=[100, 0, 0],
            reward_mints=["", "", ""],
            reward_states=[1, 0, 1],
        )
        lower_uninit = RaydiumTickState(False, 0, 5, 5, [7, 0, 0], tick=-10)
        upper = RaydiumTickState(True, 500, 11, 11, [3, 0, 0], tick=20)
        inside = reward_growth_inside_values(10, lower_uninit, upper, pool)
        # below = 100 (global); above = upper.outside (current < upper.tick)
        inside_expected = (100 - 100 - 3) & ((1 << 128) - 1)
        self.assertEqual(inside[0], inside_expected)


if __name__ == "__main__":
    unittest.main()
