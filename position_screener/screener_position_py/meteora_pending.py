"""Meteora DLMM pending fee/reward computation (stdlib only).

Ports the `@meteora-ag/dlmm` SDK position fee/reward math (dist
`DLMM.processPosition` bin loop, `BinLiquidity.fromBin`, `mulShr`,
`deriveBinArray`, `binIdToBinArrayIndex`) to Python so Meteora positions
report live pending amounts instead of the raw per-bin sums captured at the
position account's own fields (`FeeInfo.feeXPending` checkpoints).

Transcribed from the SDK dist on 2026-10-04; verified against the SDK itself
on live mainnet accounts (see /tmp/fee-verify, report missy_cross_dex_fairness).

Meteora model differs from CLMM: fees accrue per bin as fee-per-liquidity-
token. Each position carries, per bin, a checkpoint of that per-token fee
(`FeeInfo.feeXPerTokenComplete`) plus an already-settled residue
(`feeXPending`); pending = share * (bin.stored - checkpoint) >> 64 + pending.
Rewards follow the same pattern against `bin.rewardPerTokenStored`, with the
active bin's reward accumulator advanced to the current time by the caller.

Layouts (packed, after 8-byte anchor discriminator; from the dlmm IDL,
verified against live accounts 2026-10-04):

LbPair (904 bytes):
    function_type         35  (u8: 0=undetermined, 1=liquidity-mining, 2=limit-order)
    active_id             76  (i32)   [also in solana-account-decode skill]
    bin_step              80  (u16)   [also in solana-account-decode skill]
    reward_infos[2] @264, stride 144:
        mint                   +0    (pubkey)
        vault                  +32   (pubkey)
        funder                 +64   (pubkey)
        reward_duration        +96   (u64)
        reward_duration_end    +104  (u64)
        reward_rate            +112  (u128)
        last_update_time       +128  (u64)

BinArray (10136 bytes): index i64 @8, version u8 @16, lb_pair @24,
bins[70] @56, Bin stride 144:
    amount_x                    +0    (u64)
    amount_y                    +8    (u64)
    price                       +16   (u128)
    liquidity_supply            +32   (u128)
    reward_per_token_x          +48   (u128: fulfilled_order_amount_x|y bytes)
    reward_per_token_y          +64   (u128: limit_order_fee_ask|bid bytes)
    fee_amount_x_per_token_stored +80  (u128)
    fee_amount_y_per_token_stored +96  (u128)
    (order_age u136 u32, limit_order_ask_side +140 u8, padding 3)
    reward_per_token reuses the limit-order u64 field bytes as one u128
    (SDK decodeRewardPerTokenStored).

PositionV2 (8120 bytes): lb_pair @8, owner @40,
    liquidity_shares[70] @72   (u128 each)
    reward_infos[70] @1192     stride 48:
        reward_per_token_completes[2]  +0/+16  (u128 each)
        reward_pendings[2]             +32/+40 (u64 each)
    fee_infos[70] @4552        stride 48:
        fee_x_per_token_complete  +0   (u128)
        fee_y_per_token_complete  +16  (u128)
        fee_x_pending             +32  (u64)
        fee_y_pending             +40  (u64)
    lower_bin_id @7912 (i32), upper_bin_id @7916 (i32)
Legacy `Position` accounts (sha256(b"account:Position")[:8]) share this
prefix through upper_bin_id; the pending math only reads the prefix, so both
decode with the same offsets.

Bin-array PDA (SDK deriveBinArray): seeds =
[b"bin_array", lb_pair_bytes, i64 little-endian index (two's complement)].
Bin-array index = floor(bin_id / 70) -- Python // matches the SDK's
divmod-with-negative-fix exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from core.rpc import RpcClient

U128_MASK = (1 << 128) - 1
MAX_BINS_PER_ARRAY = 70
BINS_PER_POSITION = 70
SCALE_OFFSET = 64
DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# LbPair offsets (see module docstring)
_LBPAIR_FUNCTION_TYPE = 35
_LBPAIR_ACTIVE_ID = 76
_LBPAIR_BIN_STEP = 80
_LBPAIR_REWARD_BASE = 264
_REWARD_STRIDE = 144

_BINARRAY_BINS_BASE = 56
_BIN_STRIDE = 144

_POSITION_SHARES_BASE = 72
_POSITION_REWARDS_BASE = 1192
_POSITION_FEES_BASE = 4552
_POSITION_LOWER = 7912
# upper_bin_id i32 lives at 7916 (right after lower@7912) — verified live
# 2026-10-04: bytes 7912..7920 = 3debffff 81ebffff decode as two sane i32s,
# while a 7915 read yields garbage (-1342977 on the live position).
_POSITION_UPPER = 7916


def _account_discriminator(name: str) -> bytes:
    import hashlib

    return hashlib.sha256(b"account:" + name.encode()).digest()[:8]


def _trunc_div(a: int, b: int) -> int:
    """BN.div semantics: truncate toward zero (Python // floors)."""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _mul_shr_down(x: int, y: int, offset: int = SCALE_OFFSET) -> int:
    """SDK mulShr(x, y, offset, Rounding.Down) == trunc(x*y / 2^offset)."""
    return _trunc_div(x * y, 1 << offset)


def bin_id_to_bin_array_index(bin_id: int) -> int:
    """SDK binIdToBinArrayIndex: floor division by 70 (Python // floors)."""
    return bin_id // MAX_BINS_PER_ARRAY


def bin_array_lower_bin_id(bin_array_index: int) -> int:
    return bin_array_index * MAX_BINS_PER_ARRAY


def derive_bin_array(lb_pair: str, index: int) -> str:
    """SDK deriveBinArray: i64 LE seed (two's complement for negatives)."""
    from core.solana import b58decode
    from .raydium_pending import _pda

    seed = (index & ((1 << 64) - 1)).to_bytes(8, "little")
    return _pda(
        [b"bin_array", b58decode(lb_pair), seed],
        b58decode(DLMM_PROGRAM),
    )


@dataclass
class LbPairState:
    active_id: int
    bin_step: int
    function_type: int
    reward_mints: List[str]
    reward_rates: List[int]
    reward_duration_ends: List[int]
    reward_last_update_times: List[int]


@dataclass
class PositionState:
    lb_pair: str
    liquidity_shares: List[int]  # per relative bin, u128
    fee_x_per_token_complete: List[int]
    fee_y_per_token_complete: List[int]
    fee_x_pending: List[int]
    fee_y_pending: List[int]
    reward_per_token_completes: List[Tuple[int, int]]  # per bin
    reward_pendings: List[Tuple[int, int]]  # per bin
    lower_bin_id: int
    upper_bin_id: int


@dataclass
class BinRow:
    """One bin slot as the SDK's BinLiquidity.fromBin surfaces it."""

    bin_id: int
    amount_x: int
    amount_y: int
    liquidity_supply: int
    fee_x_per_token_stored: int
    fee_y_per_token_stored: int
    reward_per_token: Tuple[int, int]


def decode_lb_pair(data: bytes) -> LbPairState:
    if len(data) < 264 + 2 * _REWARD_STRIDE:
        raise ValueError(f"LbPair too short: {len(data)}")
    from core.solana import b58encode

    mints, rates, ends, updates = [], [], [], []
    for i in range(2):
        base = _LBPAIR_REWARD_BASE + i * _REWARD_STRIDE
        mints.append(b58encode(data[base : base + 32]))
        rates.append(int.from_bytes(data[base + 112 : base + 128], "little"))
        ends.append(int.from_bytes(data[base + 104 : base + 112], "little"))
        updates.append(int.from_bytes(data[base + 128 : base + 136], "little"))
    return LbPairState(
        active_id=int.from_bytes(data[_LBPAIR_ACTIVE_ID : _LBPAIR_ACTIVE_ID + 4], "little", signed=True),
        bin_step=int.from_bytes(data[_LBPAIR_BIN_STEP : _LBPAIR_BIN_STEP + 2], "little"),
        function_type=data[_LBPAIR_FUNCTION_TYPE],
        reward_mints=mints,
        reward_rates=rates,
        reward_duration_ends=ends,
        reward_last_update_times=updates,
    )


def decode_position(data: bytes) -> PositionState:
    """Decode PositionV2 (or legacy Position; identical prefix)."""
    expected = 8 + _POSITION_SHARES_BASE
    if len(data) < _POSITION_UPPER + 4:
        raise ValueError(f"Position too short: {len(data)}")
    from core.solana import b58encode

    del expected
    shares = [
        int.from_bytes(data[_POSITION_SHARES_BASE + i * 16 : _POSITION_SHARES_BASE + (i + 1) * 16], "little")
        for i in range(BINS_PER_POSITION)
    ]
    fx, fy, px, py = [], [], [], []
    rptc, pend = [], []
    for i in range(BINS_PER_POSITION):
        fbase = _POSITION_FEES_BASE + i * 48
        fx.append(int.from_bytes(data[fbase : fbase + 16], "little"))
        fy.append(int.from_bytes(data[fbase + 16 : fbase + 32], "little"))
        px.append(int.from_bytes(data[fbase + 32 : fbase + 40], "little"))
        py.append(int.from_bytes(data[fbase + 40 : fbase + 48], "little"))
        rbase = _POSITION_REWARDS_BASE + i * 48
        rptc.append(
            (
                int.from_bytes(data[rbase : rbase + 16], "little"),
                int.from_bytes(data[rbase + 16 : rbase + 32], "little"),
            )
        )
        pend.append(
            (
                int.from_bytes(data[rbase + 32 : rbase + 40], "little"),
                int.from_bytes(data[rbase + 40 : rbase + 48], "little"),
            )
        )
    return PositionState(
        lb_pair=b58encode(data[8:40]),
        liquidity_shares=shares,
        fee_x_per_token_complete=fx,
        fee_y_per_token_complete=fy,
        fee_x_pending=px,
        fee_y_pending=py,
        reward_per_token_completes=rptc,
        reward_pendings=pend,
        lower_bin_id=int.from_bytes(data[_POSITION_LOWER : _POSITION_LOWER + 4], "little", signed=True),
        upper_bin_id=int.from_bytes(data[_POSITION_UPPER : _POSITION_UPPER + 4], "little", signed=True),
    )


def bin_row_from_array(bin_array_data: bytes, bin_id: int) -> Optional[BinRow]:
    """Extract one bin row from a BinArray account covering bin_id."""
    index = int.from_bytes(bin_array_data[8:16], "little", signed=True)
    lower = bin_array_lower_bin_id(index)
    offset = bin_id - lower
    if offset < 0 or offset >= MAX_BINS_PER_ARRAY:
        return None
    base = _BINARRAY_BINS_BASE + offset * _BIN_STRIDE
    if base + _BIN_STRIDE > len(bin_array_data):
        return None
    return BinRow(
        bin_id=bin_id,
        amount_x=int.from_bytes(bin_array_data[base : base + 8], "little"),
        amount_y=int.from_bytes(bin_array_data[base + 8 : base + 16], "little"),
        liquidity_supply=int.from_bytes(bin_array_data[base + 32 : base + 48], "little"),
        # SDK decodeRewardPerTokenStored: the limit-order u64 pairs double as
        # the reward accumulators on non-limit-order pools.
        reward_per_token=(
            int.from_bytes(bin_array_data[base + 48 : base + 64], "little"),
            int.from_bytes(bin_array_data[base + 64 : base + 80], "little"),
        ),
        fee_x_per_token_stored=int.from_bytes(bin_array_data[base + 80 : base + 96], "little"),
        fee_y_per_token_stored=int.from_bytes(bin_array_data[base + 96 : base + 112], "little"),
    )


def empty_bin_row(bin_id: int) -> BinRow:
    """SDK BinLiquidity.empty when the BinArray account does not exist."""
    return BinRow(bin_id, 0, 0, 0, 0, 0, (0, 0))


def is_support_limit_order(pair: LbPairState) -> bool:
    return pair.function_type == 2  # SDK FunctionType.LimitOrder


def compute_fees_and_rewards(
    pair: LbPairState,
    position: PositionState,
    bin_arrays: Dict[str, bytes],
    address_for_bin_id,
    current_timestamp: int,
) -> dict:
    """SDK DLMM.processPosition fee/reward loop, transcribed exactly.

    `bin_arrays` maps derived bin-array address -> raw account bytes
    (missing keys mean the array does not exist on chain -> empty bins).
    `address_for_bin_id` resolves a bin id to its derived array address so
    derivation happens once per array, not once per bin.
    `current_timestamp` is the clock sysvar unix timestamp.
    """
    fee_x = 0
    fee_y = 0
    rewards = [0, 0]
    # SDK processPosition also accumulates the position's per-bin token
    # amounts: posShare * bin reserves / bin liquiditySupply (BN trunc div).
    # liquidity shares alone cannot express amounts; the bin rows can.
    amount_x = 0
    amount_y = 0
    limit_order = is_support_limit_order(pair)

    for bin_id in range(position.lower_bin_id, position.upper_bin_id + 1):
        idx = bin_id - position.lower_bin_id
        if idx < 0 or idx >= len(position.liquidity_shares):
            continue  # extended (>70-bin) positions unsupported
        share = position.liquidity_shares[idx]
        address = address_for_bin_id(bin_id)
        data = bin_arrays.get(address)
        if data is None:
            row = empty_bin_row(bin_id)
        else:
            row = bin_row_from_array(data, bin_id)
            if row is None:
                row = empty_bin_row(bin_id)

        # Fees (not gated on limit-order support, matching the SDK).
        if share == 0:
            new_fee_x = 0
            new_fee_y = 0
        else:
            new_fee_x = _mul_shr_down(
                share >> SCALE_OFFSET,
                row.fee_x_per_token_stored - position.fee_x_per_token_complete[idx],
            )
            new_fee_y = _mul_shr_down(
                share >> SCALE_OFFSET,
                row.fee_y_per_token_stored - position.fee_y_per_token_complete[idx],
            )
        fee_x += new_fee_x + position.fee_x_pending[idx]
        fee_y += new_fee_y + position.fee_y_pending[idx]

        # Token amounts: share * reserves / supply, truncating per bin.
        # A zero supply (or missing bin array -> empty row) contributes 0,
        # matching the SDK's binSupply.eq(ZERO) guard.
        if share != 0 and row.liquidity_supply != 0:
            amount_x += _trunc_div(share * row.amount_x, row.liquidity_supply)
            amount_y += _trunc_div(share * row.amount_y, row.liquidity_supply)

        # Rewards (skipped for limit-order pairs).
        if limit_order:
            continue
        for j in range(2):
            if pair.reward_mints[j] == SYSTEM_PROGRAM or pair.reward_mints[j] == "":
                continue
            reward_per_token = row.reward_per_token[j]
            if bin_id == pair.active_id and row.liquidity_supply != 0:
                current = min(current_timestamp, pair.reward_duration_ends[j])
                delta_t = current - pair.reward_last_update_times[j]
                liquidity_supply = row.liquidity_supply >> SCALE_OFFSET
                # SDK: rate * delta / 15 / liquiditySupply (BN truncating div)
                reward_per_token = reward_per_token + _trunc_div(
                    _trunc_div(pair.reward_rates[j] * delta_t, 15),
                    liquidity_supply,
                )
            delta = reward_per_token - position.reward_per_token_completes[idx][j]
            new_reward = (
                0
                if share == 0
                else _mul_shr_down(delta, share >> SCALE_OFFSET)
            )
            rewards[j] += new_reward + position.reward_pendings[idx][j]

    return {
        "fees_owed_raw": [fee_x, fee_y],
        "rewards_owed_raw": rewards,
        "amount_x_raw": amount_x,
        "amount_y_raw": amount_y,
    }


class MeteoraPendingFeesFetcher:
    """Fetches LbPair + covering BinArray accounts and computes live pending
    fees/rewards. Read-only."""

    def __init__(self, rpc: RpcClient):
        self.rpc = rpc
        # Per-instance caches (mirrors the Raydium fetcher): multiple
        # positions in the same pool share one LbPair fetch and one
        # BinArray batch instead of refetching identical accounts.
        self._lb_pair_cache: Dict[str, LbPairState] = {}
        self._bin_array_cache: Dict[Tuple[str, int], Optional[bytes]] = {}

    def fetch_lb_pair(self, pool_address: str) -> LbPairState:
        cached = self._lb_pair_cache.get(pool_address)
        if cached is not None:
            return cached
        import base64

        result = self.rpc.call("getAccountInfo", [pool_address, {"encoding": "base64"}])
        value = (result or {}).get("value")
        if not value:
            raise ValueError(f"lb pair not found: {pool_address}")
        state = decode_lb_pair(base64.b64decode(value["data"][0]))
        self._lb_pair_cache[pool_address] = state
        return state

    def fetch_bin_arrays(self, pool_address: str, lower_bin_id: int, upper_bin_id: int) -> Dict[str, bytes]:
        import base64

        indexes = sorted(
            {
                bin_id_to_bin_array_index(b)
                for b in (lower_bin_id, upper_bin_id, (lower_bin_id + upper_bin_id) // 2)
            }
        )
        addresses = [derive_bin_array(pool_address, i) for i in indexes]
        missing = [
            (i, addr)
            for i, addr in zip(indexes, addresses)
            if (pool_address, i) not in self._bin_array_cache
        ]
        if missing:
            results = self.rpc.batch(
                [("getAccountInfo", [a, {"encoding": "base64"}]) for _, a in missing]
            )
            for (i, _addr), res in zip(missing, results):
                value = (res or {}).get("value")
                self._bin_array_cache[(pool_address, i)] = (
                    base64.b64decode(value["data"][0]) if value else None
                )
        out: Dict[str, bytes] = {}
        for i, addr in zip(indexes, addresses):
            data = self._bin_array_cache[(pool_address, i)]
            if data:
                out[addr] = data
        return out

    def fetch_clock(self) -> int:
        import base64

        result = self.rpc.call(
            "getAccountInfo",
            ["SysvarC1ock11111111111111111111111111111111", {"encoding": "base64"}],
        )
        value = (result or {}).get("value")
        if not value:
            raise ValueError("clock sysvar unavailable")
        return int.from_bytes(base64.b64decode(value["data"][0])[32:40], "little", signed=True)

    def compute_pending(self, pool_address: str, position_data: bytes) -> dict:
        """Live pending fees/rewards for one Meteora position.

        `position_data` is the raw PositionV2 (or legacy Position) bytes.
        """
        position = decode_position(position_data)
        pool = self.fetch_lb_pair(pool_address)
        address_for = _address_resolver(pool_address, position.lower_bin_id, position.upper_bin_id)
        bin_arrays = self.fetch_bin_arrays(pool_address, position.lower_bin_id, position.upper_bin_id)
        timestamp = self.fetch_clock()
        out = compute_fees_and_rewards(
            pool, position, bin_arrays, address_for, timestamp
        )
        out["reward_mints"] = list(pool.reward_mints)
        return out


def _address_resolver(pool_address: str, lower_bin_id: int, upper_bin_id: int) -> dict:
    """bin_id -> derived bin-array address, computed once per array."""
    mapping: Dict[int, str] = {}
    for b in range(lower_bin_id, upper_bin_id + 1):
        idx = bin_id_to_bin_array_index(b)
        if idx not in mapping:
            mapping[idx] = derive_bin_array(pool_address, idx)

    def resolve(bin_id: int) -> str:
        return mapping[bin_id_to_bin_array_index(bin_id)]

    return resolve
