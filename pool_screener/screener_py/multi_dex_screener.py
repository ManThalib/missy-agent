"""Provider normalization and shared multi-DEX screening policy."""

import base64
import math
import time
from collections import defaultdict
from operator import attrgetter
from typing import Any, Dict, List, Optional, Tuple

from .candidate import Candidate
from .client import MeteoraClient, MultiDexClient, OrcaClient, RaydiumClient
from .config import DEFAULT_CONFIG, FilterConfig
from .normalize_utils import (
    finite_float,
    integer,
    mapping,
    token_entry,
    top_level_token_metadata,
)
from .scoring import PoolScoreInput, PoolScorer
from .whitelist import Whitelist, normalize_symbol
from core.prices import JupiterPriceClient


__all__ = [
    "RaydiumScreener",
    "MultiDexScreener",
    "MeteoraScreener",
    "Whitelist",
    "FilterConfig",
    "DEFAULT_CONFIG",
    "Candidate",
    "RaydiumClient",
    "OrcaClient",
    "MultiDexClient",
    "MeteoraClient",
    "normalize_pool",
]

_ORCA_TIMEFRAMES = {"day": "24h", "week": "7d", "month": "30d"}
_WINDOW_DAYS = {"day": 1.0, "week": 7.0, "month": 30.0}

# Sentinel used to distinguish a missing/unknown tick/bin from the real integer 0.
# Using None in Candidate serialization makes the missing state explicit.
UNKNOWN_TICK: Optional[int] = None


# LbPair byte layout (anchor discriminator + bytemuck struct fields):
#   8..40   parameters   StaticParameters   (32 bytes)
#   40..72  vParameters  VariableParameters (32 bytes)
#   72..73  bumpSeed     [u8; 1]
#   73..75  binStepSeed  [u8; 2]
#   75..76  pairType     u8
#   76..80  activeId     i32 (signed; bins are negative below the mid price)
#   80..82  binStep      u16
# Verified against the anchor IDL (@meteora-ag/dlmm) and live accounts: the
# binStep u16 read at offset 80 matches the API bin_step for every pool.
_ACTIVE_ID_OFFSET = 76

# Orca Whirlpool account: anchor discriminator(8) | whirlpoolsConfig(32)
# | whirlpoolBump(1) | tickSpacing(u16) | tickSpacingSeed(2) | feeRate(u16)
# | protocolFeeRate(u16) | liquidity(u128) | sqrtPrice(u128)
# | tickCurrentIndex i32 @81. Verified against live mainnet accounts:
# ZEC/USDC GTHK... decoded 26726 where the SDK reported ~26191 hours earlier.
_ORCA_TICK_OFFSET = 81

# Raydium CLMM PoolState (zero-copy, repr(packed) — no alignment padding):
# discriminator(8) | bump(1) | ammConfig(32) | owner(32) | tokenMint0(32)
# | tokenMint1(32) | tokenVault0(32) | tokenVault1(32) | observationKey(32)
# | mintDecimals0(1) | mintDecimals1(1) | tickSpacing u16 @235
# | liquidity(u128)@237 | sqrtPriceX64(u128)@253 | tickCurrent i32 @269.
# Verified live on 3 mainnet CLMM pools (2026-09-30): tickSpacing u16@235
# matched (120/60/60), and tickCurrent@269 equals floor(log(sqrtPriceX64^2
# / 2^128)/log(1.0001)) exactly on every account. The old @304 derivation
# assumed 16-byte alignment + [u8;3] bump and always read 0.
_RAYDIUM_TICK_OFFSET = 269


def _decode_i32_at(account_data: bytes, offset: int) -> int:
    if len(account_data) < offset + 4:
        return 0
    return int.from_bytes(account_data[offset : offset + 4], "little", signed=True)


def _decode_active_bin_id(account_data: bytes) -> int:
    """Extract activeId (i32, little-endian) from a Meteora DLMM LbPair account.

    activeId is NOT the first field after the 8-byte discriminator; it sits
    at offset 76, after the 32-byte StaticParameters, 32-byte
    VariableParameters, bumpSeed, binStepSeed, and pairType fields.
    """
    end = _ACTIVE_ID_OFFSET + 4
    if len(account_data) < end:
        return 0
    return int.from_bytes(
        account_data[_ACTIVE_ID_OFFSET:end], "little", signed=True
    )


