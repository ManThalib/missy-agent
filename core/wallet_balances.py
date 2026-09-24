"""Reusable low-level Solana wallet balance fetching helpers.

These functions perform the raw RPC work and return plain data structures so
that consumers can map results into their own models without re-implementing the
``jsonParsed`` token-account traversal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .constants import LAMPORTS_PER_SOL, TOKEN_PROGRAM_IDS

# Backwards-compatible alias for consumers that refer to the program tuple.
TOKEN_PROGRAMS = TOKEN_PROGRAM_IDS

__all__ = [
    "LAMPORTS_PER_SOL",
    "TOKEN_PROGRAM_IDS",
    "TOKEN_PROGRAMS",
    "fetch_sol_balance_lamports",
    "fetch_token_accounts",
    "ui_amount",
]


def ui_amount(raw_amount: str, decimals: int) -> float:
    """Convert a raw integer token amount to a decimal UI amount."""
    try:
        raw = float(raw_amount)
    except (TypeError, ValueError):
        return 0.0
    if decimals > 0:
        return raw / (10 ** decimals)
    return raw


def fetch_sol_balance_lamports(rpc: Any, wallet: str) -> int:
    """Return the wallet's finalized SOL balance in lamports."""
    result = rpc.call("getBalance", [wallet, {"commitment": "finalized"}])
    if isinstance(result, dict):
        return int(result.get("value", 0) or 0)
    if isinstance(result, int):
        return result
    return 0


def fetch_token_accounts(rpc: Any, wallet: str) -> List[Dict[str, Any]]:
    """Return SPL (legacy and Token-2022) parsed token accounts."""
    calls: List[Tuple[str, List[Any]]] = [
        (
            "getTokenAccountsByOwner",
            [
                wallet,
                {"programId": program},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )
        for program in TOKEN_PROGRAMS
    ]
    accounts: List[Dict[str, Any]] = []
    for result in rpc.batch(calls):
        for account in (result or {}).get("value", []):
            accounts.append(account)
    return accounts
