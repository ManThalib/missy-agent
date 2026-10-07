# Changelog

All notable changes to the Missy-agent Multi-DEX Screener Suite are documented here.

## [Unreleased] - 2026-10-07

### Changed

- **PnL-vs-HODL is now computed** (`analytics/scoring.py`): `_pnl_vs_hold`
  applies the constant-product IL formula (`2*sqrt(Pt/P0)/(1+Pt/P0) - 1`,
  entry price = range lower bound) instead of returning a permanent neutral
  `0.5`. Score is `1 - |IL|` (1.0 = no IL); still neutral when price data
  is missing or the lower bound is zero.
- **Orca positions get live pending amounts** (`position_scanner.py`):
  `_refresh_orca_pending` now runs every active Whirlpool position through
  `OrcaPendingFeesFetcher.compute_pending()` (live pool state + boundary
  tick arrays, SDK growth-inside math) and labels the source `computed`.
  Failures keep the checkpointed values and record `pending_fees_error`
  with a `raw_checkpoint` label instead of aborting the scan.

## [Unreleased] - 2026-10-06

### Added

- **Fee-capture pool scoring** (`pool_screener/screener_py/pool_scorer.py`):
  `PoolScorer.score_fee_capture()` emits a second 0-100 model alongside the
  default score. Weights: fee-yield 35 (log-scaled fee/TVL, 0.05%-5% band),
  absolute-fee 25 ($200/day benchmark), liquidity-effectiveness 20
  (volatility centered at 15%), turnover-penalty 10 (erodes past 20x
  TVL/day), LP-share 10. Results land on
  `Candidate.new_fc_score` / `new_score_breakdown` / `new_score_model`.
- **Concentrated Yield Capture position scorer**
  (`position_screener/screener_position_py/analytics/scoring.py`):
  `CYCPositionScorer` separates pool-level yield potential from
  position-level capture efficiency (`cyield`/`cdepth`/`cefficiency`/`crisk`
  components from existing enrichment data, no new RPC calls).
  `PositionScoreInput` gains optional pool context (`pool_tvl_usd`,
  `pool_type`, `volatility_pct`, `fee_tier_pct`, `fees_apr_pct`).
- **Versioned position feature vector**
  (`position_screener/screener_position_py/position_features.py`):
  every scanned position carries a `position_features` block
  (`FEATURES_VERSION=1`: identity, range geometry, value/fees/rewards,
  pool context, plus a `gaps` list for missing inputs). Features only, no
  score, no verdict; Sheldon scores downstream.
- **Meteora DLMM live pending math** (`meteora_pending.py`): stdlib port of
  the `@meteora-ag/dlmm` SDK per-bin fee/reward loop (fee-per-liquidity
  checkpoints, active-bin reward advancement), verified against the SDK on
  live mainnet accounts. Positions report live pending amounts plus real
  token amounts instead of raw per-bin checkpoint sums.
- **Orca Whirlpool live pending math** (`orca_pending.py`): stdlib port of
  the whirlpools-sdk `collectFeesQuote` / `collectRewardsQuote`
  (fee/reward growth-inside math), replacing stale checkpointed
  `feeOwedA/B` / `amountOwed` fields.
- **SDK test vectors** (`testdata/gen_vectors.cjs`, `ray_vectors.json`):
  durable Raydium SDK ground-truth vectors plus Meteora amount tests
  (`test_meteora_amounts.py`) and feature tests
  (`test_position_features.py`).
- **Disk cache** (`core/cache.py`): `DiskCache` with per-key TTL (default
  300 s, `$MISSY_CACHE_DIR` or `/tmp/missy-cache`), wired into provider
  JSON fetches and the pending-fee RPC fetcher (per-pool PoolState /
  tick-array caching: N positions in one pool cost 2 RPC calls, not 2N).

### Changed

