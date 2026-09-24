"""Configuration constants for the wallet scanner."""

import os

from core.constants import (  # noqa: F401  (re-exported for scanner consumers)
    JUPITER_PRICE_V2_URL,
    LAMPORTS_PER_SOL,
    SOLANA_RPC_URL,
    SOL_DECIMALS,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    TOKEN_PROGRAM_IDS,
    WRAPPED_SOL_MINT,
)

# Assets whose total USD value is at or below this threshold are filtered out.
USD_THRESHOLD = 0.10

# Default Jupiter endpoint (re-exported for convenience/overrides).
JUPITER_PRICE_URL = JUPITER_PRICE_V2_URL

# Number of mints to request per Jupiter price call.
PRICE_BATCH_SIZE = 50

# HTTP/RPC defaults.
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_RATE_LIMIT_SECONDS = 0.15

# Solana RPC: prefer Helius when a key is present, otherwise SOLANA_RPC_URL.
HELIUS_API_KEY_ENV = "HELIUS_API_KEY"
SOLANA_RPC_URL_ENV = "SOLANA_RPC_URL"
WALLET_ENV = "WALLET_PUBLIC_KEY"
DEFAULT_SOLANA_RPC_URL = SOLANA_RPC_URL

# Native SOL is wrapped as WSOL when priced through Jupiter.
SOL_MINT = WRAPPED_SOL_MINT


def resolve_rpc_url() -> str:
    """Resolve the RPC URL, preferring Helius when an API key is set."""
    explicit = os.environ.get(SOLANA_RPC_URL_ENV)
    if explicit:
        return explicit
    key = os.environ.get(HELIUS_API_KEY_ENV)
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    return DEFAULT_SOLANA_RPC_URL


def resolve_wallet(explicit: str = "") -> str:
    """Return the explicit wallet or fall back to the environment variable."""
    return explicit or os.environ.get(WALLET_ENV, "")
