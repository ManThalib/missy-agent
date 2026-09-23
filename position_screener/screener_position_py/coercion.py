"""Shared coercion and base58 helpers for Solana payload decoding."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Tuple

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


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


def b58decode(value: str, *, strict: bool = False) -> bytes:
    """Decode base58 *value*; raise when *strict*, otherwise return ``b""``."""
    number = 0
    try:
        for char in value:
            number = number * 58 + B58_ALPHABET.index(char)
    except ValueError as exc:
        if strict:
            raise ValueError("address is not valid base58") from exc
        return b""
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def b58encode(value: bytes) -> str:
    """Encode *value* using the Bitcoin/Solana base58 alphabet."""
    leading = len(value) - len(value.lstrip(b"\0"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = B58_ALPHABET[remainder] + encoded
    return "1" * leading + encoded


def discriminator(name: str) -> bytes:
    """Return the 8-byte Anchor account discriminator for *name*."""
    return hashlib.sha256(f"account:{name}".encode("ascii")).digest()[:8]
