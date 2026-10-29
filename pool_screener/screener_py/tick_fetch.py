"""Current tick/bin lookup via Solana RPC for Meteora, Orca, and Raydium pools.

Discovery APIs do not always expose the live tick (Orca, Raydium) or active
bin (Meteora), so the pool account is read directly. ``None`` means unknown;
``0`` is a real on-chain value and must never be used as a failure marker.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Optional, Tuple

from core.retry import backoff_delay
from core.rpc import RpcRateLimited

UNKNOWN_TICK: Optional[int] = None

# LbPair byte layout (anchor discriminator + bytemuck struct fields):
#   8..40   parameters   StaticParameters   (32 bytes)
#   40..72  vParameters  VariableParameters (32 bytes)
#   72..73  bumpSeed     [u8; 1]
#   73..75  binStepSeed  [u8; 2]
#   75..76  pairType     u8
#   76..80  activeId     i32 (signed; bins are negative below the mid price)
#   80..82  binStep      u16
# Verified against the anchor IDL (@meteora-ag/dlmm) and live accounts.
_ACTIVE_ID_OFFSET = 76

# Orca Whirlpool account: anchor discriminator(8) | whirlpoolsConfig(32)
# | whirlpoolBump(1) | tickSpacing(u16) | tickSpacingSeed(2) | feeRate(u16)
# | protocolFeeRate(u16) | liquidity(u128) | sqrtPrice(u128)
# | tickCurrentIndex i32 @81. Verified against live mainnet accounts.
_ORCA_TICK_OFFSET = 81

# Raydium CLMM PoolState (zero-copy, repr(packed)): tickCurrent i32 @269.
# Verified live on 3 mainnet CLMM pools (2026-09-30).
_RAYDIUM_TICK_OFFSET = 269
_TICK_OFFSETS = {"orca": _ORCA_TICK_OFFSET, "raydium": _RAYDIUM_TICK_OFFSET}

# Total planned backoff sleep per batch lookup. A dead RPC must not stall the
# scan for minutes before per-pool fallbacks are even skipped.
BATCH_BUDGET_SECONDS = 30.0
_CHUNK_PAUSE_SECONDS = 0.25


def _decode_i32_at(account_data: bytes, offset: int) -> Optional[int]:
    """Decode a little-endian i32, or None when the payload is truncated."""
    if len(account_data) < offset + 4:
        return None
    return int.from_bytes(account_data[offset : offset + 4], "little", signed=True)


def _decode_active_bin_id(account_data: bytes) -> Optional[int]:
    """Extract activeId (i32, little-endian) from a Meteora DLMM LbPair account."""
    return _decode_i32_at(account_data, _ACTIVE_ID_OFFSET)


def _decode_tick_from_account(dex: str, data: Any) -> Optional[int]:
    """Decode the tick/bin from an account ``data`` field (base64 str, list, or bytes)."""
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, str):
        raw = base64.b64decode(data)
    elif isinstance(data, bytes):
        raw = data
    else:
        return UNKNOWN_TICK
    if not raw:
        return UNKNOWN_TICK
    dex = (dex or "").lower()
    if dex == "meteora":
        return _decode_active_bin_id(raw)
    offset = _TICK_OFFSETS.get(dex)
    if offset is None:
        return UNKNOWN_TICK
    return _decode_i32_at(raw, offset)


def _fetch_tick(rpc: Any, dex: str, pool_address: str) -> Optional[int]:
    """Read one pool account and decode its tick/bin; RpcRateLimited propagates."""
    if rpc is None:
        return UNKNOWN_TICK
    try:
        result = rpc.get_account_info(pool_address)
    except RpcRateLimited:
        raise
    except Exception:
        return UNKNOWN_TICK
    value = result.get("value") if isinstance(result, dict) else None
    data = value.get("data") if isinstance(value, dict) else None
    return _decode_tick_from_account(dex, data)


def _fetch_tick_with_retry(
    fetch_fn,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> Optional[int]:
    """Run a tick/bin fetch callable with jittered backoff between failures.

    Retries on RpcRateLimited (honoring its Retry-After hint) and on any other
    transient failure. Returns the first non-None value, or None when every
    attempt fails.
    """
    retry_after: Optional[float] = None
    for attempt in range(attempts):
        try:
            value = fetch_fn()
            if value is not None:
                return value
        except RpcRateLimited as exc:
            retry_after = exc.retry_after
        except Exception:
            pass
        if attempt < attempts - 1:
            time.sleep(backoff_delay(attempt, base_delay, retry_after))
    return UNKNOWN_TICK


def _batch_fetch_ticks(
    rpc: Any,
    requests: List[Tuple[str, str]],
    retries: int = 6,
    backoff_seconds: float = 1.5,
    batch_size: int = 20,
    budget_seconds: float = BATCH_BUDGET_SECONDS,
) -> Dict[str, Optional[int]]:
    """Batch-fetch the current tick/bin for ``(dex, pool_address)`` pairs.

    Returns ``{pool_address: tick_or_bin_or_None}``. Each chunk is a batch RPC
    call with independent per-item results, so one failing account does not
    fail its neighbours. A chunk-level failure leaves its entries pending for
    the next attempt. HTTP 429 stops the current attempt and feeds the next
    backoff sleep. Total planned sleep is capped by *budget_seconds*.
    """
    if not rpc or not requests:
        return {}

    result: Dict[str, Optional[int]] = {}
    pending: List[Tuple[str, str]] = list(dict.fromkeys(requests))
    retry_after_hint: Optional[float] = None
    planned_sleep = 0.0

    for attempt in range(retries + 1):
        if not pending:
            break
        chunks = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
        for index, chunk in enumerate(chunks):
            calls = [("getAccountInfo", [addr, {"encoding": "base64"}]) for _, addr in chunk]
            try:
                outcomes = rpc.batch_partial(calls)
            except RpcRateLimited as exc:
                if exc.retry_after and exc.retry_after > 0:
                    retry_after_hint = float(exc.retry_after)
                break
            except Exception:
                continue
            for (dex, addr), (value_result, error) in zip(chunk, outcomes):
                value = value_result.get("value") if isinstance(value_result, dict) else None
                if error or not isinstance(value, dict):
                    result.setdefault(addr, UNKNOWN_TICK)
                    continue
                tick = _decode_tick_from_account(dex, value.get("data"))
                # Keep the first known value; never overwrite known with unknown.
                if result.get(addr) is UNKNOWN_TICK:
                    result[addr] = tick
            if index < len(chunks) - 1:
                time.sleep(_CHUNK_PAUSE_SECONDS)

        pending = [(dex, addr) for dex, addr in pending if result.get(addr) is UNKNOWN_TICK]
        if not pending or attempt >= retries:
            break
        delay = backoff_delay(attempt, backoff_seconds, retry_after_hint)
        if planned_sleep + delay > budget_seconds:
            break
        time.sleep(delay)
        planned_sleep += delay
        retry_after_hint = None

    for _, addr in pending:
        result.setdefault(addr, UNKNOWN_TICK)
    return result
