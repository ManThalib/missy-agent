"""Solana/base58 helpers used across screeners."""

from __future__ import annotations

import hashlib

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_SET = set(B58_ALPHABET)


def b58encode(value: bytes) -> str:
    """Encode *value* using the Bitcoin/Solana base58 alphabet."""
    leading = len(value) - len(value.lstrip(b"\0"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = B58_ALPHABET[remainder] + encoded
    return "1" * leading + encoded


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


def discriminator(name: str) -> bytes:
    """Return the 8-byte Anchor account discriminator for *name*."""
    return hashlib.sha256(f"account:{name}".encode("ascii")).digest()[:8]


def is_valid_solana_address(value: str) -> bool:
    """Return True if *value* is a valid 32-byte Solana base58 address."""
    if not value or not isinstance(value, str):
        return False
    if len(value) > 44 or any(ch not in _B58_SET for ch in value):
        return False
    try:
        return len(b58decode(value, strict=True)) == 32
    except ValueError:
        return False
