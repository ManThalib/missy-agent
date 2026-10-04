"""Discover and decode Meteora DLMM, Raydium CLMM, and Orca positions."""

import base64
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.solana import (
    b58decode as _b58decode,
    b58encode as _b58encode,
    discriminator as _discriminator,
)
from core.wallet_balances import (
    TOKEN_PROGRAMS,
    fetch_sol_balance_lamports,
    fetch_token_accounts,
)

from .helius_history_client import HeliusHistoryClient
from .helius_parser import HeliusWebhookParser
from .helius_types import NormalizedTransaction, ParsedInstruction
from .analytics.closure_state import ClosureState
from .enrichment import enrich_positions
from .liquidity_position import LiquidityPosition
from .position_scan import PositionScan
from .raydium_pending import PendingFeesFetcher, raydium_position_checkpoint
from .rpc_client import RpcClient

METEORA_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
RAYDIUM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
ORCA_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
PROGRAM_DEX = {
    METEORA_PROGRAM: "meteora",
    RAYDIUM_PROGRAM: "raydium",
    ORCA_PROGRAM: "orca",
}
_RPC_BATCH_SIZE = 50


def _account_bytes(account: Dict[str, Any]) -> bytes:
    data = (account.get("account") or {}).get("data", account.get("data", ""))
    encoded = data[0] if isinstance(data, list) else data
    return base64.b64decode(encoded or "")


def _u128(data: bytes, offset: int) -> int:
    return (
        int.from_bytes(data[offset : offset + 16], "little")
        if len(data) >= offset + 16
        else 0
    )


def _u64(data: bytes, offset: int) -> int:
    return (
        int.from_bytes(data[offset : offset + 8], "little")
        if len(data) >= offset + 8
        else 0
    )