- **Missy Phase 1 scoring**: Missy now emits a default 0-100 pool score and
  pre-scoring eligibility gates from `pool_screener/missy_policy.json`
  (`pool_eligibility`: min TVL $25k, min volume $5k, min fee/TVL 0.05%, max
  volatility 50%, max turnover 50x; `scoring`: equal 25/25/25/25
  yield/depth/efficiency/risk weights with anchors). Failing pools are still
  emitted with `eligible: false` and `rejected_reason` so the audit trail
  survives. `policy_loader.py` loads/validates the policy fail-closed with a
  built-in default fallback; `pair_class.py` tags cohorts (`stable_stable`,
  `stable_bluechip`, `bluechip_bluechip`, `off_universe`). `Candidate`
  gains `eligible`, `rejected_reason`, `pair_class`, `score_model`,
  `score_version`, `score_breakdown`, `daily_turnover`, and
  `active_liquidity_factor`.
- **Pool screener speedup**: whitelist pre-filter in `screen_all()` runs
  before Jupiter/RPC enrichment (~117 s to ~2.9 s per scan); Raydium
  discovery uses targeted `poolsByMint` (1 page/mint) instead of full pages.
- **Orca volatility windows**: excursion is now computed for both `24h`
  (last 2 of 14 price-history points plus current price) and `7d`; table
  header widened from `VOL (%)` to `VOLAT (%)`.
- **Token-price derivation**: a missing token leg USD price is derived from
  the pool price and the known leg price.
- **Meteora correctness fixes**: bogus position values no longer emitted,
  `current_bin_id` fixed, fee sentinel corrected; active-bin RPC enrichment
  is opt-in (only when `SOLANA_RPC_URL` is set).
- **Wallet SOL pricing**: SOL price retries via a dedicated fetch when the
  token-account price is missing; top-level `sol_price_usd` /
  `wallet_total_usd` exposed.
- Pending-fee sources are labeled (`raw_checkpoint` / `raw_per_bin_sum`);
  Orca pool reward mints are fetched and priced.

## [Unreleased] - 2026-10-03

### Changed

- **Missy emits facts, Sheldon owns policy**: `MultiDexScreener.screen_pool()`
  no longer rejects on whitelist/TVL/volume/fee-tier/spacing/volatility/APR.
  Every normalized pool is emitted with a neutral `score=0.0`
  (`effective_tvl` equals `tvl`; realized/adjusted APR and score buckets are
  `0.0`). Filter flags remain parsed for CLI compatibility. Pool tests updated
  to assert fact emission.
- **Wallet dust policy moved to Sheldon**: `USD_THRESHOLD` removed from
  `wallet_screener/config.py`; `BalanceCalculator` defaults to `0.0` and
  `WalletScanner.scan()` uses `enrich()` with no filtering. `--threshold` is
  compat-only (scanner ignores it); `--include-dust` is a no-op.
- **Multi-wallet mirror**: every `PositionScan`, `LiquidityPosition`, and
  wallet scan carries `wallet_id` (default `main`). New `--wallet-id` flags on
  the position and wallet CLIs; `run_positions.sh` / `run_wallet.sh` pass
  through `$WALLET_ID`. Mirror wallet screens write
  `wallet_screen-<id>-<TS>.json` (main keeps unsuffixed names).

### Added

- **Raydium CLMM real pending math** (`position_screener/screener_position_py/raydium_pending.py`):
  stdlib-only port of raydium-sdk-v2 `PositionUtils` (PoolState + boundary
  tick-array reads, tick-array PDA derivation, u128 growth-inside semantics).
  Active Raydium positions overwrite stale checkpointed owed fields; reward
  mints/decimals recorded and priced (`enrichment.py` prices pair mints from
  the pool record and third-party mints via Jupiter once per scan).
  Includes `test_raydium_pending.py` (SDK vectors, PDA/curve ground truth,
  layout offsets) plus degraded-provider tests.
- **Degraded-provider marking**: positions from a failed provider are flagged
  `status=unknown` instead of reading as out-of-range evidence.
