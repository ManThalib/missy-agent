"""Orca Whirlpool pending fee/reward computation (stdlib only).

Ports the whirlpools-sdk `collectFeesQuote` / `collectRewardsQuote` math to
Python so Orca positions report live pending amounts instead of the stale
checkpointed `Position.feeOwedA/B` / `rewardInfos[].amountOwed`, which only
update when a claim or pool touch lands.

Transcribed from `@orca-so/whirlpools-sdk` dist (quotes/public/collect-fees-quote.js,
collect-rewards-quote.js) on 2026-10-04; verified against the SDK itself on
live mainnet accounts (see /tmp/fee-verify, report missy_cross_dex_fairness).

Layouts (packed, after 8-byte anchor discriminator; from the whirlpools IDL,
verified against live accounts 2026-10-04):

Whirlpool (629 bytes):
    liquidity             49  (u128)
    sqrt_price            65  (u128)
    tick_current_index    81  (i32)   [also in solana-account-decode skill]
    fee_growth_global_a  165  (u128)
    fee_growth_global_b  245  (u128)
    reward_last_updated_timestamp  261  (u64)
    reward_infos[3] @269, stride 120:
        mint                       +0   (pubkey)
        vault                      +32  (pubkey)
        extension                  +64  ([u8;32])
        emissions_per_second_x64   +96  (u128)
        growth_global_x64          +112 (u128)
    inactive slot: mint and vault both the system program id.

Position (216 bytes):
    whirlpool              8  (pubkey)
    position_mint         40  (pubkey)
    liquidity             72  (u128)
    tick_lower_index      88  (i32)
    tick_upper_index      92  (i32)
    fee_growth_checkpoint_a  96  (u128)
    fee_owed_a           112  (u64)
    fee_growth_checkpoint_b 120  (u128)
    fee_owed_b           136  (u64)
    reward_infos[3] @144, stride 24:
        growth_inside_checkpoint  +0   (u128)
        amount_owed               +16  (u64)

TickArray (9988 bytes): start_tick_index i32 @8, ticks[88] @12, stride 113:
    initialized        +0   (u8, 1 = initialized)
    liquidity_net      +1   (i128)
    liquidity_gross    +17  (u128)
    fee_growth_outside_a +33 (u128)
    fee_growth_outside_b +49 (u128)
    reward_growths_outside[3] +65/+81/+97 (u128 each)

DynamicTickArray (10004 bytes): start_tick_index i32 @8, whirlpool @12,
tick_bitmap u128 @44, ticks[88] @60, stride 113 (Rust enum: 1 tag byte +
112-byte DynamicTickData; tag 0 = uninitialized):
    liquidity_net      +1   (i128)
    liquidity_gross    +17  (u128)
    fee_growth_outside_a +33 (u128)
    fee_growth_outside_b +49 (u128)
    reward_growths_outside[3] +65/+81/+97 (u128 each)

Tick-array PDA (SDK pda-utils `getTickArray`): seeds =
[b"tick_array", whirlpool_bytes, ASCII decimal string of start index]
(e.g. b"-21280") -- NOT big-endian bytes; this differs from Raydium.
Start index (SDK TickUtil.getStartTickIndex):
    floor(tick / (tick_spacing * 88)) * tick_spacing * 88
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from core.rpc import RpcClient

U128_MASK = (1 << 128) - 1
TICKS_PER_ARRAY = 88
WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
_SYSTEM_PROGRAM_BYTES = bytes(32)  # Pubkey.default = all zeros


def _sub_underflow_u128(a: int, b: int) -> int:
    """SDK MathUtil.subUnderflowU128: wrapping subtraction mod 2^128."""
    return (a - b) & U128_MASK


@dataclass
class WhirlpoolState:
    tick_spacing: int
    liquidity: int
    tick_current_index: int
    fee_growth_global_a: int
    fee_growth_global_b: int
    reward_last_updated_timestamp: int
    reward_mints: List[str]
    reward_emissions_x64: List[int]
    reward_growth_globals: List[int]


@dataclass
class WhirlpoolPosition:
    whirlpool: str
    liquidity: int
    tick_lower_index: int
    tick_upper_index: int
    fee_growth_checkpoint_a: int
    fee_owed_a: int
    fee_growth_checkpoint_b: int
    fee_owed_b: int
    reward_growth_inside_checkpoints: List[int]
    reward_amounts_owed: List[int]


@dataclass
class WhirlpoolTick:
    """One tick row. `initialized` False leaves outside values at 0."""

    initialized: bool
    fee_growth_outside_a: int
    fee_growth_outside_b: int
    reward_growths_outside: List[int]


def decode_whirlpool(data: bytes) -> WhirlpoolState:
    if len(data) < 629:
        raise ValueError(f"Whirlpool too short: {len(data)}")
    from core.solana import b58encode

    mints, emissions, growths = [], [], []
    for i in range(3):
        base = 269 + i * 120
        mints.append(b58encode(data[base : base + 32]))
        emissions.append(int.from_bytes(data[base + 96 : base + 112], "little"))
        growths.append(int.from_bytes(data[base + 112 : base + 128], "little"))
    return WhirlpoolState(
        tick_spacing=int.from_bytes(data[41:43], "little"),
        liquidity=int.from_bytes(data[49:65], "little"),
        tick_current_index=int.from_bytes(data[81:85], "little", signed=True),
        fee_growth_global_a=int.from_bytes(data[165:181], "little"),
        fee_growth_global_b=int.from_bytes(data[245:261], "little"),
        reward_last_updated_timestamp=int.from_bytes(data[261:269], "little"),
        reward_mints=mints,
        reward_emissions_x64=emissions,
        reward_growth_globals=growths,
    )


def decode_position(data: bytes) -> WhirlpoolPosition:
    if len(data) < 216:
        raise ValueError(f"Position too short: {len(data)}")
    from core.solana import b58encode

    checkpoints, owed = [], []
    for i in range(3):
        base = 144 + i * 24
        checkpoints.append(int.from_bytes(data[base : base + 16], "little"))
        owed.append(int.from_bytes(data[base + 16 : base + 24], "little"))
    return WhirlpoolPosition(
        whirlpool=b58encode(data[8:40]),
        liquidity=int.from_bytes(data[72:88], "little"),
        tick_lower_index=int.from_bytes(data[88:92], "little", signed=True),
        tick_upper_index=int.from_bytes(data[92:96], "little", signed=True),
        fee_growth_checkpoint_a=int.from_bytes(data[96:112], "little"),
        fee_owed_a=int.from_bytes(data[112:120], "little"),
        fee_growth_checkpoint_b=int.from_bytes(data[120:136], "little"),
        fee_owed_b=int.from_bytes(data[136:144], "little"),
        reward_growth_inside_checkpoints=checkpoints,
        reward_amounts_owed=owed,
    )


def tick_array_start_index(tick: int, tick_spacing: int) -> int:
    """SDK TickUtil.getStartTickIndex (true floor; Python // floors)."""
    return tick // (tick_spacing * TICKS_PER_ARRAY) * tick_spacing * TICKS_PER_ARRAY


def tick_array_address(pool_address: str, start_index: int) -> str:
    """SDK PDAUtil.getTickArray: seed is the ASCII decimal string.

    Verified against the SDK on live mainnet (2026-10-04): SOL/USDC
    Czfq3xZZ... arrays at starts -192720 and -192544 reproduce the SDK
    addresses byte-for-byte.
    """
    from core.solana import b58decode

    seeds = [
        b"tick_array",
        b58decode(pool_address),
        str(start_index).encode("ascii"),
    ]
    from .raydium_pending import _pda

    return _pda(seeds, b58decode(WHIRLPOOL_PROGRAM))

def decode_tick_array(data: Optional[bytes], tick: int) -> WhirlpoolTick:
    """Extract one tick row from a TickArray or DynamicTickArray account.

    Static arrays carry an `initialized` flag byte; dynamic arrays encode
    initialization as an enum tag (0 = uninitialized) per slot.
    """
    zeros = WhirlpoolTick(False, 0, 0, [0, 0, 0])
    if not data or len(data) < 60:
        return zeros
    dynamic = data[:8] == _account_discriminator("DynamicTickArray")
    ticks_base = 60 if dynamic else 12
    stride = 113
    start = int.from_bytes(data[8:12], "little", signed=True)
    offset = tick - start
    if offset < 0 or offset >= TICKS_PER_ARRAY:
        return zeros
    base = ticks_base + offset * stride
    if base + stride > len(data):
        return zeros
    # Both layouts lead each 113-byte slot with an init marker: a bool byte
    # (static) or the enum tag 0/1 (dynamic).
    initialized = data[base] == 1
    body = base + 1
    return WhirlpoolTick(
        initialized=initialized,
        fee_growth_outside_a=int.from_bytes(data[body + 32 : body + 48], "little"),
        fee_growth_outside_b=int.from_bytes(data[body + 48 : body + 64], "little"),
        reward_growths_outside=[
            int.from_bytes(data[body + 64 + i * 16 : body + 80 + i * 16], "little")
            for i in range(3)
        ],
    )


def _account_discriminator(name: str) -> bytes:
    import hashlib

    return hashlib.sha256(b"account:" + name.encode()).digest()[:8]


def fee_growth_inside_values(
    tick_current: int,
    tick_lower: int,
    tick_upper: int,
    lower: WhirlpoolTick,
    upper: WhirlpoolTick,
    global_a: int,
    global_b: int,
) -> Tuple[int, int]:
    """SDK collectFeesQuote growth-inside, transcribed exactly.

    below = current < lower ? global - lower.outside : lower.outside
    above = current < upper ? upper.outside : global - upper.outside
    inside = global - below - above     (all subUnderflowU128)
    """
    def _per(global_v: int, lower_out: int, upper_out: int) -> int:
        below = (
            _sub_underflow_u128(global_v, lower_out)
            if tick_current < tick_lower
            else lower_out
        )
        above = (
            upper_out
            if tick_current < tick_upper
            else _sub_underflow_u128(global_v, upper_out)
        )
        return _sub_underflow_u128(_sub_underflow_u128(global_v, below), above)

    return (
        _per(global_a, lower.fee_growth_outside_a, upper.fee_growth_outside_a),
        _per(global_b, lower.fee_growth_outside_b, upper.fee_growth_outside_b),
    )


def pending_fees(position: WhirlpoolPosition, inside_a: int, inside_b: int) -> Tuple[int, int]:
    """SDK collectFeesQuote tail: owed + ((inside - checkpoint) * liq >> 64).

    BN sum left unmasked (matches the SDK; the on-chain program masks into
    u64 only at claim time).
    """
    delta_a = _sub_underflow_u128(inside_a, position.fee_growth_checkpoint_a)
    delta_b = _sub_underflow_u128(inside_b, position.fee_growth_checkpoint_b)
    return (
        position.fee_owed_a + ((delta_a * position.liquidity) >> 64),
        position.fee_owed_b + ((delta_b * position.liquidity) >> 64),
    )


def reward_growth_inside_values(
    tick_current: int,
    tick_lower: int,
    tick_upper: int,
    lower: WhirlpoolTick,
    upper: WhirlpoolTick,
    adjusted_global: int,
) -> int:
    """SDK collectRewardsQuote per-reward growth-inside, transcribed.

    below = adjusted_global unless the lower tick is initialized:
            current < lower ? global - lower.outside : lower.outside
    above = 0 unless the upper tick is initialized:
            current < upper ? upper.outside : global - upper.outside
    """
    below = adjusted_global
    if lower.initialized:
        below = (
            _sub_underflow_u128(adjusted_global, lower.reward_growths_outside)
            if tick_current < tick_lower
            else lower.reward_growths_outside
        )
    above = 0
    if upper.initialized:
        above = (
            upper.reward_growths_outside
            if tick_current < tick_upper
            else _sub_underflow_u128(adjusted_global, upper.reward_growths_outside)
        )
    return _sub_underflow_u128(_sub_underflow_u128(adjusted_global, below), above)


def pending_rewards(
    pool: WhirlpoolState,
    position: WhirlpoolPosition,
    lower: WhirlpoolTick,
    upper: WhirlpoolTick,
    current_timestamp: int,
) -> List[int]:
    """SDK collectRewardsQuote, transcribed exactly (transfer-fee exempt).

    The reward growth global is advanced from rewardLastUpdatedTimestamp to
    `current_timestamp` via mulDiv(delta, emissions, pool_liquidity) when
    the pool holds liquidity -- this is why live rewards need a timestamp.
    """
    out: List[int] = []
    delta_t = current_timestamp - pool.reward_last_updated_timestamp
    for i in range(3):
        mint = pool.reward_mints[i]
        if mint in ("", "11111111111111111111111111111111"):
            out.append(0)
            continue
        adjusted_global = pool.reward_growth_globals[i]
        if pool.liquidity != 0:
            # BitMath.mulDiv(delta, emissions, liquidity, 128) -- plain floor
            # division on non-negative operands.
            growth_delta = (delta_t * pool.reward_emissions_x64[i]) // pool.liquidity
            adjusted_global = (adjusted_global + growth_delta) & U128_MASK
        inside = reward_growth_inside_values(
            pool.tick_current_index,
            position.tick_lower_index,
            position.tick_upper_index,
            lower,
            upper,
            adjusted_global,
        )
        delta = _sub_underflow_u128(inside, position.reward_growth_inside_checkpoints[i])
        total_x64 = (position.reward_amounts_owed[i] << 64) + delta * position.liquidity
        out.append(total_x64 >> 64)
    return out


def is_reward_initialized(mint: str) -> bool:
    """SDK PoolUtil.isRewardInitialized: mint/vault not Pubkey.default."""
    return mint not in ("", "11111111111111111111111111111111")


class OrcaPendingFeesFetcher:
    """Fetches whirlpool + boundary tick-array accounts and computes live
    pending fees/rewards. Read-only."""

    TICK_ARRAY_BATCH = 100

    def __init__(self, rpc: RpcClient):
        self.rpc = rpc

    def fetch_whirlpool(self, pool_address: str) -> WhirlpoolState:
        import base64

        result = self.rpc.call("getAccountInfo", [pool_address, {"encoding": "base64"}])
        value = (result or {}).get("value")
        if not value:
            raise ValueError(f"whirlpool not found: {pool_address}")
        return decode_whirlpool(base64.b64decode(value["data"][0]))

    def fetch_tick_arrays(
        self,
        pool_address: str,
        tick_spacing: int,
        ticks: Sequence[int],
    ) -> dict:
        """start_index -> raw account bytes for the arrays holding `ticks`."""
        import base64

        starts = sorted({tick_array_start_index(t, tick_spacing) for t in ticks})
        addresses = [tick_array_address(pool_address, s, tick_spacing) for s in starts]
        out: dict = {}
        for start in range(0, len(addresses), self.TICK_ARRAY_BATCH):
            batch = addresses[start : start + self.TICK_ARRAY_BATCH]
            results = self.rpc.batch(
                [("getAccountInfo", [a, {"encoding": "base64"}]) for a in batch]
            )
            for st, res in zip(starts, results):
                value = (res or {}).get("value")
                out[st] = base64.b64decode(value["data"][0]) if value else None
        return out

    def compute_pending(
        self,
        pool_address: str,
        position_data: bytes,
        current_timestamp: Optional[int] = None,
    ) -> dict:
        """Live pending fees/rewards for one Orca position.

        `position_data` is the raw Position account bytes (fetch and cache
        upstream). Timestamp defaults to the RPC clock sysvar so the reward
        accrual matches what a claim would pay now.
        """
        import base64

        position = decode_position(position_data)
        pool = self.fetch_whirlpool(pool_address)
        if current_timestamp is None:
            result = self.rpc.call(
                "getAccountInfo",
                ["SysvarC1ock11111111111111111111111111111111", {"encoding": "base64"}],
            )
            value = (result or {}).get("value")
            if not value:
                raise ValueError("clock sysvar unavailable")
            clock = base64.b64decode(value["data"][0])
            current_timestamp = int.from_bytes(clock[32:40], "little", signed=True)

        arrays = self.fetch_tick_arrays(
            pool_address,
            pool.tick_spacing,
            (position.tick_lower_index, position.tick_upper_index),
        )
        lower = decode_tick_array(
            arrays.get(tick_array_start_index(position.tick_lower_index, pool.tick_spacing)),
            position.tick_lower_index,
        )
        upper = decode_tick_array(
            arrays.get(tick_array_start_index(position.tick_upper_index, pool.tick_spacing)),
            position.tick_upper_index,
        )
        inside_a, inside_b = fee_growth_inside_values(
            pool.tick_current_index,
            position.tick_lower_index,
            position.tick_upper_index,
            lower,
            upper,
            pool.fee_growth_global_a,
            pool.fee_growth_global_b,
        )
        fees = pending_fees(position, inside_a, inside_b)
        rewards = pending_rewards(pool, position, lower, upper, current_timestamp)
        return {
            "fees_owed_raw": [fees[0], fees[1]],
            "rewards_owed_raw": rewards,
            "reward_mints": [
                m if is_reward_initialized(m) else "" for m in pool.reward_mints
            ],
            "tick_spacing": pool.tick_spacing,
            "tick_current_index": pool.tick_current_index,
            "lower_tick_initialized": lower.initialized,
            "upper_tick_initialized": upper.initialized,
        }
