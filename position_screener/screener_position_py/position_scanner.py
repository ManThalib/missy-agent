"""Discover and decode Meteora DLMM, Raydium CLMM, and Orca positions."""

import base64
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from core.constants import LAMPORTS_PER_SOL, SOL_MINT
from core.prices import JupiterPriceClient
from core.solana import (
    b58decode as _b58decode,
    b58encode as _b58encode,
    discriminator as _discriminator,
    is_valid_solana_address,
)
from core.wallet_balances import (
    fetch_sol_balance_lamports,
    fetch_token_accounts,
    parse_token_account,
)

from .helius_history_client import HeliusHistoryClient
from .helius_parser import HeliusWebhookParser
from .helius_types import NormalizedTransaction, ParsedInstruction
from .analytics.closure_state import ClosureState
from .enrichment import enrich_positions
from .liquidity_position import LiquidityPosition
from .meteora_pending import MeteoraPendingFeesFetcher
from .orca_pending import OrcaPendingFeesFetcher
from .position_scan import PositionScan
from .raydium_pending import PendingFeesFetcher, _pda, raydium_position_checkpoint
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
# Fresh position NFTs can take several seconds to index; one retry covers it.
_INDEXING_RETRY_SECONDS = 3.0


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
        self.meteora_fees = MeteoraPendingFeesFetcher(self.rpc)
        self._reward_decimals_cache: Dict[tuple, List[int]] = {}
        self._last_token_accounts: Optional[List[Dict[str, Any]]] = None

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
            accounts = self._fetch_account_bytes(
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
        """Compute live pending fees/rewards for Orca Whirlpool positions.

        Position accounts are read in one batch, then OrcaPendingFeesFetcher
        applies the SDK math against live pool state. Best-effort: a failure
        keeps the checkpointed values and records the error per position.
        """
        active = [p for p in positions if p.dex == "orca" and p.status == "active"]
        if not active:
            return
        try:
            blobs = self._fetch_account_bytes([p.position_address for p in active])
        except Exception as exc:
            blobs = {}
            batch_error = f"position refetch failed: {exc}"
        else:
            batch_error = ""

        fetcher = OrcaPendingFeesFetcher(self.rpc)
        for position in active:
            if position.pool_enrichment is None:
                position.pool_enrichment = {}
            try:
                data = blobs.get(position.position_address)
                if data is None:
                    raise ValueError(batch_error or f"Position account not found: {position.position_address}")
                out = fetcher.compute_pending(position.pool_address, data, None)
                position.fees_owed_raw = [int(v) for v in out["fees_owed_raw"]]
                position.rewards_owed_raw = [int(v) for v in out["rewards_owed_raw"]]
                position.reward_mints = [m if m else "" for m in out["reward_mints"]]
                position.reward_decimals = self._reward_decimals(position.reward_mints)
                position.pending_fees_source = "computed"
                position.pool_enrichment["pending_fees_source"] = "computed"
            except Exception as exc:
                # Keep checkpointed values on failure; label the error.
                position.pending_fees_source = "raw_checkpoint"
                position.pool_enrichment["pending_fees_error"] = str(exc)
                position.pool_enrichment["pending_fees_source"] = "raw_checkpoint"

    def _refresh_meteora_pending(self, positions: List[LiquidityPosition]) -> None:
        """Compute live fees/rewards/token amounts for Meteora DLMM positions.

        Raw position accounts only carry per-bin checkpoint sums; the SDK
        math in meteora_pending.py derives accrual since each checkpoint and
        the position's per-bin token amounts (share * reserves / supply),
        which raw liquidity shares alone cannot express. Best-effort: a
        failure keeps the checkpointed fee sums, leaves amounts unknown (so
        enrichment reports value_known=false instead of a bogus value), and
        records the error on the position.
        """
        active = [
            p for p in positions if p.dex == "meteora" and p.status == "active"
        ]
        for position in positions:
            if position.dex != "meteora" or position.status != "active":
                continue
            position.pending_fees_source = "raw_per_bin_sum"
            if position.pool_enrichment is None:
                position.pool_enrichment = {}
            position.pool_enrichment["pending_fees_source"] = "raw_per_bin_sum"
        if not active:
            return
        try:
            raw_accounts = self._fetch_account_bytes(
                [p.position_address for p in active if p.position_address]
            )
        except Exception as exc:
            # Best-effort enrichment: an RPC failure here must not abort the
            # whole scan. Keep raw checkpointed sums and record the error so
            # downstream enrichment reports value_known=false.
            for position in active:
                position.pool_enrichment["meteora_pending_error"] = str(exc)
            return
        for position in active:
            data = raw_accounts.get(position.position_address)
            if data is None:
                continue
            try:
                out = self.meteora_fees.compute_pending(
                    position.pool_address, data
                )
            except Exception as exc:
                position.pool_enrichment["meteora_compute_error"] = str(exc)
                continue
            fees_raw = out["fees_owed_raw"]
            rewards_raw = out["rewards_owed_raw"]
            position.fees_owed_raw = [int(fees_raw[0]), int(fees_raw[1])]
            position.rewards_owed_raw = [int(v) for v in rewards_raw]
            position.amounts_x_raw = int(out["amount_x_raw"])
            position.amounts_y_raw = int(out["amount_y_raw"])
            position.reward_mints = [str(m) for m in out.get("reward_mints") or []]
            position.reward_decimals = self._reward_decimals(position.reward_mints)
            position.pending_fees_source = "computed"
            position.pool_enrichment["pending_fees_source"] = "computed"

    def _fetch_account_bytes(
        self, addresses: Sequence[str]
    ) -> Dict[str, Optional[bytes]]:
        """Batch-fetch raw account data by address; missing accounts map to None."""
        out: Dict[str, Optional[bytes]] = {}
        for start in range(0, len(addresses), _RPC_BATCH_SIZE):
            batch = addresses[start:start + _RPC_BATCH_SIZE]
            results = self.rpc.batch(
                [("getAccountInfo", [a, {"encoding": "base64"}]) for a in batch]
            )
            for addr, res in zip(batch, results):
                value = (res or {}).get("value")
                out[addr] = base64.b64decode(value["data"][0]) if value else None
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
        if not is_valid_solana_address(wallet):
            raise ValueError(f"wallet must be a 32-byte Solana base58 address: {wallet!r}")
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
        current, errors, successful_providers = self._fetch_current(wallet, selected)

        if successful_providers == 0:
            failures = "; ".join(
                f"{name}: {errors.get(name, 'not attempted')}" for name in selected
            )
            raise RuntimeError(f"All selected position providers failed: {failures}")

        if not current or errors:
            # Freshly opened positions race RPC indexing; an empty or partial
            # view here would make downstream dedup re-open held pools. One
            # retry after a short pause covers that race.
            time.sleep(_INDEXING_RETRY_SECONDS)
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

        merged, newly_closed_keys = self._filter_closed_positions(
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

        token_accounts = self._last_token_accounts
        if token_accounts is None:
            try:
                token_accounts = fetch_token_accounts(self.rpc, wallet, commitment="confirmed")
            except Exception as exc:
                errors["wallet_balances"] = str(exc)
                token_accounts = []
        wallet_balances = [
            parsed
            for parsed in (parse_token_account(account) for account in token_accounts)
            if parsed is not None
        ]

        sol_balance_lamports = 0
        try:
            sol_balance_lamports = fetch_sol_balance_lamports(self.rpc, wallet)
        except Exception as exc:
            errors["sol_balance"] = str(exc)

        mints = [SOL_MINT] + [entry["mint"] for entry in wallet_balances]
        try:
            prices = JupiterPriceClient().fetch_prices(mints)
        except Exception as exc:
            prices = {}
            errors["prices"] = f"USD prices unavailable: {exc}"
        sol_price_usd = prices.get(SOL_MINT, 0.0)
        for entry in wallet_balances:
            price = prices.get(entry["mint"], 0.0)
            entry["price_usd"] = price
            entry["usd_value"] = entry["ui_amount"] * price

        wallet_total_usd = sol_balance_lamports / LAMPORTS_PER_SOL * sol_price_usd
        wallet_total_usd += sum(entry["usd_value"] for entry in wallet_balances)

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
            newly_closed_keys=sorted(newly_closed_keys),
        )

    def _fetch_current(
        self, wallet: str, selected: Sequence[str]
    ) -> Tuple[List[LiquidityPosition], Dict[str, str], int]:
        """Fetch live positions from the selected DEX providers.

        Returns (positions, errors, successful_provider_count). The token
        accounts used for NFT discovery are kept in ``_last_token_accounts`` so
        the scan can reuse them for wallet balances.
        """
        errors: Dict[str, str] = {}
        current: List[LiquidityPosition] = []
        successful_providers = 0
        jobs = {}
        self._last_token_accounts = None
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            if "meteora" in selected:
                jobs["meteora"] = executor.submit(self._fetch_meteora, wallet)
            nft_mints: Optional[List[str]] = None
            if "raydium" in selected or "orca" in selected:
                try:
                    token_accounts = fetch_token_accounts(
                        self.rpc, wallet, commitment="confirmed"
                    )
                    self._last_token_accounts = token_accounts
                    nft_mints = self._nft_mints(token_accounts)
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
                reward_base = 72 + 70 * 16
                fee_base = reward_base + 70 * 48
                bounds_offset = fee_base + 70 * 48
                if len(data) < bounds_offset + 8:
                    continue
                shares = [_u128(data, 72 + index * 16) for index in range(70)]
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

    @staticmethod
    def _nft_mints(token_accounts: Sequence[Dict[str, Any]]) -> List[str]:
        """Mints of wallet NFT-like holdings (decimals 0, amount 1)."""
        mints = set()
        for token_account in token_accounts:
            info = (
                ((token_account.get("account") or {}).get("data") or {}).get("parsed")
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
        """Read each held NFT's position account directly by its derived PDA.

        Raydium and Orca both derive the position account as
        ``["position", position_mint]`` under their program. Batched
        getAccountInfo replaces one full-program getProgramAccounts scan per mint.
        """
        program = RAYDIUM_PROGRAM if dex == "raydium" else ORCA_PROGRAM
        program_id = _b58decode(program)
        addresses = [
            _pda([b"position", _b58decode(mint)], program_id) for mint in mints or []
        ]
        accounts = self._fetch_account_bytes(addresses)
        positions: List[LiquidityPosition] = []
        for address, data in accounts.items():
            if not data:
                continue
            if dex == "raydium":
                position = self._decode_raydium(address, data)
            else:
                position = self._decode_orca(address, data)
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
    ) -> Tuple[List[LiquidityPosition], Set[str]]:
        """Exclude closed positions already emitted in a previous scan.

        This is the single owner of the closure state for a scan: it refreshes
        the state file once and returns the newly closed keys alongside the
        filtered positions. On the very first scan (no state file exists), all
        closed positions are recorded as "already reported" and omitted, but
        they are persisted so future scans know they are old.
        """
        state_existed = os.path.exists(closure_state_path)
        closure_state = ClosureState(path=closure_state_path)

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

        kept = [position for position in positions if _is_kept(position)]
        return kept, newly_closed_keys

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