- **Wallet runner outputs**: `--output-dir` / `--no-output-file` on the wallet
  CLI; `run_wallet.sh` refreshes the symbol-keyed `wallet_balances.json` cache.
- **Token metadata**: `TokenConfigParser` now collects `aliases` arrays as
  extra match literals; shipped `tokens.json` uses
  `{asset, mint, aliases}` objects (targets add TAO/ZEC/XRP/XMR/QNT/ZBCN/GRASS/
  VIRTUAL; paired set adds BTC/ETH/USD).

### Fixed

- Raydium CLMM `tickCurrent` offset `304 -> 269` (verified live on mainnet);
  Meteora DLMM `activeId` decoded as signed i32 at LbPair offset 76; Orca
  Position fee/reward offsets corrected.
- Runner scripts scan `--dex all` (`run_meteora.sh` writes
  `pool_scan-<TS>.json`; `run_positions.sh` writes `position_scan-<TS>.json`
  with failure-context `.failed` records).
- Shared defaults: `METEORA_API_BASE=https://pool-discovery-api.datapi.meteora.ag`,
  `ORCA_API_BASE=https://api.orca.so/v2/solana`,
  `JUPITER_PRICE_V2_URL=https://api.jup.ag/price/v3` (v3 shapes parsed);
  shared client lives in `core/http_client.py`.

## [Unreleased] - 2026-09-30

### Changed

- **Rename**: `wallet_scanner/` module and package renamed to `wallet_screener/`
  to match its role as a screener rather than a scanner. All imports, scripts,
  and tests updated.
- Pool screener multi-DEX candidate handling and test suite updated for latest
  provider normalizations.
- Position screener closure state tracking and run script updated.
- Documentation and agent notes refreshed.

## [Unreleased] - 2026-09-24

### Added

#### Shared Core

- **New `core/` package** consolidating logic that was duplicated across the
  screeners:
  - `core/http.py` — `HttpJsonClient` (moved from
    `pool_screener/screener_py/clients/base_client.py`).
  - `core/rpc.py` — `RpcClient` (moved from
    `position_screener/screener_position_py/rpc_client.py`).
  - `core/helius.py` — `HeliusHistoryClient` (moved from
    `position_screener/screener_position_py/helius_history_client.py`).
  - `core/solana.py` — base58 encode/decode, Anchor discriminator, and
    `is_valid_solana_address`.
  - `core/constants.py` — shared env-based API/RPC constants plus token
    program, mint, and unit constants.
  - `core/normalize.py` — `finite_float`, `finite_number`, `integer`,
    `mapping`, `token_entry`, and `safe_get`.
  - `core/display.py` — shared `truncate`.
  - `core/prices.py` — `JupiterPriceClient` for the keyless Jupiter Price API v2.
  - `core/wallet_balances.py` — `fetch_sol_balance_lamports`,
    `fetch_token_accounts`, and `ui_amount`.
- **Core tests** (`core/test_core.py`) covering base58 round-trips, address
  validation, normalization, display, and Jupiter price parsing.

#### Wallet Screener

- **New `wallet_screener/` module** built on `core/` with one class per file:
  - `config.py` — `USD_THRESHOLD` (default `$0.10`), endpoints, program IDs,
    and `resolve_rpc_url()` / `resolve_wallet()` helpers.
  - `models.py` — `TokenBalance` dataclass with `to_dict()` and a
    `native_sol()` factory.
  - `rpc_client.py` — `SolanaRpcClient` with linear-backoff retries for native
    SOL and SPL/Token-2022 token accounts.
  - `price_fetcher.py` — `TokenPriceFetcher` batching Jupiter Price API v2 calls.
  - `balance_calculator.py` — `BalanceCalculator` for USD valuation and
    threshold filtering.
  - `wallet_screener.py` — `WalletScanner` orchestrator returning assets sorted
    by descending USD value.
  - `main.py` — CLI with `--wallet`, `--threshold`, `--json`, `--watch`,
    `--include-dust`, and `--rpc-url`/`--price-url` overrides.
