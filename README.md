# Solana Multi-DEX Screener Suite

A standard-library Python suite for screening liquidity pools and monitoring wallet positions on Meteora DLMM, Raydium Standard/CLMM, and Orca Whirlpools. The project is split into three independent tools:

1. **Pool Screener**: Discovers, normalizes, and scores pools using provider
   APIs. Missy enforces the `missy_policy.json` eligibility gates, emits a
   default 0-100 score (plus an experimental fee-capture score) for every
   candidate, and still emits failing pools with `eligible=false` so the
   audit trail survives. Strategy policy lives in Sheldon.
2. **Position Screener**: Analyzes active and historical LP positions for a given wallet using RPC, recomputes live pending fees/rewards per protocol, and attaches a versioned `position_features` block to every position.
3. **Wallet Screener**: Fetches SOL, SPL, and Token-2022 balances, prices them in USD via Jupiter, and emits all assets sorted by USD value. Dust filtering is Sheldon's policy.

The applications do not build, sign, or submit transactions. Pool discovery uses indexed public APIs and is appropriate for screening, not settlement.

## Features

- Concurrent discovery across three independent DEX providers
- Partial-provider failure isolation with warnings
- Token symbol, mint, and alias parsing with paired-asset metadata
- Normalized TVL, fee, volume, turnover, fee-tier, spacing, volatility, and APR observations
- Policy-driven eligibility gates plus a default 0-100 pool score (and an experimental fee-capture score); rejected pools are still emitted
- Table or JSON output
- Optional current and historical wallet LP-position discovery with `wallet_id` tagging
- Live pending fee/reward recomputation per protocol (Raydium CLMM, Meteora DLMM, Orca Whirlpool) plus pool-scan enrichment and a versioned position feature vector
- On-disk TTL response cache shared by provider fetches and RPC reads
- Helius Enhanced/Parsed/Raw transaction normalization
- No third-party Python dependencies

## Requirements

- Python 3.8 or newer
- Outbound HTTPS access
- A Solana RPC endpoint for wallet position scans
- A Helius API key only when historical position lifecycle data is required
- A Solana wallet public key for wallet scans (or set `WALLET_PUBLIC_KEY`)

## Setup

No installation or virtual environment is required. The project is split into three independent tools: `pool_screener`, `position_screener`, and `wallet_screener`.

### Pool Screener

Change into the `pool_screener` directory so that `tokens.json` is found:

```bash
cd pool_screener
python3 pool_screener.py --help
python3 pool_screener.py
python3 pool_screener.py --dex orca --pages 1 --page-size 25
python3 pool_screener.py --dex all --watch 60
python3 pool_screener.py --json
python3 pool_screener.py --dex all --pool-type concentrated --pages 1
```

`run_meteora.sh` runs `--dex all --json --pages 1` and writes a
timestamped `pool_scan-<TS>.json` under `/data/missy-data/pool_screens`.

Meteora's discovery API supports the CLI's `24h` window. A `7d` or `30d`
`--dex all` scan can still return Raydium and Orca results while reporting a
Meteora warning. A Meteora-only CLI scan rejects those incompatible windows.

`--json` and `--watch` cannot be combined because repeated pretty-printed JSON
documents would not form a valid JSON stream.

Filter flags (`--min-tvl`, `--max-tvl`, `--min-fee-tvl`, `--min-daily-fee`,
`--min-volume`, `--min-apr`, `--min-bin-step`/`--max-bin-step`,
`--min-fee-pct`/`--max-fee-pct`, `--max-volatility`) are still parsed for CLI
compatibility. Eligibility is enforced from `missy_policy.json`
(`pool_eligibility` gates); pools that fail are still emitted with
`eligible=false` and a `rejected_reason`. Every candidate carries the policy
default score (`score`, `score_breakdown`, `score_model`/`score_version`) plus
the experimental fee-capture score (`new_fc_score`, `new_score_breakdown`).

## Environment

The CLI reads environment variables directly; it does not parse `.env` files.
For local development, export values in the shell or source your own `.env`:

```dotenv
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
HELIUS_API_KEY=your-helius-key
WALLET_PUBLIC_KEY=your-wallet-public-key
RAYDIUM_API_BASE=https://api-v3.raydium.io
ORCA_API_BASE=https://api.orca.so/v2/solana
METEORA_API_BASE=https://pool-discovery-api.datapi.meteora.ag
JUPITER_PRICE_V2_URL=https://api.jup.ag/price/v3
MISSY_CACHE_DIR=/tmp/missy-cache
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
when Python imports the package. `JUPITER_PRICE_V2_URL` overrides the Jupiter
Price endpoint; the default is the v3 URL (`https://api.jup.ag/price/v3`) and
the client parses both v2 (`data` map/list) and v3 (flat mint map) shapes.

## Token Configuration

`tokens.json` carries token metadata (symbols, mints, aliases). Missy parses
every entry into match literals; Sheldon owns whitelist/paired-token policy.
Each field accepts an array, a single string, or a single object:

