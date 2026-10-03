"""Provider normalization and shared multi-DEX screening policy."""

import base64
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
        apr = _yield_over_tvl_apr(volume_fee, tvl, window)
        if not apr and reported_apr:
            apr = reported_apr
        price = finite_float(pool.get("price"), "price")
        price_min = finite_float(period.get("priceMin"), "minimum price")
        price_max = finite_float(period.get("priceMax"), "maximum price")
        volatility = _volatility(price, price_min, price_max)

        fee_tvl_ratio = (volume_fee / tvl * 100.0) if tvl > 0 and volume_fee else 0.0

        provider_config = mapping(pool.get("config"))
        tick_spacing = integer(provider_config.get("tickSpacing"), "tick spacing")

        return {
            "dex": pool.get("_dex", "raydium"),
            "pool_address": pool_address,
            "name": name,
            "pool_type": pool_type,
            "tvl": tvl,
            "fee_tvl_ratio": fee_tvl_ratio,
            "fee": volume_fee,
            "volume": volume,
            "volatility": volatility,
            "volatility_available": (price > 0 and price_min > 0 and price_max > 0),
            "apr": apr,
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            "bin_step": tick_spacing,
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
        volatility = 0.0
        volatility_available = False
        if current_price > 0 and bool(prices):
            if timeframe == "24h" and len(prices) >= 2:
                # 14 points = 7 days (every 12h). Last 2 points cover the last 24h.
                window_prices = prices[-2:] + [current_price]
                volatility = (max(window_prices) - min(window_prices)) / current_price * 100.0
                volatility_available = True
            elif timeframe == "7d":
                window_prices = prices + [current_price]
                volatility = (max(window_prices) - min(window_prices)) / current_price * 100.0
                volatility_available = True
        fee_rate = finite_float(pool.get("feeRate"), "fee rate") / 1_000_000.0
        tick_spacing = integer(pool.get("tickSpacing"), "tick spacing")
        current_tick_index = integer(
            pool.get("tickCurrentIndex")
            or pool.get("currentTickIndex")
            or pool.get("current_tick_index")
            or 0,
            "tickCurrentIndex",
        )
        return {
            "dex": "orca",
            "pool_address": pool.get("address", ""),
            "name": f"{token_a['symbol']}-{token_b['symbol']}",
            "pool_type": "Splash" if tick_spacing == 32896 else "Whirlpool",
            "tvl": tvl,
            "fee_tvl_ratio": (fees / tvl * 100.0) if tvl > 0 else 0.0,
            "fee": fees,
            "volume": finite_float(stats.get("volume"), "volume"),
            "volatility": volatility,
            "volatility_available": volatility_available,
            "apr": finite_float(stats.get("yieldOverTvl"), "yield over TVL")
            * 100.0
            * (
                365.0
                if timeframe == "24h"
                else 365.0 / (7.0 if timeframe == "7d" else 30.0)
            ),
            "fee_pct": fee_rate * 100.0,
            "fee_rate": fee_rate,
            "tick_spacing": tick_spacing,
            "bin_step": tick_spacing,
            "token_x": token_a,
            "token_y": token_b,
            "current_tick_index": current_tick_index,
            **top_level_token_metadata(token_a, token_b),
            "pool_price": current_price,
            "active_bin_id": 0,
        }

    if pool.get("_dex") == "meteora" or "dlmm_params" in pool:
        normalized = dict(pool)
        normalized["dex"] = "meteora"
        normalized["pool_type"] = "DLMM"
        normalized["tick_spacing"] = integer(
            mapping(pool.get("dlmm_params")).get("bin_step"), "bin step"
        )
        normalized["bin_step"] = normalized["tick_spacing"]
        for field in ("tvl", "fee", "volume", "apr", "fee_pct"):
            normalized[field] = finite_float(pool.get(field), field)
        normalized["apr"] = _yield_over_tvl_apr(
            normalized["fee"], normalized["tvl"], window
        )
        normalized.setdefault("fee_rate", normalized["fee_pct"] / 100.0)
        current_price = finite_float(
            pool.get("pool_price", pool.get("price")), "price"
        )
        price_min = finite_float(pool.get("min_price"), "minimum price")
        price_max = finite_float(pool.get("max_price"), "maximum price")
        normalized["volatility"] = _volatility(current_price, price_min, price_max)
        normalized["volatility_available"] = (
            current_price > 0 and price_min > 0 and price_max > 0
        ) or "volatility" in pool
        if "volatility" in pool and normalized["volatility"] == 0.0:
            normalized["volatility"] = finite_float(pool.get("volatility"), "volatility")
        token_x = token_entry(_apply_price_map(mapping(pool.get("token_x")), price_map or {}))
        token_y = token_entry(_apply_price_map(mapping(pool.get("token_y")), price_map or {}))
        normalized["token_x"] = token_x
        normalized["token_y"] = token_y
        normalized.update(top_level_token_metadata(token_x, token_y))
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
        self.client = client or MultiDexClient(timeout=self.config.timeout)
        self.rpc = rpc
        self.scorer = PoolScorer()

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
            return JupiterPriceClient().fetch_prices(mints)
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

    def screen_pool(self, p: Dict[str, Any], price_map: Dict[str, float] = None) -> Tuple[Optional[Candidate], str]:
        """Normalize and enrich a raw pool, then emit it as a fact.

        Policy filtering (whitelist, TVL/volume bands, scoring, etc.) is now
        owned by Sheldon. Missy only supplies clean, normalized observations.
        """
        n = normalize_pool(p, window=self.config.window, price_map=price_map)

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

        # Fetch live price index via RPC. Meteora DLMM exposes activeId;
        # Orca/Raydium discovery APIs omit the current tick, so their pool
        # accounts are decoded directly. Without this, open ranges would be
        # centered at tick 0 (the ZEC/USDC re-open bug).
        active_bin_id = 0
        current_tick_index = 0
        dex_name = str(n.get("dex") or "")
        if self.rpc and dex_name == "meteora" and pool_type == "DLMM":
            active_bin_id = _fetch_active_bin_id(self.rpc, n.get("pool_address", ""))
            current_tick_index = active_bin_id
        elif self.rpc and dex_name in ("orca", "raydium") and pool_type.lower() != "standard":
            current_tick_index = int(n.get("current_tick_index") or 0)
            if current_tick_index == 0:
                current_tick_index = _fetch_current_tick(
                    self.rpc, dex_name, n.get("pool_address", "")
                )

        pool_price = float(n.get("pool_price") or 0.0)
        token_x_price_usd = float(n.get("token_x_price_usd") or 0.0)
        token_y_price_usd = float(n.get("token_y_price_usd") or 0.0)

        # Derive missing token prices using the pool price (X denominated in Y)
        if token_x_price_usd == 0.0 and token_y_price_usd > 0.0 and pool_price > 0.0:
            token_x_price_usd = token_y_price_usd * pool_price
        elif token_y_price_usd == 0.0 and token_x_price_usd > 0.0 and pool_price > 0.0:
            token_y_price_usd = token_x_price_usd / pool_price

        candidate = Candidate(
            pool_address=n.get("pool_address", ""),
            name=n.get("name", ""),
            whitelisted_token_symbol=normalize_symbol((n.get("token_x") or {}).get("symbol", "")),
            whitelisted_token_address=(n.get("token_x") or {}).get("address", ""),
            paired_token_symbol=normalize_symbol((n.get("token_y") or {}).get("symbol", "")),
            tvl=tvl,
            fee_tvl_ratio=fee_tvl_ratio,
            daily_fee_usd=daily_fee_usd,
            volume_window=volume_window,
            bin_step=tick_spacing,
            fee_pct=fee_pct,
            volatility=volatility,
            # Scoring is now Sheldon's job; Missy emits a neutral placeholder.
            score=0.0,
            dex=str(n.get("dex") or "raydium"),
            pool_type=pool_type,
            apr=apr,
            fee_rate=float(n.get("fee_rate") or 0.0),
            tick_spacing=tick_spacing,
            active_bin_id=int(active_bin_id),
            current_tick_index=int(current_tick_index),
            effective_tvl=tvl,
            realized_fee_apr=0.0,
            adjusted_apr=0.0,
            yield_score=0.0,
            depth_score=0.0,
            efficiency_score=0.0,
            risk_score=0.0,
            lp_fee_share=lp_fee_share,
            pool_price=pool_price,
            token_x_address=n.get("token_x_address", ""),
            token_x_decimals=int(n.get("token_x_decimals") or 0),
            token_x_price_usd=token_x_price_usd,
            token_y_address=n.get("token_y_address", ""),
            token_y_decimals=int(n.get("token_y_decimals") or 0),
            token_y_price_usd=token_y_price_usd,
        )
        return candidate, ""

    def screen_all(
        self, pools: List[Dict[str, Any]]
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        candidates: List[Candidate] = []
        rejects: Dict[str, int] = {}
        price_map = self._fetch_pool_prices(pools)
        for pool in pools:
            try:
                candidate, reason = self.screen_pool(pool, price_map=price_map)
            except (TypeError, ValueError, OverflowError) as exc:
                reason = f"invalid provider payload: {exc}"
                candidate = None
            if candidate:
                candidates.append(candidate)
            elif reason:
                rejects[reason] = rejects.get(reason, 0) + 1
        candidates.sort(key=attrgetter("score"), reverse=True)
        return candidates, rejects

    def fetch_and_screen(
        self, max_pages: int = 5
    ) -> Tuple[List[Candidate], Dict[str, int]]:
        pools = self.client.fetch_pools(self.config, max_pages=max_pages)
        return self.screen_all(pools)


# Compatibility aliases for existing integrations.
RaydiumScreener = MultiDexScreener
MeteoraScreener = MultiDexScreener
