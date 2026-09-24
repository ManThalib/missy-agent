"""Shared coercion and base58 helpers for Solana payload decoding."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Tuple

from core.solana import B58_ALPHABET, b58decode, b58encode, discriminator

__all__ = [
    "B58_ALPHABET",
    "as_items",
    "as_mapping",
    "b58decode",
    "b58encode",
    "discriminator",
    "to_int",
    "token_amount",
    "ui_amount_to_raw",
]


def as_mapping(value: Any) -> Mapping[str, Any]:
    """Return *value* when it is a mapping, otherwise an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def as_items(value: Any) -> Iterable[Any]:
    """Iterate *value* only when it is a list or tuple."""
    return value if isinstance(value, (list, tuple)) else ()


def to_int(value: Any, default: int = 0) -> int:
    """Best-effort integer coercion that never raises."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def token_amount(value: Any) -> Tuple[int, int]:
    """Return ``(raw_amount, decimals)`` from a token-amount mapping."""
    amount = as_mapping(value)
    raw = amount.get("amount", amount.get("tokenAmount", 0))
    return to_int(raw), to_int(amount.get("decimals"))


def ui_amount_to_raw(value: Any, decimals: int) -> int:
    """Scale a decimal UI amount into integer base units."""
    try:
        amount = Decimal(str(value)) * (Decimal(10) ** decimals)
        if not amount.is_finite() or amount < 0:
            return 0
        return int(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0
