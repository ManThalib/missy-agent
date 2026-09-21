# Solana Multi-DEX Screener Suite

A standard-library Python suite for screening liquidity pools and monitoring wallet positions on Meteora DLMM, Raydium Standard/CLMM, and Orca Whirlpools. The project is split into two independent tools:

1. **Pool Screener**: Discovers and filters pools using provider APIs.
2. **Position Screener**: Analyzes active and historical LP positions for a given wallet using RPC.

The applications do not build, sign, or submit transactions. Pool discovery uses indexed public APIs and is appropriate for screening, not settlement.

## Features

- Concurrent discovery across three independent DEX providers
- Partial-provider failure isolation with warnings
- Token symbol and mint whitelists with paired-asset policy
- Shared TVL, fee, volume, turnover, fee-tier, spacing, volatility, and APR gates
- Provider-independent 0-100 pool scoring
- Table or JSON output
- Optional current and historical wallet LP-position discovery
- Helius Enhanced/Parsed/Raw transaction normalization
- No third-party Python dependencies

## Requirements

- Python 3.8 or newer
- Outbound HTTPS access
- A Solana RPC endpoint for wallet position scans
- A Helius API key only when historical position lifecycle data is required

## Setup

No installation or virtual environment is required. The project is split into two independent tools: `pool_screener` and `position_screener`.

### Pool Screener

Change into the `pool_screener` directory so that `tokens.json` is found:

```bash
cd pool_screener
python3 pool_screener.py --help
python3 pool_screener.py
python3 pool_screener.py --dex orca --pages 1 --page-size 25
python3 pool_screener.py --dex all --watch 60
python3 pool_screener.py --json
```

Meteora's discovery API supports the CLI's `24h` window. A `7d` or `30d`
`--dex all` scan can still return Raydium and Orca results while reporting a
Meteora warning. A Meteora-only CLI scan rejects those incompatible windows.

`--json` and `--watch` cannot be combined because repeated pretty-printed JSON
documents would not form a valid JSON stream.

## Environment

The CLI reads environment variables directly; it does not parse `.env` files.
For local development, export values in the shell or source your own `.env`:

```dotenv
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
HELIUS_API_KEY=your-helius-key
RAYDIUM_API_BASE=https://api-v3.raydium.io
ORCA_API_BASE=https://api.orca.so
METEORA_API_BASE=https://dlmm-api.meteora.ag
```

```bash
set -a
source .env
set +a
cd position_screener
python3 position_screener.py --wallet YOUR_SOLANA_WALLET
```

Pool APIs require no API key. `SOLANA_RPC_URL` selects the current-position RPC.
When it is unset and `HELIUS_API_KEY` is present, wallet scans use Helius RPC.
The three provider base variables override default pool API hosts and are read
when Python imports the package.

## Token Configuration

`tokens.json` is both the target-token whitelist and paired-token allowlist.
Each field accepts an array, a single string, or a single object:

```json
{
  "target_tokens": [
    "SOL",
    {"symbol": "ETH"},
    {"mint": "So11111111111111111111111111111111111111112"},
    {"asset": "My Token / USD"}
  ],
  "allowed_paired_tokens": ["SOL", "USDC", "USDT"]
}
```

Accepted object keys, in precedence order, are `symbol`, `mint`, `address`,
`asset`, and `value`. The first non-empty string is used. Invalid entries are
skipped. Missing, invalid, or empty paired-token configuration falls back to
`SOL`, `USDC`, and `USDT`; missing targets match no pools.

Symbols are case-insensitive. Quote wrappers and outer whitespace are removed.
`WSOL` maps to `SOL`; wrapped ETH/BTC aliases map to canonical `ETH`/`BTC`.
Use mint addresses when distinct wrapped assets must not share a symbol alias.

CLI values replace file values for one run:

```bash
python3 pool_screener.py --tokens SOL,ETH --paired-tokens USDC,SOL
```

An explicitly selected missing or malformed configuration file is a CLI error.

## Wallet Positions

```bash
cd position_screener
python3 position_screener.py --wallet YOUR_SOLANA_WALLET
cd position_screener
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --show-inactive
cd position_screener
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --json
cd position_screener
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --position-history-pages 5
```

Current positions are fetched from Solana RPC. If `HELIUS_API_KEY` is set,
history is also reconstructed. `--position-history-pages 0` means unbounded
pagination until Helius reports completion; a positive value bounds work and
sets `history_complete` to false when more pages remain.

Liquidity, tick/bin bounds, fees, and rewards are raw protocol values. They are
not USD and are not added across token legs. Position score components that
require normalized prices or quote-valued fee deltas remain neutral until those
values are supplied through `pool_enrichment`.

With `--json --wallet`, output is an object containing `pools` and
`position_scan`. Without a wallet, JSON output is an array of pool candidates.

## Python API

```python
from screener_py import FilterConfig, MultiDexScreener, Whitelist

whitelist = Whitelist(
    target_tokens=["SOL", "ETH"],
    allowed_paired_tokens=["USDC", "SOL"],
)
config = FilterConfig(dex="orca", pages=1, page_size=25)
candidates, rejection_counts = MultiDexScreener(
    whitelist=whitelist,
    config=config,
).fetch_and_screen(max_pages=config.pages)
```

```python
from screener_position_py import PositionScanner

scan = PositionScanner().scan(
    "YOUR_SOLANA_WALLET",
    dex="all",
    history_pages=0,
)
```

See `documentation.md` for normalized schemas, public interfaces, error
handling, scoring behavior, and the complete directory map.

## Verification

```bash
python3 -m compileall -q .
cd pool_screener
python3 -m unittest test_screener
python3 pool_screener.py --help
cd ../position_screener
python3 -m unittest test_positions
python3 position_screener.py --help
```