class PositionScanner:
    """Fetches current on-chain state and optional Helius position history."""

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        helius_api_key: Optional[str] = None,
        timeout: float = 20.0,
        rpc: Optional[RpcClient] = None,
        history_client: Optional[HeliusHistoryClient] = None,
    ):
        key = (
            helius_api_key
            if helius_api_key is not None
            else os.environ.get("HELIUS_API_KEY", "")
        )
        default_rpc = os.environ.get(
            "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
        )
        if key and "api-key=" not in default_rpc and "SOLANA_RPC_URL" not in os.environ:
            default_rpc = f"https://mainnet.helius-rpc.com/?api-key={key}"
        self.rpc = rpc or RpcClient(rpc_url or default_rpc, timeout=timeout)
        self.history = history_client or (
            HeliusHistoryClient(key, timeout=timeout) if key else None
        )
        self.webhook_parser = HeliusWebhookParser()
        self.pending_fees = PendingFeesFetcher(self.rpc)
        self._reward_decimals_cache: Dict[tuple, List[int]] = {}

    def _refresh_raydium_pending(self, positions: List[LiquidityPosition]) -> None:
        """Recompute real pending fees/rewards for Raydium CLMM positions.

        PersonalPositionState checkpoint fields (token_fees_owed_* and
        reward_amount_owed_*) only update when a claim touches the position,
        so a never-claimed position reports 0 forever. Fetch the live
        PoolState + boundary tick arrays and apply the SDK math
        (raydium_pending.py, verified against raydium-sdk-v2 on mainnet).
        Best-effort: a fetch/math failure records the error and keeps the
        checkpointed values rather than zeroing the position.
        """
        if not any(p.dex == "raydium" and p.status == "active" for p in positions):
            return
        try:
            accounts = self._fetch_raydium_position_accounts(
                [p.position_address for p in positions
                 if p.dex == "raydium" and p.status == "active"]
            )
        except Exception as exc:
            for position in positions:
                if position.dex == "raydium" and position.status == "active":
                    if position.pool_enrichment is None:
                        position.pool_enrichment = {}
                    position.pool_enrichment["pending_fees_error"] = (
                        f"position refetch failed: {exc}"
                    )
            return
        for position in positions:
            if position.dex != "raydium" or position.status != "active":
                continue
            data = accounts.get(position.position_address)
            if data is None:
                continue
            try:
                out = self.pending_fees.compute_pending(
                    position.pool_address,
                    position.lower_bound,
                    position.upper_bound,
                    position.liquidity_raw,
                    raydium_position_checkpoint(data),
                )
            except Exception as exc:
                if position.pool_enrichment is None:
                    position.pool_enrichment = {}
                position.pool_enrichment["pending_fees_error"] = str(exc)
                continue
            fees_raw = out["fees_owed_raw"]
            rewards_raw = out["rewards_owed_raw"]
            position.fees_owed_raw = [int(fees_raw[0]), int(fees_raw[1])]
            position.rewards_owed_raw = [int(v) for v in rewards_raw]
            position.reward_mints = [str(m) for m in out["reward_mints"]]
            position.reward_decimals = self._reward_decimals(out["reward_mints"])
            if position.pool_enrichment is None:
                position.pool_enrichment = {}
            position.pool_enrichment["pending_fees_source"] = "computed"
            position.pending_fees_source = "computed"

    def _refresh_orca_pending(self, positions: List[LiquidityPosition]) -> None:
        """Best-effort reward-mint population for Orca Whirlpool positions.

        Orca Position accounts already carry checkpointed feeOwedA/B, so we
        keep those raw values and label the source. We do fetch the pool's
        reward configuration so reward USD can be priced consistently with
        Raydium positions.
        """
        active = [p for p in positions if p.dex == "orca" and p.status == "active"]
        if not active:
            return
        pool_addresses = sorted({p.pool_address for p in active if p.pool_address})
        pool_data: Dict[str, bytes] = {}
        try:
            for start in range(0, len(pool_addresses), _RPC_BATCH_SIZE):
                batch = pool_addresses[start:start + _RPC_BATCH_SIZE]
                results = self.rpc.batch(
                    [("getAccountInfo", [a, {"encoding": "base64"}]) for a in batch]
                )
                for addr, res in zip(batch, results):
                    value = (res or {}).get("value")
                    data = value["data"][0] if value and isinstance(value.get("data"), list) else None
                    if data:
                        pool_data[addr] = base64.b64decode(data)
        except Exception:
            pass

        reward_mints_by_pool: Dict[str, List[str]] = {}
        for pool_address, data in pool_data.items():
            try:
                reward_mints_by_pool[pool_address] = self._decode_orca_pool_rewards(data)
            except Exception:
                reward_mints_by_pool[pool_address] = []

        for position in active:
            position.pending_fees_source = "raw_checkpoint"
            if position.pool_enrichment is None:
                position.pool_enrichment = {}
            position.pool_enrichment["pending_fees_source"] = "raw_checkpoint"
            mints = reward_mints_by_pool.get(position.pool_address, [])
            if mints:
                position.reward_mints = mints
                position.reward_decimals = self._reward_decimals(mints)

    @staticmethod
    def _decode_orca_pool_rewards(data: bytes) -> List[str]:
        """Return up to 3 reward mints from an Orca Whirlpool pool account.

        Best-effort: the layout is the anchor Whirlpool state. Failures
        return an empty list rather than raising.
        """
        if len(data) < 389:
            return []
        reward_info_start = 125
        reward_info_stride = 88
        mints: List[str] = []
        for i in range(3):
            offset = reward_info_start + i * reward_info_stride
            if offset + 32 > len(data):
                break
            mint = _b58encode(data[offset:offset + 32])
            mints.append(mint)
        return mints

    def _refresh_meteora_pending(self, positions: List[LiquidityPosition]) -> None:
        """Label pending-fee source for Meteora DLMM positions.

        Meteora positions already carry per-bin fee accumulations summed in
        _fetch_meteora. Live DLMM fee-growth math is not yet implemented;
        the raw summed values are retained and the source is flagged.
        """
        for position in positions:
            if position.dex != "meteora" or position.status != "active":
                continue
            position.pending_fees_source = "raw_per_bin_sum"
            if position.pool_enrichment is None:
                position.pool_enrichment = {}
            position.pool_enrichment["pending_fees_source"] = "raw_per_bin_sum"

    def _fetch_raydium_position_accounts(
        self, addresses: Sequence[str]
    ) -> Dict[str, Optional[bytes]]:
        """Batch-fetch raw PersonalPositionState account data by address."""
        import base64

        out: Dict[str, Optional[bytes]] = {}
        for start in range(0, len(addresses), _RPC_BATCH_SIZE):
            batch = addresses[start : start + _RPC_BATCH_SIZE]
            results = self.rpc.batch(
                [("getAccountInfo", [a, {"encoding": "base64"}]) for a in batch]
            )
            for addr, res in zip(batch, results):
                value = (res or {}).get("value")
                out[addr] = (
                    base64.b64decode(value["data"][0]) if value else None
                )
        return out

    def _reward_decimals(self, mints: Sequence[str]) -> List[int]:
        """Decimals for the reward mints (mint accounts, jsonParsed).

        Placeholder/inactive mints (system program id) yield 0. Shared across
        positions via a small cache; a failure leaves zeros, and enrichment
        prices nothing it cannot scale.
        """
        if not mints:
            return []
        cache_key = tuple(mints)
        cached = self._reward_decimals_cache.get(cache_key)
        if cached is not None:
            return cached
        unique = [m for m in dict.fromkeys(mints) if m and not m.startswith("1111")]
        decimals: Dict[str, int] = {m: 0 for m in mints}
        if unique:
            try:
                results = self.rpc.batch(
                    [
                        ("getAccountInfo", [m, {"encoding": "jsonParsed"}])
                        for m in unique
                    ]
                )
                for mint, res in zip(unique, results):
                    info = (((res or {}).get("value") or {}).get("data") or {}).get(
                        "parsed", {}
                    ).get("info", {})
                    decimals[mint] = int(info.get("decimals") or 0)
            except Exception:
                pass
        out = [decimals[m] for m in mints]
        self._reward_decimals_cache[cache_key] = out
        return out

    def scan(
        self,
        wallet: str,
        dex: str = "all",
        history_pages: int = 0,
        closure_state_path: Optional[str] = None,
        wallet_id: str = "main",
    ) -> PositionScan:
        try:
            if len(_b58decode(wallet)) != 32:
                raise ValueError
        except Exception:
            raise ValueError(
                f"wallet must be a 32-byte Solana base58 address: {wallet!r}"
            ) from None
        normalized_dex = dex.lower()
        supported = {"all", "meteora", "raydium", "orca"}
        if normalized_dex not in supported:
            raise ValueError(f"unsupported DEX: {dex}")
        if history_pages < 0:
            raise ValueError("history_pages cannot be negative")
        selected = (
            ["meteora", "raydium", "orca"]
            if normalized_dex == "all"
            else [normalized_dex]
        )
        errors: Dict[str, str] = {}
        current: List[LiquidityPosition] = []
        successful_providers = 0

        current, errors, successful_providers = self._fetch_current(wallet, selected)

        if successful_providers == 0:
            failures = "; ".join(
                f"{name}: {errors.get(name, 'not attempted')}" for name in selected
            )
            raise RuntimeError(f"All selected position providers failed: {failures}")

        if not current or errors:
            # Freshly opened positions race RPC indexing (a just-minted
            # position NFT can be invisible to a confirmed
            # getTokenAccountsByOwner for several seconds). An empty or
            # partial wallet here makes downstream dedup think nothing is
            # held and re-open the same pool. Retry once before reporting.
            time.sleep(3.0)
            retry_current, retry_errors, retry_providers = self._fetch_current(
                wallet, selected
            )
            if retry_current or not retry_errors:
                current = retry_current
                errors = retry_errors
                successful_providers = retry_providers

        if not current or errors:
            # Freshly opened positions race RPC indexing (a just-minted
            # position NFT can be invisible to a finalized/confirmed
            # getTokenAccountsByOwner for several seconds). An empty or
            # partial wallet here makes downstream dedup think nothing is
            # held and re-open the same pool. Retry once before reporting.
            time.sleep(3.0)
            retry_current, retry_errors, retry_providers = self._fetch_current(
                wallet, selected
            )
            if retry_current or not retry_errors:
                current = retry_current
                errors = retry_errors
                successful_providers = retry_providers

        historical: List[LiquidityPosition] = []
        history_complete = False
        if self.history:
            try:
                transactions, history_complete = self.history.fetch(
                    wallet, max_pages=history_pages
                )
                historical = self._parse_normalized_history(
                    (
                        self.webhook_parser.parse_transaction(item)
                        for item in transactions
                    ),
                    selected,
                    wallet=wallet,
                )
            except Exception as exc:
                errors["history"] = str(exc)
        else:
            errors["history"] = (
                "HELIUS_API_KEY is not set; closed-position history was not scanned"
            )

        merged = self._merge_positions(current, historical)
        self._mark_degraded_providers(merged, errors)
        self._refresh_raydium_pending(merged)
        self._refresh_orca_pending(merged)
        self._refresh_meteora_pending(merged)
        for position in merged:
            position.wallet_id = wallet_id

        # Filter historical closed positions: keep only active/inactive
        # positions and closed positions that closed since the previous scan.
        merged = self._filter_closed_positions(
            merged,
            closure_state_path=closure_state_path or "position_closure_state.json",
        )

        enrich_positions(merged, errors)
        merged.sort(
            key=lambda item: (
                item.dex,
                item.status == "closed",
                item.pool_address,
                item.position_address,
            )
        )

        # Fetch SOL balance via RPC
        sol_balance_lamports = 0
        sol_price_usd = 0.0
        try:
            sol_balance_lamports = fetch_sol_balance_lamports(self.rpc, wallet)
        except Exception:
            pass

        # Fetch token accounts and enrich with prices
        wallet_balances: List[Dict[str, Any]] = []
        try:
            for account in fetch_token_accounts(self.rpc, wallet):
                wallet_balances.append(
                    {
                        "mint": account["mint"],
                        "symbol": account.get("owner", ""),
                        "decimals": account.get("decimals", 0),
                        "raw_amount": account.get("raw_amount", "0"),
                        "ui_amount": account.get("ui_amount", 0.0),
                        "price_usd": 0.0,
                        "usd_value": 0.0,
                    }
                )
        except Exception:
            pass

        # Calculate total USD value
        wallet_total_usd = sol_balance_lamports / 1_000_000_000 * sol_price_usd
        for bal in wallet_balances:
            wallet_total_usd += bal.get("usd_value", 0.0)

        return PositionScan(
            wallet=wallet,
            wallet_id=wallet_id,
            sol_balance_lamports=sol_balance_lamports,
            sol_price_usd=sol_price_usd,
            wallet_total_usd=wallet_total_usd,
            wallet_balances=wallet_balances,
            positions=merged,
            errors=errors,
            history_complete=history_complete,
        )

    def _fetch_current(
        self, wallet: str, selected: Sequence[str]
    ) -> Tuple[List[LiquidityPosition], Dict[str, str], int]:
        """Fetch live positions from the selected DEX providers.

        Returns (positions, errors, successful_provider_count).
        """
        errors: Dict[str, str] = {}
        current: List[LiquidityPosition] = []
        successful_providers = 0
        jobs = {}
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            if "meteora" in selected:
                jobs["meteora"] = executor.submit(self._fetch_meteora, wallet)
            nft_mints: Optional[List[str]] = None
            if "raydium" in selected or "orca" in selected:
                try:
                    nft_mints = self._fetch_nft_mints(wallet)
                except Exception as exc:
                    errors["nft_inventory"] = str(exc)
                    nft_mints = None
            if "raydium" in selected and nft_mints is not None:
                jobs["raydium"] = executor.submit(
                    self._fetch_nft_positions, "raydium", nft_mints
                )
            elif "raydium" in selected:
                errors["raydium"] = "NFT inventory lookup failed"
            if "orca" in selected and nft_mints is not None:
                jobs["orca"] = executor.submit(
                    self._fetch_nft_positions, "orca", nft_mints
                )
            elif "orca" in selected:
                errors["orca"] = "NFT inventory lookup failed"
            for name in selected:
                if name not in jobs:
                    continue
                try:
                    current.extend(jobs[name].result())
                    successful_providers += 1
                except Exception as exc:
                    errors[name] = str(exc)
        return current, errors, successful_providers

    def _program_accounts(
        self, program: str, filters: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        result = self.rpc.call(
            "getProgramAccounts",
            [
                program,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "filters": filters,
                },
            ],
        )
        return result or []

    def _fetch_meteora(self, wallet: str) -> List[LiquidityPosition]:
        positions: List[LiquidityPosition] = []
        for account_name in ("PositionV2", "Position"):
            filters = [
                {
                    "memcmp": {
                        "offset": 0,
                        "bytes": base64.b64encode(
                            _discriminator(account_name)
                        ).decode(),
                        "encoding": "base64",
                    }
                },
                {"memcmp": {"offset": 40, "bytes": wallet}},
            ]
            for account in self._program_accounts(METEORA_PROGRAM, filters):
                data = _account_bytes(account)
                bounds_offset = 72 + 70 * 16 + 70 * 48 + 70 * 48
                if len(data) < bounds_offset + 8:
                    continue
                shares = [_u128(data, 72 + index * 16) for index in range(70)]
                reward_base = 72 + 70 * 16
                fee_base = reward_base + 70 * 48
                fees = []
                rewards = []
                for index in range(70):
                    fees.extend(
                        (
                            _u64(data, fee_base + index * 48 + 32),
                            _u64(data, fee_base + index * 48 + 40),
                        )
                    )
                    rewards.extend(
                        (
                            _u64(data, reward_base + index * 48 + 32),
                            _u64(data, reward_base + index * 48 + 40),
                        )
                    )
                bounds_offset = fee_base + 70 * 48
                lower = struct.unpack_from("<i", data, bounds_offset)[0]
                upper = struct.unpack_from("<i", data, bounds_offset + 4)[0]
                liquidity = sum(shares)
                positions.append(
                    LiquidityPosition(
                        dex="meteora",
                        position_address=account.get("pubkey", ""),
                        pool_address=_b58encode(data[8:40]),
                        status="active" if liquidity else "inactive",
                        liquidity_raw=liquidity,
                        lower_bound=lower,
                        upper_bound=upper,
                        fees_owed_raw=[sum(fees[0::2]), sum(fees[1::2])],
                        rewards_owed_raw=[sum(rewards[0::2]), sum(rewards[1::2])],
                        source="solana_rpc",
                    )
                )
        return positions

    def _fetch_nft_mints(self, wallet: str) -> List[str]:
        calls = [
            (
                "getTokenAccountsByOwner",
                [
                    wallet,
                    {"programId": program},
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                    },
                ],
            )
            for program in TOKEN_PROGRAMS
        ]
        mints = set()
        for result in self.rpc.batch(calls):
            for token_account in (result or {}).get("value", []):
                info = (
                    ((token_account.get("account") or {}).get("data") or {}).get(
                        "parsed"
                    )
                    or {}
                ).get("info") or {}
                amount = info.get("tokenAmount") or {}
                if (
                    amount.get("decimals") == 0
                    and str(amount.get("amount")) == "1"
                    and info.get("mint")
                ):
                    mints.add(info["mint"])
        return sorted(mints)

    def _fetch_nft_positions(
        self, dex: str, mints: Optional[Sequence[str]]
    ) -> List[LiquidityPosition]:
        program = RAYDIUM_PROGRAM if dex == "raydium" else ORCA_PROGRAM
        discriminator = _discriminator(
            "PersonalPositionState" if dex == "raydium" else "Position"
        )
        mint_offset = 9 if dex == "raydium" else 40
        calls = []
        for mint in mints or []:
            filters = [
                {
                    "memcmp": {
                        "offset": 0,
                        "bytes": base64.b64encode(discriminator).decode(),
                        "encoding": "base64",
                    }
                },
                {"memcmp": {"offset": mint_offset, "bytes": mint}},
            ]
            calls.append(
                (
                    "getProgramAccounts",
                    [
                        program,
                        {
                            "encoding": "base64",
                            "commitment": "confirmed",
                            "filters": filters,
                        },
                    ],
                )
            )
        positions: List[LiquidityPosition] = []
        for start in range(0, len(calls), _RPC_BATCH_SIZE):
            for result in self.rpc.batch(calls[start : start + _RPC_BATCH_SIZE]):
                for account in result or []:
                    data = _account_bytes(account)
                    if dex == "raydium":
                        position = self._decode_raydium(account.get("pubkey", ""), data)
                    else:
                        position = self._decode_orca(account.get("pubkey", ""), data)
                    if position:
                        positions.append(position)
        return positions

    @staticmethod
    def _decode_raydium(address: str, data: bytes) -> Optional[LiquidityPosition]:
        if len(data) < 217 or data[:8] != _discriminator("PersonalPositionState"):
            return None
        liquidity = _u128(data, 81)
        rewards = [_u64(data, 145 + index * 24 + 16) for index in range(3)]
        return LiquidityPosition(
            dex="raydium",
            position_address=address,
            position_mint=_b58encode(data[9:41]),
            pool_address=_b58encode(data[41:73]),
            status="active" if liquidity else "inactive",
            liquidity_raw=liquidity,
            lower_bound=struct.unpack_from("<i", data, 73)[0],
            upper_bound=struct.unpack_from("<i", data, 77)[0],
            fees_owed_raw=[_u64(data, 129), _u64(data, 137)],
            rewards_owed_raw=rewards,
            source="solana_rpc",
        )

    @staticmethod
    def _decode_orca(address: str, data: bytes) -> Optional[LiquidityPosition]:
        if len(data) < 216 or data[:8] != _discriminator("Position"):
            return None
        liquidity = _u128(data, 72)
        # reward_infos start at 144; each entry is growth u128 + amount_owed u64.
        rewards = [_u64(data, 144 + index * 24 + 16) for index in range(3)]
        return LiquidityPosition(
            dex="orca",
            position_address=address,
            position_mint=_b58encode(data[40:72]),
            pool_address=_b58encode(data[8:40]),
            status="active" if liquidity else "inactive",
            liquidity_raw=liquidity,
            lower_bound=struct.unpack_from("<i", data, 88)[0],
            upper_bound=struct.unpack_from("<i", data, 92)[0],
            # Orca Position layout: fee_owed_a/b at offsets 112/136.
            fees_owed_raw=[_u64(data, 112), _u64(data, 136)],
            rewards_owed_raw=rewards,
            source="solana_rpc",
        )

    def _parse_history(
        self,
        transactions: Iterable[Dict[str, Any]],
        selected: Sequence[str],
        wallet: str = "",
    ) -> List[LiquidityPosition]:
        """Compatibility entrypoint for callers with unsanitized Helius records."""
        return self._parse_normalized_history(
            (self.webhook_parser.parse_transaction(item) for item in transactions),
            selected,
            wallet,
        )

    def _parse_normalized_history(
        self,
        transactions: Iterable[NormalizedTransaction],
        selected: Sequence[str],
        wallet: str = "",
    ) -> List[LiquidityPosition]:
        lifecycle: Dict[Tuple[str, str], LiquidityPosition] = {}
        for transaction in transactions:
            if transaction.failed:
                continue
            for instruction in transaction.instructions:
                dex = PROGRAM_DEX.get(instruction.program_id)
                if not dex or dex not in selected:
                    continue
                name = instruction.name.lower()
                if "position" not in name:
                    continue
                accounts = self._named_accounts(instruction)
                recorded_owner = self._pick(accounts, ("nftowner", "owner", "sender"))
                if wallet and recorded_owner and recorded_owner != wallet:
                    continue
                position_address = self._pick(
                    accounts, ("personalposition", "positionv2", "position")
                )
                if not position_address:
                    continue
                key = (dex, position_address)
                if any(word in name for word in ("initialize", "open")):
                    lifecycle[key] = LiquidityPosition(
                        dex=dex,
                        position_address=position_address,
                        pool_address=self._pick(
                            accounts, ("poolstate", "poolid", "whirlpool", "lbpair")
                        ),
                        position_mint=self._pick(
                            accounts, ("positionnftmint", "positionmint", "nftmint")
                        ),
                        status="inactive",
                        opened_signature=transaction.signature,
                        opened_at=transaction.block_time,
                        source="helius_history",
                    )
                elif "close" in name:
                    position = lifecycle.get(key) or LiquidityPosition(
                        dex=dex,
                        position_address=position_address,
                        source="helius_history",
                    )
                    position.status = "closed"
                    position.closed_signature = transaction.signature
                    position.closed_at = transaction.block_time
                    lifecycle[key] = position
        return list(lifecycle.values())

    @staticmethod
    def _named_accounts(instruction: ParsedInstruction) -> Dict[str, str]:
        result = {}
        for account in instruction.accounts:
            name = "".join(char for char in account.name.lower() if char.isalnum())
            if name and account.address:
                result[name] = account.address
        return result

    @staticmethod
    def _pick(accounts: Dict[str, str], names: Sequence[str]) -> str:
        for wanted in names:
            for name, pubkey in accounts.items():
                if name == wanted or name.endswith(wanted):
                    return pubkey
        return ""

    @staticmethod
    def _mark_degraded_providers(
        positions: List[LiquidityPosition], errors: Dict[str, str]
    ) -> None:
        """Flag positions whose DEX provider failed this scan.

        When a provider's RPC batch fails, the only records left for that
        DEX come from history/fallback state, which lack live tick bounds.
        Enrichment then defaults them to in_range=false, and a downstream
        scorer can read that as verifiably out-of-range (the 2026-10-03
        false REBALANCE). Mark such records status="unknown" so no
        consumer treats absent data as range evidence.
        """
        degraded = {
            name for name in ("meteora", "raydium", "orca")
            if errors.get(name)
        }
        if "nft_inventory" in errors:
            degraded.update(("raydium", "orca"))
        if not degraded:
            return
        for position in positions:
            if position.dex in degraded and position.status in ("active", "inactive"):
                position.status = "unknown"
                if position.pool_enrichment is None:
                    position.pool_enrichment = {}
                position.pool_enrichment["degraded_provider"] = errors.get(
                    position.dex, "nft_inventory lookup failed"
                )

    @staticmethod
    def _filter_closed_positions(
        positions: List[LiquidityPosition],
        closure_state_path: str,
    ) -> List[LiquidityPosition]:
        """Exclude closed positions already emitted in a previous scan.

        On the very first scan (no state file exists), all closed positions
        are recorded as "already reported" and are omitted from output, but
        they are persisted so future scans know they are old.
        """
        state_path = closure_state_path
        state_existed = os.path.exists(state_path)
        closure_state = ClosureState(path=state_path)

        newly_closed_tuples = closure_state.refresh(positions)
        newly_closed_keys = {key for _, key in newly_closed_tuples}

        if not state_existed:
            # First run: treat every closed position as already reported.
            newly_closed_keys = set()

        def _is_kept(position: LiquidityPosition) -> bool:
            if position.status != "closed":
                return True
            key = f"{position.dex}:{position.position_address}"
            return key in newly_closed_keys

        return [position for position in positions if _is_kept(position)]

    @staticmethod
    def _merge_positions(
        current: List[LiquidityPosition], historical: List[LiquidityPosition]
    ) -> List[LiquidityPosition]:
        merged = {
            (position.dex, position.position_address): position
            for position in historical
        }
        for position in current:
            prior = merged.get((position.dex, position.position_address))
            if prior:
                position.opened_signature = prior.opened_signature
                position.opened_at = prior.opened_at
            merged[(position.dex, position.position_address)] = position
        return list(merged.values())


def attach_positions(
    candidates: Iterable[Any], positions: Iterable[LiquidityPosition]
) -> None:
    """Attach current positions to qualifying pools without changing pool ranking."""
    by_pool: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for position in positions:
        if position.status == "closed" or not position.pool_address:
            continue
        by_pool.setdefault((position.dex, position.pool_address), []).append(
            position.to_dict()
        )
    for candidate in candidates:
        candidate.wallet_positions = by_pool.get(
            (candidate.dex.lower(), candidate.pool_address), []
        )
