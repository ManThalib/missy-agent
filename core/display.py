"""Shared terminal display helpers."""

from __future__ import annotations


def truncate(s: str, max_len: int) -> str:
    """Truncate *s* to *max_len* characters, appending ``...`` when shortened."""
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    if max_len <= 3:
        return "." * max_len
    return s[: max_len - 3] + "..."