def _fetch_active_bin_id(rpc: Any, pool_address: str) -> int:
    """Fetch activeId from a Meteora DLMM LbPair account via RPC.

    Returns 0 if the call fails or the field cannot be decoded.
    """
    try:
        result = rpc.get_account_info(pool_address)
        value = result.get("value") if isinstance(result, dict) else None
        data = value.get("data") if isinstance(value, dict) else None
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, str):
            return _decode_active_bin_id(base64.b64decode(data))
        if isinstance(data, bytes):
            return _decode_active_bin_id(data)
        return 0
    except Exception:
        return 0


def _fetch_current_tick(rpc: Any, dex: str, pool_address: str) -> int:
    """Fetch the live current tick via RPC for Orca whirlpools and Raydium CLMM.

    The discovery APIs do not expose tickCurrentIndex/tickCurrent, so the
    pool account is read directly. Returns 0 when unavailable; callers must
    treat 0 as unknown, not as a real tick.
    """
    offset = {"orca": _ORCA_TICK_OFFSET, "raydium": _RAYDIUM_TICK_OFFSET}.get(
        (dex or "").lower()
    )
    if rpc is None or offset is None:
        return 0
    try:
        result = rpc.get_account_info(pool_address)
        value = result.get("value") if isinstance(result, dict) else None
        data = value.get("data") if isinstance(value, dict) else None
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, str):
            return _decode_i32_at(base64.b64decode(data), offset)
        if isinstance(data, bytes):
            return _decode_i32_at(data, offset)
        return 0
    except Exception:
        return 0


