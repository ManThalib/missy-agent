"""Reusable low-level Solana wallet balance fetching helpers.

These functions perform the raw RPC work and return plain data structures so
that consumers can map results into their own models without re-implementing the
``jsonParsed`` token-account traversal.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .constants import KNOWN_MINT_SYMBOLS, LAMPORTS_PER_SOL, TOKEN_PROGRAM_IDS

# Backwards-compatible alias for consumers that refer to the program tuple.
TOKEN_PROGRAMS = TOKEN_PROGRAM_IDS

__all__ = [
    "LAMPORTS_PER_SOL",
    "TOKEN_PROGRAM_IDS",
    "TOKEN_PROGRAMS",
    "fetch_sol_balance_lamports",
    "fetch_token_accounts",
    "parse_token_account",
    "ui_amount",
]


def ui_amount(raw_amount: str, decimals: int) -> float:
    """Convert a raw integer token amount to a decimal UI amount.

    Uses Decimal so amounts above 2**53 keep full precision before the final
    float conversion.
    """
    try:
        raw = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    if decimals > 0:
        return float(raw / (Decimal(10) ** decimals))
    return float(raw)


def fetch_sol_balance_lamports(rpc: Any, wallet: str) -> int:
    """Return the wallet's finalized SOL balance in lamports."""
    result = rpc.call("getBalance", [wallet, {"commitment": "finalized"}])
    if isinstance(result, dict):
        return int(result.get("value", 0) or 0)
    if isinstance(result, int):
        return result
    return 0


def fetch_token_accounts(
    rpc: Any, wallet: str, commitment: str = "finalized"
) -> List[Dict[str, Any]]:
    """Return SPL (legacy and Token-2022) parsed token accounts."""
    calls: List[Tuple[str, List[Any]]] = [
        (
            "getTokenAccountsByOwner",
            [
                wallet,
                {"programId": program},
                {"encoding": "jsonParsed", "commitment": commitment},
            ],
        )
        for program in TOKEN_PROGRAMS
    ]
    accounts: List[Dict[str, Any]] = []
    for result in rpc.batch(calls):
        for account in (result or {}).get("value", []):
            accounts.append(account)
    return accounts


def parse_token_account(account: Any) -> Optional[Dict[str, Any]]:
    """Map a raw ``jsonParsed`` token account into a flat balance record.

    Returns None for malformed entries or entries without a mint.
    """
    if not isinstance(account, dict):
        return None
    parsed = (
        ((account.get("account") or {}).get("data") or {}).get("parsed") or {}
    ).get("info") or {}
    mint = parsed.get("mint") or account.get("mint")
    if not mint:
        return None
    token_amount = parsed.get("tokenAmount") or {}
    try:
        decimals = int(token_amount.get("decimals", account.get("decimals", 0)) or 0)
    except (TypeError, ValueError):
        decimals = 0
    raw_amount = str(token_amount.get("amount", account.get("raw_amount", "0")))
    ui_raw = token_amount.get("uiAmount", account.get("ui_amount"))
    if ui_raw is None:
        ui_value = ui_amount(raw_amount, decimals)
    else:
        try:
            ui_value = float(ui_raw)
        except (TypeError, ValueError):
            ui_value = 0.0
    return {
        "mint": mint,
        "symbol": account.get("symbol") or KNOWN_MINT_SYMBOLS.get(mint, ""),
        "decimals": decimals,
        "raw_amount": raw_amount,
        "ui_amount": ui_value,
    }
