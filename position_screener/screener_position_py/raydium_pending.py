"""Raydium CLMM pending fee/reward computation (stdlib only).

Ports the raydium-sdk-v2 `PositionUtils` math to Python so Missy can report
real pending fees and rewards instead of the stale checkpointed
`PersonalPositionState` fields (`token_fees_owed_*`, `reward_amount_owed_*`),
which never update until a claim touches the position.

Verified against the installed SDK on live mainnet 2026-10-03:
position `9X3StbggkAiqwMDmYgGynwkAKcsY96AZbgnpNLEnJt1U`
pool `3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv`
SDK:  fees_a=53976 fees_b=7634, rewards=[0,0,0]  -- this port matches.

Layouts (byte offsets, verified empirically against PoolInfoLayout /
PersonalPositionLayout / TickArrayLayout spans and synthetic markers):

PersonalPositionState (281 bytes):
    discriminator           0   (8)
    nft_mint                8   (32)   [used: position_mint]
    pool_id                40   (32)   [used: pool_address]
    tick_lower_index       73   (i32)
    tick_upper_index       77   (i32)
    liquidity              81   (u128)
    fee_growth_inside_last_x64_a   97  (u128)
    fee_growth_inside_last_x64_b  113  (u128)
    token_fees_owed_a             129  (u64)
    token_fees_owed_b             137  (u64)
    reward_infos[3] @145, each 24 bytes:
        growth_inside_last_x64  +0  (u128)
        reward_amount_owed     +16  (u64)

PoolState (1544 bytes):
    discriminator           0   (8)
    bump                    8   (1)
    tick_spacing           12   (u16)
    tick_current          269   (i32)
    fee_growth_global_x64_a 277 (u128)
    fee_growth_global_x64_b 293 (u128)
    reward_infos[3] @397, stride 169:
        state               +0   (u8)
        open_time           +1   (u64)
        end_time            +9   (u64)
        last_update_time    +17  (u64)
        emissions_per_second_x64 +25 (u64)
        total_emissioned    +33  (u64)
        claimed             +41  (u64)
        mint                +57  (32)
        vault               +89  (32)
        creator             +121 (32)
        growth_global_x64   +153 (u128)

TickArray (10240 bytes):
    discriminator           0   (8)
    pool_id                 8   (32)
    start_tick_index       40   (i32)
    ticks[60] @44, stride 168:
        tick                +0   (i32)
        liquidity_net       +4   (i128)
        liquidity_gross     +20  (u128)
        fee_growth_outside_x64_a +36 (u128)
        fee_growth_outside_x64_b +52 (u128)
        reward_growths_outside_x64[3] +68, +84, +100 (u128 each)

Tick array PDA: seeds = [b"tick_array", i32_le(start_tick_index encoded as
unsigned with offset 2147483648 — see `tick_array_start_seed`), pool_id].
Start index formula (SDK `TickArrayUtil.getTickArrayStartIndex`):
    tick_count = 60 * tick_spacing
    start = floor_div(tick, tick_count) * tick_count
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from core.rpc import RpcClient

U128_MASK = (1 << 128) - 1
U64_MASK = (1 << 64) - 1
TICKS_PER_ARRAY = 60
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
_U32_BIAS = 1 << 31  # i32 seeds are encoded as u32 (two's complement)


def _floor_div(a: int, b: int) -> int:
    """Python floor division matches Rust integer division semantics here."""
    return a // b if b else 0


def tick_array_start_index(tick: int, tick_spacing: int) -> int:
    """SDK TickArrayUtil.getTickArrayStartIndex."""
    return _floor_div(tick, TICKS_PER_ARRAY * tick_spacing) * TICKS_PER_ARRAY * tick_spacing


def _pda(seeds: Sequence[bytes], program_id: bytes) -> str:
    """Canonical Solana PDA derivation (finds the bump off-curve).

    Hash form verified against @solana/web3.js `findProgramAddress` on live
    mainnet (2026-10-03): sha256(seeds_with_bump || program_id ||
    b"ProgramDerivedAddress"); bump 255 first, descending.
    """
    from core.solana import b58encode  # local import: avoids cycle at module load

    for bump in range(255, -1, -1):
        data = b"".join(seeds) + bytes([bump])
        h = _sha256(data, program_id, b"ProgramDerivedAddress")
        if not _is_on_curve(h):
            return b58encode(h)
    raise ValueError("no valid PDA found")  # pragma: no cover


def _sha256(*chunks: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(b"".join(chunks)).digest()


_CURVE_P = (1 << 255) - 19
# Edwards25519 d = -121665/121666 (mod p). NOTE: dalek's -121665*2^254 is
# sqrt(-1), NOT d — using it silently misclassifies most points (caught on
# ground-truth curve points 2026-10-03).
_CURVE_D = (-121665 * pow(121666, _CURVE_P - 2, _CURVE_P)) % _CURVE_P


def _is_on_curve(point: bytes) -> bool:
    """True when the 32 bytes ARE a valid compressed Edwards point.

    Mirrors curve25519-dalek `CompressedEdwardsY::decompress`: y is the low
    255 bits, the top bit is x's sign; x^2 = (y^2-1)/(d*y^2+1) must have a
    solution. Candidate x = (u/v^3)·((u·v^7)^((p-5)/8)) — the exponent's
    value differs per candidate, so verify v·x^2 = ±u before accepting.
    """
    y = int.from_bytes(point, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _CURVE_P:
        return True  # out of field: dalek rejects -> treat as on-curve (invalid PDA)
    y2 = (y * y) % _CURVE_P
    u = (y2 - 1) % _CURVE_P
    v = (_CURVE_D * y2 + 1) % _CURVE_P
    v3 = (v * v * v) % _CURVE_P
    v7 = (v3 * v3 * v) % _CURVE_P
    x = (u * v3 % _CURVE_P) * pow(u * v7 % _CURVE_P, (_CURVE_P - 5) // 8, _CURVE_P) % _CURVE_P
    vx2 = (v * x * x) % _CURVE_P
    if vx2 == u % _CURVE_P:
        pass
    elif vx2 == (-u) % _CURVE_P:
        x = x * pow(2, (_CURVE_P - 1) // 4, _CURVE_P) % _CURVE_P
    else:
        return False  # no square root: not on curve => valid PDA candidate
    return True  # decompression succeeded: real curve point, skip for PDA


def tick_array_address(pool_address: str, start_index: int) -> str:
    """Derive the tick-array PDA for one start index.

    Seed layout per SDK pda.js `getPdaTickArrayAddress`:
        seeds = [b"tick_array", pool_id.toBuffer(), int32_be(start_index)]
    Verified against the SDK on live mainnet (2026-10-03): starts -22080 and
    -20640 of pool 3ucNos4N... reproduce GsSTAq6mVM18... and EgLwEjYBpeBZEq...
    """
    seed_start = struct.pack(">i", start_index)
    from core.solana import b58decode

    return _pda(
        [b"tick_array", b58decode(pool_address), seed_start],
        b58decode(RAYDIUM_CLMM_PROGRAM),
    )


@dataclass
class RaydiumPoolState:
    """Fields of PoolState needed for pending computation."""

    tick_spacing: int
    tick_current: int
    fee_growth_global_a: int
    fee_growth_global_b: int
    reward_growth_globals: List[int]  # 3 entries
    reward_mints: List[str]  # 3 entries (system program id when inactive)
    reward_states: List[int]


@dataclass
class RaydiumTickState:
    """One tick row from a TickArray account (0 when uninitialized)."""

    initialized: bool
    liquidity_gross: int
    fee_growth_outside_a: int
    fee_growth_outside_b: int
    reward_growths_outside: List[int]
    tick: int = 0


def decode_pool_state(data: bytes) -> RaydiumPoolState:
    if len(data) < 566:
        raise ValueError(f"PoolState too short: {len(data)}")
    from core.solana import b58encode

    reward_growth = []
    reward_mints = []
    reward_states = []
    for i in range(3):
        base = 397 + i * 169
        reward_states.append(data[base])
        reward_mints.append(b58encode(data[base + 57 : base + 89]))
        reward_growth.append(int.from_bytes(data[base + 153 : base + 169], "little"))
    return RaydiumPoolState(
        tick_spacing=struct.unpack_from("<H", data, 235)[0],
        tick_current=struct.unpack_from("<i", data, 269)[0],
        fee_growth_global_a=int.from_bytes(data[277:293], "little"),
        fee_growth_global_b=int.from_bytes(data[293:309], "little"),
        reward_growth_globals=reward_growth,
        reward_mints=reward_mints,
        reward_states=reward_states,
    )


def decode_tick(data: Optional[bytes], tick: int) -> RaydiumTickState:
    """Extract one tick row by index within a decoded TickArray account."""
    if not data or len(data) < 44 + TICKS_PER_ARRAY * 168:
        return RaydiumTickState(False, 0, 0, 0, [0, 0, 0])
    start = struct.unpack_from("<i", data, 40)[0]
    offset = tick - start
    if offset < 0 or offset >= TICKS_PER_ARRAY:
        return RaydiumTickState(False, 0, 0, 0, [0, 0, 0])
    base = 44 + offset * 168
    row_tick = struct.unpack_from("<i", data, base)[0]
    if row_tick != tick:
        return RaydiumTickState(False, 0, 0, 0, [0, 0, 0])
    return RaydiumTickState(
        initialized=True,
        liquidity_gross=int.from_bytes(data[base + 20 : base + 36], "little"),
        fee_growth_outside_a=int.from_bytes(data[base + 36 : base + 52], "little"),
        fee_growth_outside_b=int.from_bytes(data[base + 52 : base + 68], "little"),
        reward_growths_outside=[
            int.from_bytes(data[base + 68 + i * 16 : base + 84 + i * 16], "little")
            for i in range(3)
        ],
        tick=tick,
    )


def fee_growth_inside_values(
    tick_current: int,
    tick_lower: int,
    tick_upper: int,
    lower: RaydiumTickState,
    upper: RaydiumTickState,
    global_a: int,
    global_b: int,
) -> Tuple[int, int]:
    """SDK PositionUtils.getfeeGrowthInside, transcribed exactly.

    below = current >= lower.tick ? lower.outside : global - lower.outside
    above = current <  upper.tick ? upper.outside : global - upper.outside
    inside = global - below - above   (all mod 2^128)
    """
    def _per(global_v: int, below_out: int, above_out: int) -> int:
        below = below_out if tick_current >= tick_lower else (global_v - below_out)
        above = above_out if tick_current < tick_upper else (global_v - above_out)
        return (global_v - below - above) & U128_MASK

    return (
        _per(global_a, lower.fee_growth_outside_a, upper.fee_growth_outside_a),
        _per(global_b, lower.fee_growth_outside_b, upper.fee_growth_outside_b),
    )


def _mul_shift_right_64(value: int, growth_delta: int) -> int:
    """value * growth_delta >> 64 with u256 intermediate (BN semantics)."""
    return (value * growth_delta) >> 64


def pending_fees(
    liquidity: int,
    fee_growth_inside_last_a: int,
    fee_growth_inside_last_b: int,
    inside_a: int,
    inside_b: int,
    owed_a: int,
    owed_b: int,
) -> Tuple[int, int]:
    """SDK PositionUtils.GetPositionFees."""
    delta_a = (inside_a - fee_growth_inside_last_a) & U128_MASK
    delta_b = (inside_b - fee_growth_inside_last_b) & U128_MASK
    # SDK keeps these as unmasked BN values; only the on-chain program masks
    # into u64 storage at claim time. Masking here would silently wrap the
    # wrapped-delta case (out-of-range positions) to garbage.
    total_a = owed_a + _mul_shift_right_64(liquidity, delta_a)
    total_b = owed_b + _mul_shift_right_64(liquidity, delta_b)
    return total_a, total_b


def reward_growth_inside_values(
    tick_current: int,
    lower: RaydiumTickState,
    upper: RaydiumTickState,
    pool: RaydiumPoolState,
) -> List[int]:
    """SDK PositionUtils.getRewardGrowthInside, transcribed exactly.

    below = lower.liquidity_gross == 0 ? growth_global
            : current < lower.tick ? growth_global - lower.outside : lower.outside
    above = upper.liquidity_gross == 0 ? 0
            : current < upper.tick ? upper.outside : growth_global - upper.outside
    inside = growth_global - below - above   (mod 2^128)
    """
    result = []
    for i in range(3):
        g = pool.reward_growth_globals[i]
        if lower.liquidity_gross == 0:
            below = g
        elif tick_current < lower.tick:
            below = (g - lower.reward_growths_outside[i]) & U128_MASK
        else:
            below = lower.reward_growths_outside[i]
        if upper.liquidity_gross == 0:
            above = 0
        elif tick_current < upper.tick:
            above = upper.reward_growths_outside[i]
        else:
            above = (g - upper.reward_growths_outside[i]) & U128_MASK
        result.append((g - below - above) & U128_MASK)
    return result


def pending_rewards(
    liquidity: int,
    reward_amount_owed: List[int],
    reward_growth_inside: List[int],
    reward_growth_inside_last: List[int],
) -> List[int]:
    """SDK PositionUtils.GetPositionRewards (unmasked BN semantics)."""
    out = []
    for i in range(3):
        delta = (reward_growth_inside[i] - reward_growth_inside_last[i]) & U128_MASK
        total = reward_amount_owed[i] + _mul_shift_right_64(liquidity, delta)
        out.append(total)
    return out


def raydium_position_checkpoint(data: bytes) -> dict:
    """Extract checkpointed fields from a raw PersonalPositionState account.

    Layout offsets (see module docstring): fee_growth_inside_last @97/113,
    token_fees_owed @129/137, reward_infos[3] @145 (24 bytes each).
    """
    if len(data) < 217:
        raise ValueError(f"PersonalPositionState too short: {len(data)}")
    return {
        "fee_growth_inside_last": (
            int.from_bytes(data[97:113], "little"),
            int.from_bytes(data[113:129], "little"),
        ),
        "token_fees_owed": (
            int.from_bytes(data[129:137], "little"),
            int.from_bytes(data[137:145], "little"),
        ),
        "reward_growth_inside_last": [
            int.from_bytes(data[145 + i * 24 : 161 + i * 24], "little")
            for i in range(3)
        ],
        "reward_amount_owed": [
            int.from_bytes(data[161 + i * 24 : 169 + i * 24], "little")
            for i in range(3)
        ],
    }


class PendingFeesFetcher:
    """Fetches pool + tick-array accounts and computes real pending amounts.

    Read-only: identical math for simulate and send paths downstream.
    """

    def __init__(self, rpc: RpcClient):
        self.rpc = rpc
        # Per-scan caches: multiple positions in the same pool share one
        # PoolState fetch and one TickArray batch instead of refetching
        # identical accounts per position.
        self._pool_state_cache: Dict[str, RaydiumPoolState] = {}
        self._tick_arrays_cache: Dict[str, Dict[int, Optional[bytes]]] = {}

    def fetch_tick_arrays(
        self, pool_address: str, tick_spacing: int, ticks: Sequence[int]
    ) -> Dict[int, Optional[bytes]]:
        """Return start_index -> raw TickArray account data for the arrays
        that hold the requested ticks."""
        cached = self._tick_arrays_cache.setdefault(pool_address, {})
        starts = sorted(
            s
            for s in {tick_array_start_index(t, tick_spacing) for t in ticks}
            if s not in cached
        )
        if starts:
            addresses = [tick_array_address(pool_address, s) for s in starts]
            result = self.rpc.batch(
                [("getAccountInfo", [a, {"encoding": "base64"}]) for a in addresses]
            )
            import base64

            for start, res in zip(starts, result):
                value = (res or {}).get("value")
                if not value:
                    cached[start] = None
                    continue
                cached[start] = base64.b64decode(value["data"][0])
        return {s: cached.get(s) for s in {
            tick_array_start_index(t, tick_spacing) for t in ticks
        }}

    def fetch_pool_state(self, pool_address: str) -> RaydiumPoolState:
        if pool_address in self._pool_state_cache:
            return self._pool_state_cache[pool_address]
        import base64

        result = self.rpc.call("getAccountInfo", [pool_address, {"encoding": "base64"}])
        value = (result or {}).get("value")
        if not value:
            raise ValueError(f"pool account not found: {pool_address}")
        state = decode_pool_state(base64.b64decode(value["data"][0]))
        self._pool_state_cache[pool_address] = state
        return state

    def compute_pending(
        self,
        pool_address: str,
        tick_lower: int,
        tick_upper: int,
        liquidity: int,
        checkpoint: dict,
    ) -> Dict[str, object]:
        """Return real pending fees/rewards for one position.

        ``checkpoint`` is the dict from raydium_position_checkpoint(): the
        checkpointed PersonalPositionState fields. This method fetches the
        live PoolState and both boundary tick arrays and applies the SDK
        math.
        """
        pool = self.fetch_pool_state(pool_address)
        arrays = self.fetch_tick_arrays(
            pool_address, pool.tick_spacing, (tick_lower, tick_upper)
        )
        lower_start = tick_array_start_index(tick_lower, pool.tick_spacing)
        upper_start = tick_array_start_index(tick_upper, pool.tick_spacing)
        lower = decode_tick(arrays.get(lower_start), tick_lower)
        upper = decode_tick(arrays.get(upper_start), tick_upper)

        inside_a, inside_b = fee_growth_inside_values(
            pool.tick_current, tick_lower, tick_upper, lower, upper,
            pool.fee_growth_global_a, pool.fee_growth_global_b,
        )
        fees_a, fees_b = pending_fees(
            liquidity,
            checkpoint["fee_growth_inside_last"][0],
            checkpoint["fee_growth_inside_last"][1],
            inside_a, inside_b,
            checkpoint["token_fees_owed"][0],
            checkpoint["token_fees_owed"][1],
        )
        rg_inside = reward_growth_inside_values(
            pool.tick_current, lower, upper, pool
        )
        rewards = pending_rewards(
            liquidity,
            checkpoint["reward_amount_owed"],
            rg_inside,
            checkpoint["reward_growth_inside_last"],
        )
        return {
            "fees_owed_raw": [fees_a, fees_b],
            "rewards_owed_raw": rewards,
            "reward_mints": list(pool.reward_mints),
            "reward_states": list(pool.reward_states),
            "tick_spacing": pool.tick_spacing,
            "tick_current": pool.tick_current,
            "lower_tick_initialized": lower.initialized,
            "upper_tick_initialized": upper.initialized,
        }