- **Wallet screener tests** (`wallet_screener/test_wallet_screener.py`).

### Changed

- `pool_screener` and `position_screener` now import shared logic from `core/`;
  their former modules (`base_client.py`, `rpc_client.py`,
  `helius_history_client.py`, `constants.py`, `normalize_utils.py`,
  `coercion.py`) are compatibility re-export shims.
- `position_scanner` now fetches SOL and SPL balances through
  `core.wallet_balances` instead of inline duplicated JSON traversal.
- Package `__init__.py` files add the repository root to `sys.path` so the
  shared `core` package resolves when entrypoints and tests run from their own
  directories.
- `WalletScanner.scan()` now validates that the wallet is a 32-byte base58
  address.

### Removed

- Dead `time` import in `position_scanner.py` and unused `Iterable` import in
  `helius_parser.py`.
- Duplicated `truncate` and `_finite_number` implementations in both screeners'
  `display.py` modules (now re-exported from `core`).

## [Unreleased] - 2026-09-22

### Added

#### Pool Screener

- **RPC client integration** (`pool_screener/pool_screener.py`): the CLI now
  reads `SOLANA_RPC_URL` (defaults to `https://api.mainnet-beta.solana.com`)
  and passes an `RpcClient` into `MultiDexScreener`.
- **Meteora DLMM discovery fields** (`screener_py/candidate.py`): `Candidate`
  now preserves and serializes `pool_price`, `token_x_address`,
  `token_x_decimals`, `token_x_price_usd`, `token_y_address`,
  `token_y_decimals`, `token_y_price_usd`, and `active_bin_id`.
- **Active bin ID lookup** (`screener_py/multi_dex_screener.py`):
  `_decode_active_bin_id()` reads the `activeId` u32 at offset 8 of the DLMM
  `LbPair` account, and `_fetch_active_bin_id()` retrieves it over RPC for
  concentrated pools. `MultiDexScreener` accepts an optional `rpc` argument.
- **Token configuration** (`pool_screener/tokens.json`): added `USDG` and `BMT`
  to target tokens, and added the `BMT` mint
  (`2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH`) to allowed paired tokens.

#### Position Screener

- **Derived position metrics** (`screener_position_py/liquidity_position.py`):
  added `current_bin_id`, `in_range`, `current_price`, `lower_price`,
  `upper_price`, `fees_usd`, `rewards_usd`, `days_open`, `current_value_usd`,
  and `token_x_amount` / `token_y_amount` (raw + UI amounts).
- **Wallet balance summary** (`screener_position_py/position_scan.py`):
  `PositionScan` now exposes `sol_balance_lamports`, `sol_price_usd`,
  `wallet_total_usd`, and `wallet_balances`.
- **Balance and token-account enrichment**
  (`screener_position_py/position_scanner.py`): the scanner fetches the SOL
  balance via `getBalance` and token accounts via `getTokenAccountsByOwner`,
  normalizing raw amounts to UI amounts and computing a wallet USD total.
- **RPC helper** (`screener_position_py/rpc_client.py`): added
  `RpcClient.get_account_info()` for base64 `getAccountInfo` reads.

### Notes

- `sol_price_usd` and per-token `price_usd` are currently placeholders (`0.0`),
  so `wallet_total_usd` only reflects priced assets until a price source is
  wired in.
- Failures in balance/token-account lookups are swallowed and leave the
  defaults in place; they do not discard positions returned by other sources.

## [0.1.0] - 2026-09-22

### Added

- Initial commit of the Multi-DEX Pool Screener Suite.
- Pool screener for Meteora DLMM, Raydium Standard/CLMM, and Orca Whirlpools.
- Position screener for active and historical wallet LP positions.
- Provider-independent 0-100 pool scoring and position scoring.
- Standard-library-only implementation with table and JSON output.
- Unit test suites: `pool_screener/test_screener.py` and
  `position_screener/test_positions.py`.
