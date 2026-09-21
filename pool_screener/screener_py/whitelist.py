"""Target-token and paired-asset policy."""

import os
from typing import Any, Dict, Iterable, Optional, Tuple

from .token_config_parser import TokenConfigParser
from .token_set import TokenSet, normalize_symbol

DEFAULT_PAIRED_TOKENS = ("SOL", "USDC", "USDT")


class Whitelist:
    """Manage target tokens and allowed counter-assets."""

    def __init__(
        self,
        target_tokens: Optional[Iterable[str]] = None,
        allowed_paired_tokens: Optional[Iterable[str]] = None,
    ) -> None:
        self.targets = TokenSet(target_tokens)
        paired_tokens = (
            allowed_paired_tokens
            if allowed_paired_tokens is not None
            else DEFAULT_PAIRED_TOKENS
        )
        self.paired = TokenSet(paired_tokens)

    @classmethod
    def from_file(cls, filepath: str = "tokens.json") -> "Whitelist":
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Configuration file not found: {filepath}")
        config = TokenConfigParser.load(filepath)
        paired_tokens = config["allowed_paired_tokens"] or DEFAULT_PAIRED_TOKENS
        return cls(config["target_tokens"], paired_tokens)

    def add_target(self, token: str) -> None:
        self.targets.add(token)

    def add_paired(self, token: str) -> None:
        self.paired.add(token)

    def validate_pair(self, token_x: Dict[str, Any], token_y: Dict[str, Any]) -> Tuple[
        bool,
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        str,
    ]:
        x_is_target = self.targets.matches(token_x)
        y_is_target = self.targets.matches(token_y)
        if not x_is_target and not y_is_target:
            return False, None, None, "not in target token whitelist"
        if x_is_target and not y_is_target:
            target, paired = token_x, token_y
        elif y_is_target and not x_is_target:
            target, paired = token_y, token_x
        elif self.paired.matches(token_y):
            target, paired = token_x, token_y
        elif self.paired.matches(token_x):
            target, paired = token_y, token_x
        else:
            target, paired = token_x, token_y
        if not self.paired.matches(paired):
            paired_name = paired.get("symbol") or paired.get("address") or "unknown"
            return (
                False,
                None,
                None,
                f"paired token '{paired_name}' is not an allowed paired asset",
            )
        return True, target, paired, ""

    def matches(self, token: Dict[str, Any]) -> bool:
        return self.targets.matches(token)


__all__ = [
    "DEFAULT_PAIRED_TOKENS",
    "TokenSet",
    "Whitelist",
    "normalize_symbol",
]
