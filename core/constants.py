"""Environment-based API and RPC constants."""

import os

# Optional base URL overrides for public pool APIs.
RAYDIUM_API_BASE = os.environ.get("RAYDIUM_API_BASE", "https://api-v3.raydium.io").rstrip(
    "/"
)
ORCA_API_BASE = os.environ.get("ORCA_API_BASE", "https://api.orca.so/v2/solana").rstrip(
    "/"
)
METEORA_API_BASE = os.environ.get(
    "METEORA_API_BASE", "https://pool-discovery-api.datapi.meteora.ag"
).rstrip("/")

# Optional Solana RPC. Pool screening uses public APIs; wallet/position scans use
# this RPC (or Helius RPC when HELIUS_API_KEY is set).
SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)

# Helius parsed-events history endpoint.
HELIUS_HISTORY_URL = (
    "https://mainnet.helius-rpc.com/v1/parsed-events/transaction-history"
)

# Jupiter Price API (keyless). v2 endpoints have been retired; use v3.
JUPITER_PRICE_V2_URL = os.environ.get(
    "JUPITER_PRICE_V2_URL", "https://api.jup.ag/price/v3"
).rstrip("/")
JUPITER_PRICE_V3_URL = JUPITER_PRICE_V2_URL

# Public pool-discovery endpoints.
RAYDIUM_LIST_V2 = f"{RAYDIUM_API_BASE}/pools/info/list-v2"
RAYDIUM_POOLS_BY_MINT = f"{RAYDIUM_API_BASE}/pools/info/mint"
RAYDIUM_POOLS_BY_IDS = f"{RAYDIUM_API_BASE}/pools/info/ids"
ORCA_POOLS_URL = f"{ORCA_API_BASE}/pools"
METEORA_DISCOVER_URL = f"{METEORA_API_BASE}/pools"

# Raydium program addresses for reference/filtering.
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegB4VNigbyxdXzMRMAq6Gjqsm18N"
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Well-known mints (same set used by the Meteora screener).
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Wormhole WBTC on Solana (validated as base58; the previous constant was 43 chars).
WBTC_MINT = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
# Wormhole WETH on Solana (the previous 45-char constant was not a valid address).
WETH_MINT = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
TAO_MINT = "4zMMC9srt5RiM3NSBBsVpzTbkHhsUoZ9ToGYBcHousX"
WRAPPED_SOL_MINT = SOL_MINT

# Canonical display symbols for well-known mints (shared by pool and wallet code).
KNOWN_MINT_SYMBOLS = {
    SOL_MINT: "SOL",
    USDC_MINT: "USDC",
    USDT_MINT: "USDT",
    WBTC_MINT: "WBTC",
    WETH_MINT: "WETH",
    TAO_MINT: "TAO",
}

# SPL token programs (legacy and Token-2022) and unit constants.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAM_IDS = (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000
