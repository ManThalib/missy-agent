"""Token symbol and mint matching."""

from typing import Any, Dict, Iterable, Optional, Set

from .constants import SOL_MINT, USDC_MINT, USDT_MINT

MINT_TO_SYMBOL = {
    SOL_MINT: "SOL",
    USDC_MINT: "USDC",
    USDT_MINT: "USDT",
    "7vfCXTUXx5WJV5JADkdpS6b7S7jAVL5KZ4pjkQ6BooHSJ": "WETH",
    "4zMMC9srt5RiM3NSBBsVpzTbkHhsUoZ9ToGYBcHousX": "TAO",
    "3NZ9JMVBmGAqocybic2c4L9J6TZrvuoBsBOWi3dcAFq": "WBTC",
}
SYMBOL_ALIASES = {
    "WSOL": "SOL",
    "WETH": "ETH",
    "WBTC": "BTC",
    "XBTC": "BTC",
    "CBBTC": "BTC",
}
SYMBOL_TO_MINT = {symbol: mint for mint, symbol in MINT_TO_SYMBOL.items()}
QUOTE_PAIRS = {'"': '"', "'": "'", "`": "`"}


def clean_token_literal(value: str) -> str:
    """Trim whitespace and quote wrappers without altering inner content."""
    cleaned = value.strip()
    while cleaned and cleaned[0] in QUOTE_PAIRS:
        cleaned = cleaned[1:].strip()
    while cleaned and cleaned[-1] in QUOTE_PAIRS.values():
        cleaned = cleaned[:-1].strip()
    return cleaned


def normalize_symbol(symbol: str) -> str:
    """Normalize quote wrappers, case, and well-known wrapped aliases."""
    normalized = clean_token_literal(symbol or "").upper()
    return SYMBOL_ALIASES.get(normalized, normalized)


class TokenSet:
    """Store and match token mint addresses and complete symbol literals."""

    def __init__(self, tokens: Optional[Iterable[str]] = None) -> None:
        self.addresses: Set[str] = set()
        self.symbols: Set[str] = set()
        for token in tokens or ():
            self.add(token)

    def add(self, token: str) -> None:
        if not isinstance(token, str):
            return
        cleaned = clean_token_literal(token)
        if not cleaned:
            return
        if 32 <= len(cleaned) <= 44 and not any(char.isspace() for char in cleaned):
            self.addresses.add(cleaned)
            known_symbol = MINT_TO_SYMBOL.get(cleaned)
            if known_symbol:
                self.symbols.add(normalize_symbol(known_symbol))
            return
        symbol = normalize_symbol(cleaned)
        self.symbols.add(symbol)
        mint = SYMBOL_TO_MINT.get(symbol)
        if mint:
            self.addresses.add(mint)

    def replace(self, tokens: Iterable[str]) -> None:
        self.addresses.clear()
        self.symbols.clear()
        for token in tokens:
            self.add(token)

    def matches(self, token: Dict[str, Any]) -> bool:
        if not self.addresses and not self.symbols:
            return False
        address = token.get("address", "")
        return (
            address in self.addresses
            or normalize_symbol(token.get("symbol") or "") in self.symbols
        )
