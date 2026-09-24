# Architecture and Technical Documentation

## System Overview

The project has three cooperating packages plus a shared core:

- `screener_py` discovers, normalizes, filters, scores, and displays pools.
- `screener_position_py` discovers wallet LP positions, normalizes Helius
  transactions, tracks closures, and exposes conservative position analytics.
- `wallet_scanner` fetches SOL/SPL/Token-2022 balances, prices them via Jupiter
  Price API v2, filters dust, and reports a sorted USD portfolio.
- `core` provides the shared HTTP, RPC, Helius, base58/Solana, normalization,
  display, price, and wallet-balance helpers reused by all three tools.

Only the Python standard library is used. Pool providers and current-position
providers execute in bounded `ThreadPoolExecutor` workers. A failed source does
not discard successful results. Pool discovery and current-position discovery
raise only when every selected provider fails; a successful empty response is
still a success.

The CLI does not submit transactions. Pool APIs are indexed data and must not
be used as settlement state. Wallet scans can write the local
`position_closure_state.json` file atomically.

## Directory Map

```text
Missy-agent/
|-- README.md
|-- documentation.md
|-- AGENTS.md
|-- core/                               # Shared utilities
|   |-- __init__.py
|   |-- constants.py
|   |-- display.py
|   |-- helius.py
|   |-- http.py
|   |-- normalize.py
|   |-- prices.py
|   |-- rpc.py
|   |-- solana.py
|   |-- test_core.py
|   `-- wallet_balances.py
|-- pool_screener/
|   |-- pool_screener.py                # Pool CLI entrypoint
|   |-- tokens.json                     # Token policy
|   |-- test_screener.py
|   `-- screener_py/                    # Public pool facade and logic
|       |-- __init__.py
|       |-- constants.py                # Compatibility re-exports from core
|       |-- candidate.py
|       |-- candidate_json_encoder.py
|       |-- filter_config.py
|       |-- pool_score_input.py
|       |-- pool_scorer.py
|       |-- score_breakdown.py
|       |-- scoring_config.py
|       |-- token_config_parser.py
|       |-- token_set.py
|       |-- whitelist.py
|       |-- multi_dex_screener.py
|       |-- display.py
|       |-- client.py
|       |-- config.py
|       |-- scoring.py
|       |-- screener.py
|       |-- normalize_utils.py          # Compatibility re-exports from core
|       `-- clients/
|           |-- __init__.py
|           |-- base_client.py          # Compatibility re-export from core.http
|           |-- raydium_client.py
|           |-- orca_client.py
|           |-- meteora_client.py
|           `-- multi_dex_client.py
|-- position_screener/
|   |-- position_screener.py            # Position CLI entrypoint
|   |-- position_closure_state.json     # Local closure snapshot
|   |-- test_positions.py
|   `-- screener_position_py/           # Public position facade and logic
|       |-- __init__.py
|       |-- liquidity_position.py
|       |-- position_scan.py
|       |-- rpc_client.py               # Compatibility re-export from core.rpc
|       |-- helius_history_client.py    # Compatibility re-export from core.helius
|       |-- helius_parser.py
|       |-- helius_types.py
|       |-- position_scanner.py
|       |-- models.py
|       |-- rpc.py
|       |-- scanner.py
|       |-- display.py
|       |-- coercion.py                 # Compatibility re-exports from core.solana
|       `-- analytics/
|           |-- scoring.py
|           |-- trader_scoring.py
|           `-- closure_state.py
`-- wallet_scanner/
    |-- main.py                         # Wallet scanner CLI entrypoint
    |-- test_wallet_scanner.py
    |-- config.py
    |-- models.py
    |-- rpc_client.py                   # SolanaRpcClient with retry/backoff
    |-- price_fetcher.py              # TokenPriceFetcher (Jupiter v2)
    |-- balance_calculator.py         # USD valuation and threshold filter
    `-- wallet_scanner.py             # Orchestrator
