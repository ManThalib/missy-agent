"""Validation and normalization for token JSON configuration."""

import json
from typing import Any, Dict, List

from .token_set import clean_token_literal

TOKEN_VALUE_KEYS = ("symbol", "mint", "address", "asset", "value")


class TokenConfigParser:
    """Parse token arrays while safely skipping malformed individual entries."""

    @classmethod
    def load(cls, filepath: str) -> Dict[str, List[str]]:
        try:
            with open(filepath, "r", encoding="utf-8") as config_file:
                payload = json.load(config_file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in token configuration {filepath}: "
                f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("Token configuration must be a JSON object")
        return {
            "target_tokens": cls._parse_entries(payload.get("target_tokens", [])),
            "allowed_paired_tokens": cls._parse_entries(
                payload.get("allowed_paired_tokens", [])
            ),
        }

    @classmethod
    def _parse_entries(cls, entries: Any) -> List[str]:
        if isinstance(entries, (str, dict)):
            entries = [entries]
        if not isinstance(entries, list):
            return []
        return [
            token for token in (cls._parse_entry(entry) for entry in entries) if token
        ]

    @staticmethod
    def _parse_entry(entry: Any) -> str:
        value = entry
        if isinstance(entry, dict):
            value = next(
                (
                    entry[key]
                    for key in TOKEN_VALUE_KEYS
                    if isinstance(entry.get(key), str) and entry[key].strip()
                ),
                "",
            )
        if not isinstance(value, str):
            return ""
        return clean_token_literal(value)
