"""Discover and decode Meteora DLMM, Raydium CLMM, and Orca positions."""

import base64
import hashlib
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .helius_history_client import HeliusHistoryClient
from .helius_parser import HeliusWebhookParser
from .helius_types import NormalizedTransaction, ParsedInstruction
from .liquidity_position import LiquidityPosition
from .position_scan import PositionScan
from .rpc_client import RpcClient

METEORA_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
RAYDIUM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
ORCA_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)
PROGRAM_DEX = {
    METEORA_PROGRAM: "meteora",
    RAYDIUM_PROGRAM: "raydium",
    ORCA_PROGRAM: "orca",
}
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_RPC_BATCH_SIZE = 50


def _discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode("ascii")).digest()[:8]


def _b58encode(value: bytes) -> str:
    leading = len(value) - len(value.lstrip(b"\0"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _B58_ALPHABET[remainder] + encoded
    return "1" * leading + encoded


def _b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            number = number * 58 + _B58_ALPHABET.index(char)
        except ValueError as exc:
            raise ValueError("address is not valid base58") from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


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

    def scan(
        self, wallet: str, dex: str = "all", history_pages: int = 0
    ) -> PositionScan:
        if len(_b58decode(wallet)) != 32:
            raise ValueError("wallet must be a 32-byte Solana base58 address")
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

        if successful_providers == 0:
            failures = "; ".join(
                f"{name}: {errors.get(name, 'not attempted')}" for name in selected
            )
            raise RuntimeError(f"All selected position providers failed: {failures}")

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
            sol_result = self.rpc.call("getBalance", [wallet, "finalized"])
            sol_balance_lamports = sol_result.get("value", 0) if isinstance(sol_result, dict) else 0
        except Exception:
            pass

        # Fetch token accounts and enrich with prices
        wallet_balances: List[Dict[str, Any]] = []
        try:
            calls = [
                (
                    "getTokenAccountsByOwner",
                    [
                        wallet,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed", "commitment": "finalized"},
                    ],
                )
            ]
            result = self.rpc.batch(calls)
            token_accounts = (result or {}).get("value", [])
            for account in token_accounts:
                info = (
                    (account.get("account") or {}).get("data") or {}
                ).get("parsed") or {}
                info = info.get("info") or {}
                mint = info.get("mint")
                amount = info.get("tokenAmount") or {}
                raw_amount = amount.get("amount", "0")
                decimals = amount.get("decimals", 0)
                ui_amount = float(raw_amount) / (10 ** decimals) if decimals > 0 else float(raw_amount)
                if mint:
                    wallet_balances.append(
                        {
                            "mint": mint,
                            "symbol": info.get("owner", ""),
                            "decimals": decimals,
                            "raw_amount": raw_amount,
                            "ui_amount": ui_amount,
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
            sol_balance_lamports=sol_balance_lamports,
            sol_price_usd=sol_price_usd,
            wallet_total_usd=wallet_total_usd,
            wallet_balances=wallet_balances,
            positions=merged,
            errors=errors,
            history_complete=history_complete,
        )

    def _program_accounts(
        self, program: str, filters: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        result = self.rpc.call(
            "getProgramAccounts",
            [
                program,
                {
                    "encoding": "base64",
                    "commitment": "finalized",
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
                        "commitment": "finalized",
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
                            "commitment": "finalized",
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
        rewards = [_u64(data, 156 + index * 24) for index in range(3)]
        return LiquidityPosition(
            dex="orca",
            position_address=address,
            position_mint=_b58encode(data[40:72]),
            pool_address=_b58encode(data[8:40]),
            status="active" if liquidity else "inactive",
            liquidity_raw=liquidity,
            lower_bound=struct.unpack_from("<i", data, 88)[0],
            upper_bound=struct.unpack_from("<i", data, 92)[0],
            fees_owed_raw=[_u64(data, 108), _u64(data, 132)],
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