def _decode_tick_from_account(dex: str, data: Any) -> Optional[int]:
    """Return decoded tick/bin from a base64 account payload, or None on failure."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, str):
        return UNKNOWN_TICK
    raw = base64.b64decode(data)
    if not raw:
        return UNKNOWN_TICK

    dex = (dex or "").lower()
    if dex == "meteora":
        return _decode_active_bin_id(raw)
    offset = {"orca": _ORCA_TICK_OFFSET, "raydium": _RAYDIUM_TICK_OFFSET}.get(dex)
    if offset is None:
        return UNKNOWN_TICK
    return _decode_i32_at(raw, offset)


def _batch_fetch_ticks(
    rpc: Any,
    requests: List[Tuple[str, str]],
    retries: int = 6,
    backoff_seconds: float = 1.5,
    batch_size: int = 20,
) -> Dict[str, Optional[int]]:
    """Batch fetch current tick/bin for a list of (dex, pool_address) tuples.

    Returns a dict mapping pool_address -> tick/bin value (None if unknown).
    Uses batched getAccountInfo RPC calls with retries on transient failures.
    Defaults are tuned for Helius free-tier rate limits (small batches, longer
    backoff between attempts).
    """
    if not rpc or not requests:
        return {}

    result: Dict[str, Optional[int]] = {}
    pending: List[Tuple[str, str]] = list(requests)

    for attempt in range(retries + 1):
        if not pending:
            break
        # Build chunked batched payloads.
        chunks: List[List[Tuple[int, str, str]]] = [[] for _ in range((len(pending) // batch_size) + 1)]
        for idx, (dex, addr) in enumerate(pending):
            chunks[idx // batch_size].append((idx, dex, addr))
        # Filter empty chunks.
        chunks = [c for c in chunks if c]

        try:
            for chunk in chunks:
                payload = []
                for local_idx, dex, addr in chunk:
                    payload.append({
                        "jsonrpc": "2.0",
                        "id": local_idx + 1,
                        "method": "getAccountInfo",
                        "params": [addr, {"encoding": "base64"}],
                    })
                response = rpc._post(payload)
                if not isinstance(response, list):
                    response = [response]
                # Map responses by local id to request order. Some providers return
                # ids as strings, so coerce keys to int for robust matching.
                by_id = {}
                for item in response:
                    if isinstance(item, dict):
                        key = item.get("id")
                        try:
                            key = int(key)
                        except (TypeError, ValueError):
                            pass
                        by_id[key] = item
                for local_idx, dex, addr in chunk:
                    item = by_id.get(local_idx + 1)
                    if not item or item.get("error"):
                        result.setdefault(addr, UNKNOWN_TICK)
                        continue
                    value = (item.get("result") or {}).get("value")
                    if not isinstance(value, dict):
                        result.setdefault(addr, UNKNOWN_TICK)
                        continue
                    tick = _decode_tick_from_account(dex, value.get("data"))
                    # Keep first known value; do not overwrite known with unknown later.
                    if addr not in result or result[addr] is UNKNOWN_TICK:
                        result[addr] = tick
                # Pause between chunks to respect provider rate limits.
                time.sleep(0.25)
        except Exception:
            # On failure, leave entries pending for retry.
            pass

        # Anything we now know is no longer pending.
        pending = [(dex, addr) for (dex, addr) in pending if addr not in result or result[addr] is UNKNOWN_TICK]

        if pending and attempt < retries:
            time.sleep(backoff_seconds * (2 ** attempt))

    # Anything still pending after retries is unknown.
    for _, addr in pending:
        result.setdefault(addr, UNKNOWN_TICK)
    return result


def _volatility(price: float, price_min: float, price_max: float) -> float:
    if price > 0 and price_min > 0 and price_max > 0:
        return abs(price_max - price_min) / price * 100.0
    return 0.0


def _yield_over_tvl_apr(fee_usd: float, tvl_usd: float, window: str) -> float:
    """Annualize fee/tvl the same way Orca does for 24h/7d/30d windows."""
    if tvl_usd <= 0.0 or fee_usd <= 0.0:
        return 0.0
    days = _WINDOW_DAYS.get(window, 1.0)
    return (fee_usd / tvl_usd) * 100.0 * (365.0 / days)


def _apply_price_map(token_data: Dict[str, Any], price_map: Dict[str, float]) -> Dict[str, Any]:
    address = token_data.get("address")
    if address and address in price_map:
        token_data = dict(token_data)
        token_data["price_usd"] = price_map[address]
    return token_data


def _range_volatility(price: float, price_min: float, price_max: float) -> Tuple[float, bool]:
    """Common range volatility = (max - min) / price * 100."""
    if price > 0 and price_min > 0 and price_max > 0:
        return (price_max - price_min) / price * 100.0, True
    return 0.0, False


def _price_history_volatility(current_price: float, prices: List[float], window: str) -> Tuple[float, bool]:
    """Range volatility from a list of historical prices."""
    if current_price <= 0 or not prices:
        return 0.0, False
    # Use the most recent N points per window.
    window_points = {
        "day": 2,  # ~24h if sampled every 12h
        "week": len(prices),  # all available ~7d points
        "month": len(prices),  # no 30d history available, fall back to all
    }.get(window, len(prices))
    window_points = min(window_points, len(prices))
    window_prices = prices[-window_points:] + [current_price]
    if not window_prices:
        return 0.0, False
    return (max(window_prices) - min(window_prices)) / current_price * 100.0, True


def _common_apr(dex: str, fee_usd: float, tvl_usd: float, window: str, reported_apr: float) -> Tuple[float, str]:
    """Return APR using a common fee/tvl annualisation, with provider fallback."""
    if tvl_usd > 0 and fee_usd > 0:
        apr = _yield_over_tvl_apr(fee_usd, tvl_usd, window)
        return apr, f"{dex}_fee_over_tvl_{window}"
    if reported_apr:
        return reported_apr, "provider_reported"
    return 0.0, "unavailable"


def normalize_pool(pool: Dict[str, Any], window: str = "day", price_map: Dict[str, float] = None) -> Dict[str, Any]:
    """Converts a provider pool into the common shape used by the screener."""
    if not isinstance(pool, dict):
        raise ValueError("pool payload must be an object")

    if "mintA" in pool or "mintB" in pool:
        mint_a = _apply_price_map(mapping(pool.get("mintA")), price_map or {})
        mint_b = _apply_price_map(mapping(pool.get("mintB")), price_map or {})
        token_x = token_entry(mint_a)
        token_y = token_entry(mint_b)
        name = f"{mint_a.get('symbol', '?')}-{mint_b.get('symbol', '?')}"
        pool_address = pool.get("id", "")
        pool_type = pool.get("type", "")
        tvl = finite_float(pool.get("tvl"), "TVL")
        fee_rate = finite_float(pool.get("feeRate"), "fee rate")

        period = mapping(pool.get(window) or pool.get("day"))
        volume = finite_float(period.get("volume"), "volume")
        volume_fee = finite_float(period.get("volumeFee"), "fees")
        reported_apr = finite_float(period.get("apr"), "APR") or finite_float(
            period.get("feeApr"), "fee APR"
        )
        apr, apr_source = _common_apr(
            "raydium", volume_fee, tvl, window, reported_apr
        )
        price = finite_float(pool.get("price"), "price")
        price_min = finite_float(period.get("priceMin"), "minimum price")
        price_max = finite_float(period.get("priceMax"), "maximum price")
        volatility, volatility_available = _range_volatility(price, price_min, price_max)

        fee_tvl_ratio = (volume_fee / tvl * 100.0) if tvl > 0 and volume_fee else 0.0

        provider_config = mapping(pool.get("config"))
        tick_spacing = integer(provider_config.get("tickSpacing"), "tick spacing")

        return {
            "dex": pool.get("_dex", "raydium"),
            "pool_address": pool_address,
            "name": name,
            "provider_name": name,
            "pool_type": pool_type,
            "tvl": tvl,
            "fee_tvl_ratio": fee_tvl_ratio,
            "fee": volume_fee,
            "volume": volume,
            "volatility": volatility,
            "volatility_available": volatility_available,
            "volatility_source": f"raydium_{window}_range" if volatility_available else "raydium_unavailable",
            "volatility_window": window,
            "apr": apr,
            "apr_source": apr_source,
            "apr_window": window,
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            # Orca/Raydium are tick-based, not bin-based; keep bin_step honest.
            "bin_step": 0,
            "token_x": token_x,
            "token_y": token_y,
            "pool_price": price,
            "active_bin_id": 0,
            "current_tick_index": 0,
            **top_level_token_metadata(token_x, token_y),
        }

    if pool.get("_dex") == "orca" or "tvlUsdc" in pool:
        token_a = token_entry(
            _apply_price_map(mapping(pool.get("tokenA")), price_map or {}),
            default_address=pool.get("tokenMintA", ""),
        )
        token_b = token_entry(
            _apply_price_map(mapping(pool.get("tokenB")), price_map or {}),
            default_address=pool.get("tokenMintB", ""),
        )
        timeframe = _ORCA_TIMEFRAMES.get(window, "24h")
        stats = mapping(mapping(pool.get("stats")).get(timeframe))
        tvl = finite_float(pool.get("tvlUsdc"), "TVL")
        fees = finite_float(stats.get("fees"), "fees")
        price_history = pool.get("priceHistory7d") or []
        if not isinstance(price_history, list):
            raise ValueError("invalid price history: expected a list")
        prices = [
            finite_float(value, "price history value")
            for value in price_history
            if value is not None
        ]
        current_price = finite_float(pool.get("price"), "price")
        volatility, volatility_available = _price_history_volatility(
            current_price, prices, window
        )
        orca_name = f"{token_a['symbol']}-{token_b['symbol']}"
        fee_rate = finite_float(pool.get("feeRate"), "fee rate") / 1_000_000.0
        tick_spacing = integer(pool.get("tickSpacing"), "tick spacing")
        current_tick_index = integer(
            pool.get("tickCurrentIndex")
            or pool.get("currentTickIndex")
            or pool.get("current_tick_index")
            or 0,
            "tickCurrentIndex",
        )
        reported_apr = finite_float(stats.get("yieldOverTvl"), "yield over TVL") * 100.0 * (
            365.0
            if timeframe == "24h"
            else 365.0 / (7.0 if timeframe == "7d" else 30.0)
        )
        apr, apr_source = _common_apr("orca", fees, tvl, window, reported_apr)
        return {
            "dex": "orca",
            "pool_address": pool.get("address", ""),
            "name": orca_name,
            "provider_name": orca_name,
            "pool_type": "Splash" if tick_spacing == 32896 else "Whirlpool",
            "tvl": tvl,
            "fee_tvl_ratio": (fees / tvl * 100.0) if tvl > 0 else 0.0,
            "fee": fees,
            "volume": finite_float(stats.get("volume"), "volume"),
            "volatility": volatility,
            "volatility_available": volatility_available,
            "volatility_source": f"orca_price_history_7d_{window}" if volatility_available else "orca_unavailable",
            "volatility_window": window,
            "apr": apr,
            "apr_source": apr_source,
            "apr_window": window,
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            "bin_step": 0,
            "token_x": token_a,
            "token_y": token_b,
            "current_tick_index": current_tick_index,
            **top_level_token_metadata(token_a, token_b),
            "pool_price": current_price,
            # Orca uses ticks; bin_step is a Meteora-only concept.
            "active_bin_id": 0,
        }

    if pool.get("_dex") == "meteora" or "dlmm_params" in pool:
        normalized = dict(pool)
        normalized["dex"] = "meteora"
        normalized["pool_type"] = "DLMM"
        tick_spacing = integer(
            mapping(pool.get("dlmm_params")).get("bin_step"), "bin step"
        )
        normalized["tick_spacing"] = tick_spacing
        normalized["bin_step"] = tick_spacing
        for field in ("tvl", "fee", "volume", "apr", "fee_pct"):
            normalized[field] = finite_float(pool.get(field), field)
        normalized.setdefault("fee_rate", normalized["fee_pct"] / 100.0)
        current_price = finite_float(
            pool.get("pool_price", pool.get("price")), "price"
        )
        price_min = finite_float(pool.get("min_price"), "minimum price")
        price_max = finite_float(pool.get("max_price"), "maximum price")
        volatility, volatility_available = _range_volatility(current_price, price_min, price_max)
        if "volatility" in pool and volatility == 0.0:
            volatility = finite_float(pool.get("volatility"), "volatility")
            volatility_available = True
        normalized["volatility"] = volatility
        normalized["volatility_available"] = volatility_available
        normalized["volatility_source"] = f"meteora_{window}_range" if volatility_available else "meteora_unavailable"
        normalized["volatility_window"] = window
        reported_apr = finite_float(pool.get("apr"), "APR")
        apr, apr_source = _common_apr("meteora", normalized["fee"], normalized["tvl"], window, reported_apr)
        normalized["apr"] = apr
        normalized["apr_source"] = apr_source
        normalized["apr_window"] = window
        token_x = token_entry(_apply_price_map(mapping(pool.get("token_x")), price_map or {}))
        token_y = token_entry(_apply_price_map(mapping(pool.get("token_y")), price_map or {}))
        normalized["token_x"] = token_x
        normalized["token_y"] = token_y
        normalized.update(top_level_token_metadata(token_x, token_y))
        meteora_name = f"{token_x['symbol']}-{token_y['symbol']}"
        normalized["name"] = meteora_name
        normalized["provider_name"] = meteora_name
        normalized["active_bin_id"] = integer(
            pool.get("activeId") or pool.get("active_bin_id") or 0, "activeId"
        )
        normalized["current_tick_index"] = normalized["active_bin_id"]
        return normalized

    # Already normalized (or legacy Meteora shape); pass it through.
    return dict(pool)


class MultiDexScreener:
    """Evaluate normalized DEX pools against token and economic policies."""

    def __init__(
        self,
        whitelist: Whitelist,
        config: Optional[FilterConfig] = None,
        client: Optional[Any] = None,
        rpc: Optional[Any] = None,
    ):
        self.whitelist = whitelist
        self.config = config or FilterConfig()
        if client is None:
            target_mints = list(whitelist.targets.addresses)
            client = MultiDexClient(timeout=self.config.timeout, target_mints=target_mints)
        self.client = client
        self.rpc = rpc
        self.scorer = PoolScorer()
        self._jupiter_client: Optional[JupiterPriceClient] = None

    def _fetch_pool_prices(
        self, pools: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """Collect token mints from raw pools and fetch Jupiter USD prices."""
        mints: set = set()
        for pool in pools:
            try:
                n = normalize_pool(pool, window=self.config.window)
            except (TypeError, ValueError, OverflowError):
                # Malformed payload: skip price collection here; screen_all
                # still rejects the pool with a reason (AGENTS.md policy).
                continue
            for key in ("token_x", "token_y"):
                token = n.get(key) or {}
                address = token.get("address")
                if address:
                    mints.add(address)
        if not mints:
            return {}
        try:
            if self._jupiter_client is None:
                cache = getattr(self.client, "cache", None)
                self._jupiter_client = JupiterPriceClient()
                if cache is not None:
                    self._jupiter_client.cache = cache
            return self._jupiter_client.fetch_prices(mints)
        except Exception:
            return {}

    def _inject_token_prices(
        self, pool: Dict[str, Any], price_map: Dict[str, float]
    ) -> None:
        """Mutate a raw pool payload so its token objects carry USD prices."""
        if not price_map:
            return

        def _apply(token_obj: Any, price: float) -> None:
            if isinstance(token_obj, dict):
                token_obj["price_usd"] = price

        try:
            n = normalize_pool(pool, window=self.config.window)
        except (TypeError, ValueError, OverflowError):
            # Malformed payload: no addresses to annotate; screen_pool/screen_all
            # reports the reject reason (AGENTS.md policy).
            return

        for key in ("token_x", "token_y"):
            token = n.get(key) or {}
            address = token.get("address")
            if not address or address not in price_map:
                continue
            price = price_map[address]
            if key == "token_x":
                if "token_x" in pool:
                    _apply(pool.get("token_x"), price)
                if "mintA" in pool:
                    _apply(pool.get("mintA"), price)
                if "tokenA" in pool:
                    _apply(pool.get("tokenA"), price)
            else:
                if "token_y" in pool:
                    _apply(pool.get("token_y"), price)
                if "mintB" in pool:
                    _apply(pool.get("mintB"), price)
                if "tokenB" in pool:
                    _apply(pool.get("tokenB"), price)

    def screen_pool(self, p: Dict[str, Any], price_map: Dict[str, float] = None, tick_map: Dict[str, Optional[int]] = None) -> Tuple[Optional[Candidate], str]:
        """Normalize and enrich a raw pool, then emit it as a fact.

        Policy filtering (whitelist, TVL/volume bands, scoring, etc.) is now
        owned by Sheldon. Missy only supplies clean, normalized observations.
        """
        return self._screen_pool_internal(p, price_map=price_map, tick_map=tick_map)

    def _screen_pool_internal(
        self,
        p: Dict[str, Any],
        price_map: Dict[str, float] = None,
        tick_map: Dict[str, Optional[int]] = None,
        n: Optional[Dict[str, Any]] = None,
        target: Optional[Dict[str, Any]] = None,
        paired: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Candidate], str]:
        if n is None:
            n = normalize_pool(p, window=self.config.window, price_map=price_map)

        token_x = n.get("token_x") or {}
        token_y = n.get("token_y") or {}

        # Apply the target/paired token policy up front so we do not waste
        # work pricing or enriching pools we do not care about.
        if target is None or paired is None:
            ok, target, paired, reason = self.whitelist.validate_pair(token_x, token_y)
            if not ok:
                return None, reason

        # Canonicalise name and orient pool_price to whitelisted/paired.
        whitelisted_symbol = normalize_symbol(target.get("symbol", ""))
        paired_symbol = normalize_symbol(paired.get("symbol", ""))
        canonical_name = f"{whitelisted_symbol}-{paired_symbol}"
        provider_name = n.get("provider_name", n.get("name", ""))

        pool_price_raw = float(n.get("pool_price") or 0.0)
        token_x_address = n.get("token_x_address", "")
        target_address = target.get("address", "")
        if target_address and token_x_address and target_address == token_x_address:
            # target is token_x; provider price is token_y/token_x = paired/target.
            oriented_price = 1.0 / pool_price_raw if pool_price_raw > 0.0 else 0.0
        else:
            # target is token_y; provider price is already target/paired.
            oriented_price = pool_price_raw

        tvl = float(n.get("tvl") or 0.0)
        volume_window = float(n.get("volume") or 0.0)
        window_days = _WINDOW_DAYS.get(self.config.window, 1.0)
        gross_fee_window = float(n.get("fee") or 0.0)
        if gross_fee_window <= 0.0:
            gross_fee_window = tvl * (float(n.get("fee_tvl_ratio") or 0.0) / 100.0)
        lp_fee_share = max(0.0, min(1.0, float(n.get("lp_fee_share", 1.0))))
        daily_fee_usd = gross_fee_window * lp_fee_share / window_days
        fee_tvl_ratio = (daily_fee_usd / tvl * 100.0) if tvl > 0.0 else 0.0
        tick_spacing = int(n.get("tick_spacing") or n.get("bin_step") or 0)
        pool_type = str(n.get("pool_type") or "")
        fee_pct = float(n.get("fee_pct") or 0.0)
        volatility = float(n.get("volatility") or 0.0)
        apr = float(n.get("apr") or 0.0)

        # Determine current tick/bin. Prefer discovery API values, fall back
        # to a pre-fetched batched RPC lookup, and use None as the unknown
        # sentinel so that 0 is not misinterpreted as a missing value.
        active_bin_id: Optional[int] = UNKNOWN_TICK
        current_tick_index: Optional[int] = UNKNOWN_TICK
        tick_source = "none"
        dex_name = str(n.get("dex") or "")
        pool_address = n.get("pool_address", "")

        if pool_type.lower() == "standard":
            # Standard AMM pools have no meaningful tick/bin.
            active_bin_id = 0
            current_tick_index = 0
        elif dex_name == "meteora" and pool_type == "DLMM":
            api_bin = n.get("active_bin_id")
            if api_bin not in (None, 0, "0", ""):
                active_bin_id = int(api_bin)
                tick_source = "discovery_api"
            else:
                prefetched = (tick_map or {}).get(pool_address)
                if prefetched is not UNKNOWN_TICK:
                    active_bin_id = prefetched
                    tick_source = "rpc_batched"
                elif self.rpc:
                    # Fallback single fetch after a short cooldown to recover
                    # from any Helius rate-limiting that hit the batched phase.
                    time.sleep(0.15)
                    try:
                        raw = _fetch_active_bin_id(self.rpc, pool_address)
                        if raw == 0:
                            active_bin_id = UNKNOWN_TICK
                        else:
                            active_bin_id = raw
                            tick_source = "rpc_single"
                    except Exception:
                        active_bin_id = UNKNOWN_TICK
            # `current_tick_index` is a 1bp CLMM tick; a Meteora bin id is
            # not a tick. Keep it unknown here and expose a cross-DEX
            # comparable value via `current_tick_equivalent` below.
            current_tick_index = UNKNOWN_TICK
        elif dex_name in ("orca", "raydium"):
            api_tick = n.get("current_tick_index")
            if api_tick not in (None, 0, "0", ""):
                current_tick_index = int(api_tick)
                tick_source = "discovery_api"
            else:
                prefetched = (tick_map or {}).get(pool_address)
                if prefetched is not UNKNOWN_TICK:
                    current_tick_index = prefetched
                    tick_source = "rpc_batched"
                elif self.rpc:
                    # Fallback single fetch after a short cooldown to recover
                    # from any Helius rate-limiting that hit the batched phase.
                    time.sleep(0.15)
                    try:
                        raw = _fetch_current_tick(self.rpc, dex_name, pool_address)
                        if raw == 0:
                            current_tick_index = UNKNOWN_TICK
                        else:
                            current_tick_index = raw
                            tick_source = "rpc_single"
                    except Exception:
                        current_tick_index = UNKNOWN_TICK

        token_x_price_usd = float(n.get("token_x_price_usd") or 0.0)
        token_y_price_usd = float(n.get("token_y_price_usd") or 0.0)

        # Derive missing token prices using the *provider-raw* pool price
        # (token_y/token_x). The oriented price is emitted separately.
        if token_x_price_usd == 0.0 and token_y_price_usd > 0.0 and pool_price_raw > 0.0:
            token_x_price_usd = token_y_price_usd * pool_price_raw
        elif token_y_price_usd == 0.0 and token_x_price_usd > 0.0 and pool_price_raw > 0.0:
            token_y_price_usd = token_x_price_usd / pool_price_raw

        # `bin_step` is strictly a Meteora DLMM concept (basis points). Tick-
        # based DEXes report 0 so consumers do not confuse tick spacing with
        # bin step. The native spacing value stays in `tick_spacing`.
        bin_step = tick_spacing if dex_name == "meteora" else 0

        # Cross-DEX tick/bin unit and a DEX-agnostic comparable view.
        if pool_type.lower() == "standard":
            tick_unit = "none"
        elif dex_name == "meteora":
            tick_unit = "bin"
        elif dex_name in ("orca", "raydium"):
            tick_unit = "tick"
        else:
            tick_unit = "none"

        current_price_ratio = 0.0
        current_tick_equivalent: Optional[int] = UNKNOWN_TICK
        try:
            if tick_unit == "tick" and current_tick_index is not None:
                current_price_ratio = 1.0001 ** current_tick_index
                current_tick_equivalent = current_tick_index
            elif tick_unit == "bin" and active_bin_id is not None and tick_spacing > 0:
                current_price_ratio = (1.0 + tick_spacing / 10000.0) ** active_bin_id
                current_tick_equivalent = int(
                    round(math.log(current_price_ratio) / math.log(1.0001))
                )
        except (OverflowError, ValueError):
            current_price_ratio = 0.0
            current_tick_equivalent = UNKNOWN_TICK

        price_source = "jupiter_v2" if (token_x_price_usd > 0.0 or token_y_price_usd > 0.0) else "unavailable"
        candidate = Candidate(
            pool_address=n.get("pool_address", ""),
            name=canonical_name,
            provider_name=provider_name,
            whitelisted_token_symbol=whitelisted_symbol,
            whitelisted_token_address=target.get("address", ""),
            paired_token_symbol=paired_symbol,
            tvl=tvl,
            fee_tvl_ratio=fee_tvl_ratio,
            daily_fee_usd=daily_fee_usd,
            volume_window=volume_window,
            bin_step=bin_step,
            fee_pct=fee_pct,
            volatility=volatility,
            # Scoring is now Sheldon's job; Missy emits a neutral placeholder.
            score=0.0,
            dex=str(n.get("dex") or "raydium"),
            pool_type=pool_type,
            apr=apr,
            apr_source=n.get("apr_source", ""),
            apr_window=n.get("apr_window", ""),
            fee_rate=float(n.get("fee_rate") or 0.0),
            tick_spacing=tick_spacing,
            active_bin_id=active_bin_id,
            current_tick_index=current_tick_index,
            tick_unit=tick_unit,
            tick_source=tick_source,
            current_price_ratio=current_price_ratio,
            current_tick_equivalent=current_tick_equivalent,
            effective_tvl=tvl,
            realized_fee_apr=0.0,
            adjusted_apr=0.0,
            yield_score=0.0,
            depth_score=0.0,
            efficiency_score=0.0,
            risk_score=0.0,
            lp_fee_share=lp_fee_share,
            pool_price=oriented_price,
            pool_price_raw=pool_price_raw,
            token_x_address=n.get("token_x_address", ""),
            token_x_decimals=int(n.get("token_x_decimals") or 0),
            token_x_price_usd=token_x_price_usd,
            token_y_address=n.get("token_y_address", ""),
            token_y_decimals=int(n.get("token_y_decimals") or 0),
            token_y_price_usd=token_y_price_usd,
            volatility_source=n.get("volatility_source", ""),
            volatility_window=n.get("volatility_window", ""),
            price_source=price_source,
            pending_fees_source="",
        )
        return candidate, ""

    def screen_all(
        self, pools: List[Dict[str, Any]]
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        candidates: List[Candidate] = []
        rejects: Dict[str, int] = {}

        # Phase 1: cheaply normalize and enforce the whitelist before we spend
        # money/time on Jupiter prices and RPC tick/bin enrichment.
        kept: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
        for pool in pools:
            try:
                n = normalize_pool(pool, window=self.config.window)
            except (TypeError, ValueError, OverflowError) as exc:
                rejects[f"invalid provider payload: {exc}"] = rejects.get(f"invalid provider payload: {exc}", 0) + 1
                continue
            token_x = n.get("token_x") or {}
            token_y = n.get("token_y") or {}
            ok, target, paired, reason = self.whitelist.validate_pair(token_x, token_y)
            if not ok:
                rejects[reason] = rejects.get(reason, 0) + 1
                continue
            kept.append((pool, n, target, paired))

        # Only price and enrich pools that matched the whitelist.
        price_map = self._fetch_pool_prices([pool for pool, *_ in kept])
        tick_map = self._prefetch_current_ticks([pool for pool, *_ in kept])

        for pool, n, target, paired in kept:
            try:
                # Phase-1 normalization ran before we fetched Jupiter prices,
                # so all token USD prices were 0. Re-normalize with the fetched
                # price map so the candidate carries real token prices.
                n = normalize_pool(pool, window=self.config.window, price_map=price_map)
                candidate, reason = self._screen_pool_internal(
                    pool, price_map=price_map, tick_map=tick_map,
                    n=n, target=target, paired=paired,
                )
            except (TypeError, ValueError, OverflowError) as exc:
                reason = f"invalid provider payload: {exc}"
                candidate = None
            if candidate:
                candidates.append(candidate)
            elif reason:
                rejects[reason] = rejects.get(reason, 0) + 1
        candidates.sort(key=attrgetter("score"), reverse=True)
        return candidates, rejects

    def _prefetch_current_ticks(
        self, pools: List[Dict[str, Any]]
    ) -> Dict[str, Optional[int]]:
        """Return a map of pool_address -> tick/bin, fetching via batched RPC when needed."""
        if not self.rpc:
            return {}

        requests: List[Tuple[str, str]] = []
        for pool in pools:
            n = self._normalize_for_tick_prefetch(pool)
            if n is None:
                continue
            dex, pool_type, pool_address, has_api_tick = n
            if not pool_address:
                continue
            # Only fetch for concentrated liquidity pools.
            if pool_type.lower() == "standard":
                continue
            # Trust API-provided tick for non-Meteora pools unless it is missing.
            if dex in ("orca", "raydium") and has_api_tick:
                continue
            # Always fetch Meteora DLMM active bin via RPC.
            requests.append((dex, pool_address))

        return _batch_fetch_ticks(self.rpc, requests)

    @staticmethod
    def _normalize_for_tick_prefetch(pool: Dict[str, Any]) -> Optional[Tuple[str, str, str, bool]]:
        """Return (dex, pool_type, pool_address, has_api_tick) for prefetch decisions."""
        if not isinstance(pool, dict):
            return None
        n = normalize_pool(pool)
        dex = str(n.get("dex") or "").lower()
        pool_type = str(n.get("pool_type") or "").lower()
        pool_address = n.get("pool_address", "")
        if not dex or not pool_address:
            return None

        # Check whether a non-zero tick was already provided by the discovery API.
        api_tick = n.get("current_tick_index")
        has_api_tick = api_tick not in (None, 0, "0", "")
        if dex == "meteora":
            api_tick = n.get("active_bin_id")
            has_api_tick = api_tick not in (None, 0, "0", "")
        return dex, pool_type, pool_address, has_api_tick

    def fetch_and_screen(
        self, max_pages: int = 5
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        pools = self.client.fetch_pools(self.config, max_pages=max_pages)
        return self.screen_all(pools)


# Compatibility aliases for existing integrations.
RaydiumScreener = MultiDexScreener
MeteoraScreener = MultiDexScreener