```

Compatibility modules contain imports only. Canonical implementations live in
the modules named in the map.

## Pool APIs

### `FilterConfig`

`FilterConfig` contains discovery and screening settings. Important fields:

| Field | Type | Meaning |
|---|---|---|
| `min_tvl`, `max_tvl` | `float` | USD TVL limits; zero maximum disables the cap. |
| `min_fee_tvl` | `float` | Minimum daily LP fee/TVL percentage. |
| `min_daily_fee` | `float` | Minimum normalized daily LP fees in USD. |
| `min_volume_usd` | `float` | Minimum provider-window volume in USD. Missing/zero volume fails a positive minimum. |
| `min_apr` | `float` | Minimum adjusted APR produced by `PoolScorer`. |
| `min_bin_step`, `max_bin_step` | `int` | Meteora bin-step or concentrated-pool tick-spacing limits. Standard pools skip this gate. |
| `min_fee_pct`, `max_fee_pct` | `float` | Pool fee-tier percentage limits. |
| `max_volatility` | `float` | Maximum normalized price excursion percentage. |
| `max_turnover_ratio` | `float` | Maximum normalized daily volume/TVL ratio. |
| `timeframe` | `str` | CLI values are `24h`, `7d`, or `30d`. |
| `dex` | `str` | `all`, `meteora`, `raydium`, or `orca`. |
| `pages`, `page_size`, `sort_field`, `timeout` | mixed | Discovery controls. |

`FilterConfig.window` maps `24h`, `7d`, and `30d` to Raydium's `day`, `week`,
and `month` aggregate keys. `from_args(namespace)` copies all supported fields
and maps CLI names `min_volume` and `sort` to their configuration fields.

`DEFAULT_CONFIG` is exported for CLI defaults. Treat it as read-only.

### Provider Clients

All clients expose:

```python
fetch_pools(config: FilterConfig, max_pages: int = 5) -> list[dict]
```

- `RaydiumClient` follows `nextPageId` for Standard and concentrated pools.
- `OrcaClient` follows `meta.next`.
- `MeteoraClient` follows `after_key` and accepts `5m`, `30m`, or `24h`
  programmatically; the shared CLI exposes `24h` for Meteora.
- Every client waits 150 ms between pages and returns copied dictionaries with
  an internal `_dex` tag rather than mutating provider payloads.
- `RaydiumClient.fetch_pools_by_mint(...)` performs targeted lookup and supports
  an injectable endpoint through `pools_by_mint_url`.

`MultiDexClient.fetch_pools(config, max_pages)` executes selected providers
concurrently, preserves deterministic provider order, and stores source errors
in `errors: dict[str, str]`. It raises `RuntimeError` only if no selected
provider completes successfully.

### Normalization

```python
normalize_pool(pool: dict[str, Any], window: str = "day") -> dict[str, Any]
```

The function validates numeric fields as finite values and raises `ValueError`
for malformed records. `screen_all()` isolates that error to the affected pool
and adds an `invalid provider payload` rejection instead of losing the batch.

The normalized mapping uses these keys:

| Key | Type | Unit/meaning |
|---|---|---|
| `dex` | `str` | Provider name. |
| `pool_address`, `name`, `pool_type` | `str` | Pool identity and display data. |
| `token_x`, `token_y` | `dict` | `symbol` and `address`. |
| `tvl` | `float` | USD. |
| `fee`, `volume` | `float` | USD over the selected provider window. |
| `fee_tvl_ratio` | `float` | Window fee/TVL percentage from source data. |
| `apr` | `float` | Provider-reported annual percentage. |
| `fee_pct` | `float` | Fee tier percentage. |
| `fee_rate` | `float` | Decimal fraction. Orca millionths are converted. |
| `tick_spacing`, `bin_step` | `int` | One canonical concentrated-spacing value; `bin_step` is retained for compatibility. |
| `volatility` | `float` | Price excursion percentage where derivable. |
| `volatility_available` | `bool` | Whether excursion was observed rather than defaulted. |
| `lp_fee_share` | `float` | Optional fraction of gross fees accruing to LPs; defaults to 1. |

Raydium excursion uses period `priceMin`/`priceMax`; Orca uses its seven-day
price history only for the matching window; Meteora uses `min_price`,
`max_price`, and `pool_price` when available.

### `MultiDexScreener`

```python
screen_pool(pool: dict) -> tuple[Candidate | None, str]
screen_all(pools: list[dict]) -> tuple[list[Candidate], dict[str, int]]
fetch_and_screen(max_pages: int = 5) -> tuple[list[Candidate], dict[str, int]]
```

The gate order is token policy, TVL, normalized LP fee/TVL, daily LP fees,
volume, turnover, concentrated spacing, fee tier, volatility, and adjusted APR.
Accepted candidates are sorted by descending score. `RaydiumScreener` and
`MeteoraScreener` are compatibility aliases for `MultiDexScreener`, not
provider-specific subclasses.

### Pool Scoring

`PoolScoreInput` is an immutable normalized observation. `PoolScorer.score()`
returns immutable `ScoreBreakdown` with:

- `total`, `yield_score`, `depth_score`, `efficiency_score`, `risk_score`
- `realized_fee_apr`, `adjusted_apr`, `effective_tvl`
- `daily_turnover`, `active_liquidity_factor`

The score combines 25-point yield, depth, efficiency, and risk buckets.
Reported APR is capped and blended with fee-derived APR. Missing volatility
receives a neutral risk assumption rather than being interpreted as observed
zero volatility. `ScoringConfig` controls weights and anchors.

### Candidate and Display

`Candidate` contains normalized metrics, score components, and attached wallet
positions. `to_dict()` returns a detached JSON-compatible dictionary.
`CandidateJSONEncoder` supports standard `json.dump()`.

`print_results()` renders pools. `print_positions()` displays active positions
by default and adds inactive/closed rows with `show_inactive=True`. Raw token
legs remain labeled as raw; the display does not present them as USD or PnL.

## Token Policy APIs

### `TokenConfigParser`

```python
TokenConfigParser.load(filepath: str) -> dict[str, list[str]]
```

The JSON root must be an object. `target_tokens` and `allowed_paired_tokens`
accept arrays, a string, or an object. Object keys are checked in this order:
`symbol`, `mint`, `address`, `asset`, `value`; the first non-empty string wins.
Malformed entries are skipped. Invalid JSON raises `ValueError` with line and
column; a missing file raises `FileNotFoundError`.

### `TokenSet` and `Whitelist`

`TokenSet.add()`, `replace()`, and `matches()` maintain symbol/mint matching.
Whitespace-free 32-44 character values are treated as likely mint addresses;
this is a lightweight heuristic, not full public-key validation.

`Whitelist.from_file()` builds target and paired sets. `validate_pair(x, y)`
returns `(accepted, target_token, paired_token, reason)`. An empty target set
matches nothing. Empty or invalid paired configuration uses `SOL`, `USDC`, and
`USDT` defaults.

## Wallet Scanner APIs

### `WalletScanner`

```python
from wallet_scanner import WalletScanner