```json
{
  "target_tokens": [
    {"asset": "SOL", "mint": "So11111111111111111111111111111111111111112", "aliases": ["WSOL", "wSOL", "Wrapped SOL"]},
    {"asset": "USDC", "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "aliases": ["USDC.e", "USDCE"]}
  ],
  "allowed_paired_tokens": ["SOL", "USDC", "USDT"]
}
```

Accepted object keys, in precedence order, are `symbol`, `mint`, `address`,
`asset`, and `value`. The first non-empty string is used, and every `aliases`
array entry is added as an extra match literal. Invalid entries are
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
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --wallet-id main --dex all
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --show-inactive
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --json
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --position-history-pages 5
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --closure-state-path position_closure_state.json
python3 position_screener.py --wallet YOUR_SOLANA_WALLET --watch 60
```

`run_positions.sh` scans `--dex all --json` (with `--wallet-id "$WALLET_ID"`,
default `main`) and writes `position_scan-<TS>.json` under
`/data/missy-data/position_scans`.

## Wallet Screener

```bash
cd wallet_screener
python3 main.py --help
python3 main.py --wallet YOUR_SOLANA_WALLET
python3 main.py --wallet YOUR_SOLANA_WALLET --json
python3 main.py --wallet YOUR_SOLANA_WALLET --wallet-id main --output-dir /data/missy-data/wallet_screens
python3 main.py --wallet YOUR_SOLANA_WALLET --no-output-file
```

(When run as a module from the repo root: `python3 -m wallet_screener.main --help`.)

The scanner fetches SOL and all SPL/Token-2022 balances, prices them in USD via
the Jupiter Price API (default `https://api.jup.ag/price/v3`, no key required),
and emits all assets sorted by USD value. Dust filtering is Sheldon's policy:
`--threshold` is kept for CLI compatibility but the scanner ignores it, and
`--include-dust` is a no-op that preserves the flag. Every scan record carries
`wallet_id` (default `main`; `run_wallet.sh` writes
`wallet_screen-<id>-<TS>.json` for mirror wallets and keeps the unsuffixed
name for `main`). JSON/table screens are also written to `--output-dir`
(default `/data/missy-data/wallet_screens`) unless `--no-output-file` is set,
and `run_wallet.sh` refreshes the symbol-keyed `wallet_balances.json` cache.
The `$WALLET_PUBLIC_KEY` environment variable is also supported.

Current positions are fetched from Solana RPC. If `HELIUS_API_KEY` is set,
history is also reconstructed. `--position-history-pages 0` means unbounded
pagination until Helius reports completion; a positive value bounds work and
sets `history_complete` to false when more pages remain. Active positions get
live pending fees/rewards recomputed per protocol — Raydium CLMM from PoolState
plus boundary tick arrays (`raydium_pending.py`), Meteora DLMM from the per-bin
fee/reward loop (`meteora_pending.py`), Orca Whirlpools from growth-inside
quotes (`orca_pending.py`); a failed provider's positions are marked
`status=unknown` instead of reading as out-of-range evidence. Positions are
enriched from the latest `/data/missy-data/pool_screens/pool_scan-*.json`
(current value, in-range flag, `fees_usd`/`rewards_usd`, token prices); one bad
pool record never aborts the scan. Every position also carries a versioned
`position_features` block (range geometry, value/fees/rewards, pool context,
`gaps` for missing inputs) for downstream scoring.

Liquidity, tick/bin bounds, fees, and rewards are raw protocol values. They are
not USD and are not added across token legs. Position score components that
require normalized prices or quote-valued fee deltas remain neutral until those
values are supplied through `pool_enrichment`. Every `PositionScan` and
`LiquidityPosition` carries `wallet_id` (default `main`) for the multi-wallet
mirror.

Pool `--json` output is an array of pool candidates. Position `--json` output
is a `PositionScan` object (`wallet`, `wallet_id`, `positions`, `errors`,
`history_complete`). Wallet `--json` output is a scan object (`wallet`,
`wallet_id`, `total_usd`, `asset_count`, `assets`).

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
    closure_state_path="position_closure_state.json",
    wallet_id="main",
)
```

```python
from wallet_screener import WalletScanner

result = WalletScanner(wallet_id="main").scan("YOUR_SOLANA_WALLET")
print(result["wallet_id"], result["total_usd"], result["asset_count"])
```

See `documentation.md` for normalized schemas, public interfaces, error
handling, scoring behavior, and the complete directory map.

## Verification

```bash
python3 -m compileall -q .
cd pool_screener && python3 -m unittest test_screener
python3 pool_screener.py --help
cd ../position_screener && python3 -m unittest test_positions
python3 position_screener.py --help
cd ../wallet_screener && python3 -m unittest test_wallet_screener
python3 main.py --help
cd .. && python3 -m unittest core.test_core
```
