"""Token symbol and mint matching.

Mint addresses are authoritative whenever a token supplies one and the
configured symbol has a canonical mint. Symbol-only matching remains for
symbols without a known mint, so a look-alike token cannot pass by reusing
a canonical symbol such as SOL or USDC under a different mint.
"""

from typing import Any, Dict, Iterable, Optional, Set

from core.constants import KNOWN_MINT_SYMBOLS

SYMBOL_ALIASES = {
    "WSOL": "SOL",
    "WETH": "ETH",
    "WBTC": "BTC",
    "XBTC": "BTC",
    "CBBTC": "BTC",
    "CBXRP": "XRP",
}
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


# Canonical mint per normalized symbol (WBTC is stored under "BTC").
MINT_TO_SYMBOL = dict(KNOWN_MINT_SYMBOLS)
SYMBOL_TO_MINT: Dict[str, str] = {
    normalize_symbol(symbol): mint for mint, symbol in MINT_TO_SYMBOL.items()
}


class TokenSet:
    """Store and match token mint addresses and complete symbol literals."""

    def __init__(self, tokens: Optional[Iterable[str]] = None) -> None:
        self.addresses: Set[str] = set()
        self.symbols: Set[str] = set()
        self.symbol_mints: Dict[str, str] = {}
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
                symbol = normalize_symbol(known_symbol)
                self.symbols.add(symbol)
                self.symbol_mints.setdefault(symbol, cleaned)
            return
        symbol = normalize_symbol(cleaned)
        self.symbols.add(symbol)
        mint = SYMBOL_TO_MINT.get(symbol)
        if mint:
            self.addresses.add(mint)
            self.symbol_mints.setdefault(symbol, mint)

    def replace(self, tokens: Iterable[str]) -> None:
        self.addresses.clear()
        self.symbols.clear()
        self.symbol_mints.clear()
        for token in tokens:
            self.add(token)

    def matches(self, token: Dict[str, Any]) -> bool:
        if not self.addresses and not self.symbols:
            return False
        address = token.get("address", "") or ""
        if address and address in self.addresses:
            return True
        symbol = normalize_symbol(token.get("symbol") or "")
        if symbol not in self.symbols:
            return False
        required = self.symbol_mints.get(symbol)
        if required is None or not address:
            return True
        return address == required