result = WalletScanner().scan("YOUR_SOLANA_WALLET")
```

`scan()` returns a dictionary containing the wallet address, the count of assets
above the threshold, the total USD value of those assets, and a list of
`TokenBalance` records serialized as dictionaries. Assets are sorted by
`total_value_usd` in descending order.

The constructor accepts:

- `rpc_url`: Solana RPC URL. Defaults to `SOLANA_RPC_URL` or Helius RPC if
  `HELIUS_API_KEY` is set.
- `price_url`: Jupiter Price API v2 base URL. Defaults to `JUPITER_PRICE_V2_URL`.
- `threshold_usd`: Assets with total USD value at or below this value are
  filtered out. Defaults to `$0.10`.
- `timeout`: Per-request timeout in seconds.
- `rpc_client` / `price_fetcher`: Optional injected dependencies for testing or
  advanced use.

The scanner validates that the wallet is a 32-byte base58 Solana address, fetches
the native SOL balance plus all SPL and Token-2022 balances, prices mints in
batches of 50 via Jupiter, and applies the threshold. Pricing failures are not
fatal; affected assets are returned with `price_usd=0.0` and filtered out unless
`threshold_usd` is set to `0.0`.

### `TokenBalance`

A dataclass representing a single asset:

- `mint`: SPL mint address (native SOL uses the wrapped-SOL mint address).
- `symbol`: Human-readable symbol when available.
- `decimals`, `amount_raw`, `amount_ui`: Token amount and precision.
- `price_usd`, `total_value_usd`: USD price and holding value.
- `is_native_sol`: `True` for the native SOL balance.

### `TokenPriceFetcher` / `SolanaRpcClient`

These one-class-per-file helpers wrap the shared `core.prices.JupiterPriceClient`
and `core.rpc.RpcClient` with wallet-specific retry/backoff and batch sizing.
They are not normally used directly but can be injected into `WalletScanner`.

## Position APIs

### `RpcClient`

```python
call(method: str, params: list[Any]) -> Any
batch(calls: Iterable[tuple[str, list[Any]]]) -> list[Any]
```

Request-ID reservation is synchronized, so one client can be used by the
scanner's worker threads. Batch results are restored to request order. Missing
or duplicate response IDs, transport errors, malformed response roots, and RPC
error objects raise `RuntimeError`. Timeout is per HTTP request; retries are not
automatic. The canonical implementation lives in `core.rpc`; the per-module
`rpc_client.py` files are thin compatibility re-exports.

### Shared Core Helpers

- `core.http.HttpJsonClient` — standard-library JSON GET/POST with a 150 ms
  per-page pause.
- `core.prices.JupiterPriceClient` — keyless Jupiter Price API v2 batch resolver.
- `core.solana.b58encode` / `b58decode` / `discriminator` / `is_valid_solana_address`
- `core.normalize.finite_number` / `mapping` / `token_entry`
- `core.display.truncate` — terminal-width string truncation.
- `core.wallet_balances` — `fetch_sol_balance_lamports`,
  `fetch_token_accounts`, and `ui_amount` used by both position and wallet
  scanners.

### `HeliusHistoryClient`

```python
fetch(address: str, max_pages: int = 0) -> tuple[list[dict], bool]
```

The client fetches finalized parsed events in ascending order. Zero pages means
unbounded retrieval; positive values bound pages. The boolean is true only when
pagination reaches completion. Malformed transaction entries are skipped, and
repeated pagination tokens raise instead of looping forever. Results are
currently accumulated in memory before lifecycle parsing.

### `PositionScanner`

```python
scan(wallet: str, dex: str = "all", history_pages: int = 0) -> PositionScan
```

The scanner validates a 32-byte base58 wallet and DEX selection. It concurrently
fetches current Meteora, Raydium, and Orca state, chunks NFT-position RPC
batches, optionally reconstructs Helius history, merges by
`(dex, position_address)`, and returns sorted positions. Current state wins over
historical state while retaining known open metadata.

Source-specific failures are recorded in `PositionScan.errors`. History failure
is non-fatal. If every selected current-state provider fails, `scan()` raises.
A provider returning no positions still counts as success.

`attach_positions(candidates, positions)` mutates each candidate's
`wallet_positions`, attaching non-closed records by exact `(dex, pool_address)`.

### Position Records

`LiquidityPosition` fields include DEX/pool/position identity, status, raw
liquidity, raw tick/bin bounds, raw fee/reward legs, lifecycle signatures and
times, source, and optional `pool_enrichment`.

```python
position.to_dict(include_enrichment: bool = False) -> dict[str, Any]
```

The result includes computed `has_claimable`. `PositionScan.to_dict()` emits
wallet, positions, errors, and `history_complete`.

### Helius Normalization

`HeliusWebhookParser.parse_payload()` accepts bytes, JSON text, one mapping, or
a sequence of mappings and returns immutable `NormalizedTransaction` records.
`parse_transaction()` accepts one mapping. Public nested records include:

- `NativeTransfer`, `TokenTransfer`
- `AccountBalanceDelta`, `TokenBalanceDelta`, `TokenFee`
- `NamedAccount`, `ParsedInstruction`
- `SwapAsset`, `SwapEvent`, `RentAdjustment`

Native fallback transfers are accepted only from the System Program, so SPL
token transfers cannot be mislabeled as lamports. UI token quantities are
scaled with `decimal.Decimal`; raw integer amount fields take precedence.
`fee_lamports` is the total network fee. `base_fee_lamports` and a derived
`priority_fee_lamports` can be estimates when Helius does not provide an
explicit priority fee.

### Position Scoring

`PositionScorer.score(input)` returns
`(PositionScoreBreakdown, float_total)`. `breakdown.total` equals the returned
total. Components are unweighted values in `[0, 1]`; total is `[0, 100]`.

`PositionScoreInput` deliberately distinguishes raw protocol coordinates from
normalized enrichment:

- `lower_bound` and `upper_bound` are raw tick/bin identifiers.
- `lower_price`, `upper_price`, and `current_price` are normalized prices.
- `position_value_quote` and `fee_value_delta_quote` must use one quote currency.
- `pool_fee_rate` is a decimal fraction.

Range and boundary components remain neutral without normalized price bounds.
Fee yield remains neutral without both quote-valued position size and a
timestamp-window fee delta. Raw fee legs are never summed into APR. PnL versus
HODL remains neutral because deposits, withdrawals, token quantities, and
historical prices are not available from the current scanner.

`PositionSnapshot` and `record_snapshot()` support bounded in-memory range
history. `from_liquidity_position(position, pool_extra)` accepts normalized
enrichment keys listed above.

### Trader Scoring

`TradePerformance` requires monetary fields in one quote currency.
`network_fee` must exclude `priority_fee` to avoid double counting.
`TraderPerformanceScorer.score(trades, now=None)` returns
`TraderScoreBreakdown` with risk-adjusted score, Sharpe/Sortino metrics, PnL,
costs, win rate, drawdown, and effective trade count.

Factor weights must be nonnegative and sum to one. `mev_weight` must be in
`[0, 1]` and discounts suspected positive performance; it does not reduce the
impact of suspected losses. Invalid, non-finite, negative-cost, or nonpositive-
capital records are skipped.

### Closure State

`ClosureState.refresh(positions)` compares closed positions with the previous
JSON snapshot and returns `(closed_at, dex:position)` tuples for new closures.
Closed records without a block time use scan time. Corrupt or non-object state
is treated as empty. Writes use a same-directory temporary file and
`os.replace()`; write failures are raised to the caller.

`scan_closure_state(positions, state_path)` returns the tracker and a list of
newly closed keys. The default file is local to the working directory and is
not wallet- or network-scoped; callers needing isolation should provide a
distinct path.

## CLI Error Handling

- Invalid numeric ranges and negative page/watch values are argument errors.
- A missing or malformed explicit token file exits with status 2.
- A failed one-shot screening cycle exits with status 1.
- Watch mode reports cycle failures and continues.
- Partial pool-provider and position/history failures are warnings on stderr.
- JSON serialization rejects NaN and infinity.
- `--json --watch` is rejected.

## Environment Variables

| Variable | Purpose |
|---|---|
| `RAYDIUM_API_BASE` | Override Raydium API base. |
| `ORCA_API_BASE` | Override Orca API base. |
| `METEORA_API_BASE` | Override Meteora API base. |
| `SOLANA_RPC_URL` | Current-position Solana RPC endpoint. |
| `HELIUS_API_KEY` | Enable Helius RPC fallback and lifecycle history. |
| `WALLET_PUBLIC_KEY` | Default wallet for `wallet_scanner/main.py`. |
| `JUPITER_PRICE_V2_URL` | Override Jupiter Price API v2 endpoint. |

The project does not load `.env` itself. Set variables before the Python process
starts because provider constants are evaluated at import time.

## Verification Workflow

```bash
python3 -m compileall -q .
cd pool_screener
python3 -m unittest test_screener
python3 pool_screener.py --help
python3 pool_screener.py --dex meteora --pages 1 --page-size 25

cd ../position_screener
python3 -m unittest test_positions
python3 position_screener.py --help

cd ../wallet_scanner
python3 -m unittest test_wallet_scanner
python3 -m wallet_scanner.main --help

cd ..
python3 -m unittest core.test_core
```

Before any future transaction path signs, it must re-read relevant on-chain
state through RPC and validate account ownership, freshness, slippage, and
program constraints independently of this screener.
