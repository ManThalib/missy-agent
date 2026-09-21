"""
Solana mint addresses and API constants for Raydium.
Docs: https://docs.raydium.io (REST API surface: api-v3.raydium.io)
"""

import os

# Public read APIs. None requires an API key. Override a base URL when using a
# proxy, cache, or devnet-compatible endpoint.
#   export RAYDIUM_API_BASE="https://api-v3-devnet.raydium.io"
RAYDIUM_API_BASE = os.environ.get(
    "RAYDIUM_API_BASE", "https://api-v3.raydium.io"
).rstrip("/")
ORCA_API_BASE = os.environ.get("ORCA_API_BASE", "https://api.orca.so/v2/solana").rstrip(
    "/"
)
METEORA_API_BASE = os.environ.get(
    "METEORA_API_BASE", "https://pool-discovery-api.datapi.meteora.ag"
).rstrip("/")

# Optional Solana RPC. Pool screening uses public APIs; --wallet uses this RPC
# for current position accounts (or Helius RPC when HELIUS_API_KEY is set).
#   export SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

RAYDIUM_LIST_V2 = f"{RAYDIUM_API_BASE}/pools/info/list-v2"
RAYDIUM_POOLS_BY_MINT = f"{RAYDIUM_API_BASE}/pools/info/mint"
RAYDIUM_POOLS_BY_IDS = f"{RAYDIUM_API_BASE}/pools/info/ids"
ORCA_POOLS_URL = f"{ORCA_API_BASE}/pools"
METEORA_DISCOVER_URL = f"{METEORA_API_BASE}/pools"

# Raydium CLMM program, CPMM program, AMM v4 program (for reference / filtering)
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegB4VNigbyxdXzMRMAq6Gjqsm18N"
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Well-known mints (same set as the Meteora screener)
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
