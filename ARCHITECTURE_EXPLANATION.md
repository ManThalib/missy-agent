# Multi-DEX Pool Screener — Comprehensive Architecture Explanation

## 1. High-Level Blueprint & Core Mechanics

### Primary Objective
The **Multi-DEX Pool Screener** (Missy) discovers liquidity pools across three Solana DEX protocols—**Meteora DLMM**, **Raydium** (Standard/CLMM), and **Orca Whirlpools**—and evaluates each pool against economic policy gates and a scoring model that ranks pools by yield quality, depth, efficiency, and risk.

### Architectural Pattern
- **Pipeline architecture**: Data flows through sequential phases: discovery → normalization → whitelist filtering → price enrichment → tick/bin enrichment → eligibility gating → scoring → output.
- **Provider-independent normalization**: `normalize_pool()` converts each DEX's native payload into a common intermediate shape, preserving provider-specific fields while enabling unified filtering and scoring.
- **Policy-driven**: Eligibility gates, scoring weights/anchors, and universe definitions come from `missy_policy.json` (with fallbacks to in-code defaults).
- **Fail-closed / audit-preserving**: Pools that fail eligibility gates are still emitted with `eligible=false` and `rejected_reason`; enrichment failures record errors but do not abort the scan.

### Data Flow (Start to Finish)
```
CLI args → Argparse → FilterConfig → Whitelist
     ↓
MultiDexClient.fetch_pools() → concurrent provider fetches
     ↓
screen_all(): Phase 1 (normalize + whitelist filter)
     ↓
Phase 2: Jupiter price fetch for target token mints
     ↓
Phase 3: Re-normalize with price map + RPC tick/bin enrichment
     ↓
Phase 4: eligibility_gates() + PoolScorer.score() → Candidate objects
     ↓
sort by score → print_results() or json.dump()
```

---

## 2. Module & File Deep-Dive

### `pool_screener/pool_screener.py` — CLI Entry Point
- **`parse_args()`**: argparse-based CLI with flags for `--dex`, `--min-tvl`, `--max-fee-pct`, `--min-apr`, `--timeframe`, `--pages`, `--page-size`, `--sort`, `--watch`, `--json`.
- **`load_whitelist()`**: Loads `tokens.json`; allows `--tokens` and `--paired-tokens` overrides.
- **`main()`**: Orchestrates the scan cycle: creates `FilterConfig`, optional `RpcClient` (if `SOLANA_RPC_URL` set), and `MultiDexScreener`. Runs `screener.fetch_and_screen()`, prints warnings for provider errors, then either JSON-outputs candidates or calls `print_results()`.

### `pool_screener/screener_py/normalizers.py`, `tick_fetch.py`, `multi_dex_screener.py`

Screening is split across three modules:

- `normalizers.py`: `normalize_pool()` converts each provider payload into the common
  shape. It is the only place provider field names are read. Meteora payloads fall
  back to `address` when `pool_address` is absent.
- `tick_fetch.py`: batched `getAccountInfo` lookups for live Orca/Raydium ticks and
  Meteora active bins. Each chunk uses `RpcClient.batch_partial()`, so one failed
  account does not fail its chunk. Total planned backoff is capped at 30 s, and a
  batch in which every lookup fails sets the screener's `_rpc_unavailable` flag.
  That flag skips per-pool RPC fallbacks for the cycle.
- `multi_dex_screener.py`: gating, scoring, and candidate assembly.

### `pool_screener/screener_py/multi_dex_screener.py` — Screening Engine
- **`MultiDexScreener`** class: Core logic container.
  - `__init__()`: Stores `Whitelist`, `FilterConfig`, creates `MultiDexClient` (with concurrent Raydium/Orca/Meteora clients), loads policy via `load_policy()`, creates `PoolScorer(ScoringConfig.from_policy())`.
  - `_fetch_pool_prices()`: Collects token mints from pools, fetches USD prices via `JupiterPriceClient` (cached via `DiskCache`).
  - `_inject_token_prices()`: Mutates raw pool payloads so token objects carry `price_usd`—used before Jupiter prices are available.
  - `screen_pool()` / `_screen_pool_internal()`: Normalize a single pool, validate against whitelist, orient price, compute APR/fee/volatility, fetch tick/bin via discovery API or RPC fallback, build `PoolScoreInput`, score via `self.scorer.score()`, apply eligibility gates, return `(Candidate, "")` or `(None, rejection_reason)`.
  - `screen_all()`: Two-phase process:
    1. **Phase 1 (cheap)**: Normalize all pools, enforce whitelist *before* price/RPC fetches.
    2. **Phase 2**: Fetch Jupiter prices, re-normalize with price map, enrich with prefetched tick/bin maps, run `_screen_pool_internal()` per pool.
  - `_prefetch_current_ticks()`: Batched RPC fetch of current tick/bin for concentrated pools (Meteora DLMM active bin, Orca/Raydium tick). Uses `_batch_fetch_ticks()` with retries and 250ms chunk cooldown.
  - `fetch_and_screen()`: Delegates to `self.client.fetch_pools()` then `self.screen_all()`.

### Provider Clients (`clients/` directory)
Each client extends `HttpJsonClient` (from `core/http_client.py`, which wraps `urllib` with timeout and simple JSON parsing):

- **`MeteoraClient`**: Discovery API. Uses `after_key` cursor. Timeframe supports `5m`, `30m`, `24h`. Filters: `pool_type=dlmm`, TVL, bin_step, volume.
- **`OrcaClient`**: Whirlpool REST API. Uses `meta.next` cursor. Stats: `24h`/`7d`/`30d` via `stats` param. Sorts by liquidity/volume/fee/APR.
- **`RaydiumClient`**: List V2 + pools-by-mint. Uses `nextPageId` cursor. Supports `Standard` + `Concentrated` (CLMM) pool types. Sort fields depend on timeframe (`24h`/`7d`/`30d`).

### `pool_screener/screener_py/normalize_utils.py` — Normalization Helpers
- `finite_float()`: Returns float or raises `ValueError`; used for mandatory fields.
- `integer()`: Returns int or raises `ValueError`; used for tick_spacing/bin_step.
- `mapping()`: Safe dict guard (returns `{}` if not a Mapping).
- `token_entry()`: Normalizes a token object to `{symbol, address, decimals, price_usd}`.
- `top_level_token_metadata()`: Flattens token metadata to top-level candidate fields (`token_x_address`, `token_y_decimals`, etc.).

### `pool_screener/screener_py/normalize.py` — Core Normalization (core package)
Same helpers as `normalize_utils.py` but from `core.normalize`; re-exported for compatibility.

### `pool_screener/screener_py/policy_loader.py` — Policy Management
- **`DEFAULT_POLICY`**: In-code defaults for universe, eligibility gates, scoring anchors/weights.
- **`load_policy()`**: Loads `missy_policy.json` from repo root; merges with defaults via `_deep_merge()`; validates structure via `_validate()`; raises `PolicyError` on malformed file.
- **`eligibility_gates()`**: Returns `{min_tvl_usd, min_volume_usd, min_fee_tvl_ratio_pct, max_volatility_pct, max_turnover_ratio}` from policy.
- **`missy_policy.json`** (actual gates): `min_tvl_usd: 25000`, `min_volume_usd: 5000`, `min_fee_tvl_ratio_pct: 0.05`, `max_volatility_pct: 50`, `max_turnover_ratio: 50`.

### `pool_screener/screener_py/candidate.py` — Score Record
Dataclass `Candidate` with fields for:
- Pool identity (`pool_address`, `name`, `dex`, `pool_type`)
- Economics (`tvl`, `fee_tvl_ratio`, `daily_fee_usd`, `volume_window`, `bin_step`, `fee_pct`, `volatility`, `apr`)
- Prices (`token_x_price_usd`, `token_y_price_usd`, `pool_price`, `pool_price_raw`)
- Tick/bin state (`active_bin_id`, `current_tick_index`, `tick_unit`, `current_price_ratio`, `current_tick_equivalent`)
- Scoring (`score`, `eligible`, `rejected_reason`, `pair_class`, `score_model`, `score_version`, `score_breakdown`)
- Fee-capture model (`new_fc_score`, `new_score_breakdown`, `new_score_model`)
- Provenance (`tick_source`, `price_source`, `volatility_source`, `apr_source`, `pending_fees_source`)

### `pool_screener/screener_py/scorer/` — Scoring Engine
- **`PoolScorer.score()`** (main score, 0-100):
  - **yield_score**: log-scaled APR relative to anchor (50% APR anchor). Uses `adjusted_apr` (realized + projected cap).
  - **depth_score**: log-scaled effective TVL (TVL × liquidity_factor) relative to 1M USD anchor.
  - **efficiency_score**: log-scaled daily turnover (volume/tvl/day) relative to 2.0 daily anchor.
  - **risk_score**: `weight × (1 - risk_penalty)`, where risk_penalty blends volatility risk, concentration risk, yield divergence, and fee risk.
  - **score_fee_capture()`**: Separate 0-100 score with 5 sub-components: fee-yield (35%), absolute fee (25%), liquidity effectiveness (20%), turnover penalty (10%), LP-share adjustment (10%).

- **`ScoringConfig`**: Anchors and weights (all configurable via policy). Key values: yield_weight=25, depth_weight=25, efficiency_weight=25, risk_weight=25; yield_anchor_apr=50, depth_anchor_usd=1e6, turnover_anchor_daily=2.0, volatility_anchor_pct=20, high_fee_anchor_pct=1.0, projected_apr_weight=0.30.

### `pool_screener/screener_py/score_breakdown.py` — Score Auditing
- `ScoreBreakdown` dataclass: `total`, `yield_score`, `depth_score`, `efficiency_score`, `risk_score`, `realized_fee_apr`, `adjusted_apr`, `effective_tvl`, `daily_turnover`, `active_liquidity_factor`.

### `pool_screener/screener_py/whitelist.py` — Token Whitelist
- **`Whitelist`**: Manages target tokens and allowed paired tokens.
- **`validate_pair(token_x, token_y)`**: Determines which token is the "target" (whitelisted) and which is "paired" (allowed counter-asset). Returns `(ok, target, paired, reason)`. Reasons include "not in target token whitelist" and "paired token 'X' is not an allowed paired asset".
- **`TokenSet`**: Matches tokens by symbol or mint address; `normalize_symbol()` lowercases and strips.
- `tokens.json`: Defines `target_tokens` (SOL/WSOL, USDC, USDG, USDT, ETH/WETH, TAO, ZEC, BTC/cbBTC, XRP/cbXRP, XMR, BMT, QNT, ZBCN, GRASS, VIRTUAL) and `allowed_paired_tokens` (SOL/WSOL, BTC/cbBTC, ETH/WETH, USDC, USDG, USDT).

### `pool_screener/screener_py/clients/multi_dex_client.py` — Concurrent Provider Aggregation
- **`MultiDexClient`**: Holds `RaydiumClient`, `OrcaClient`, `MeteoraClient`. Runs them concurrently via `ThreadPoolExecutor`. Isolates per-provider errors in `self.errors` dict; if all selected providers fail, raises `RuntimeError`.

### `core/` Package — Cross-Cutting Concerns
- **`core/http_client.py`**: `HttpJsonClient` — simple `urllib`-based HTTP GET with timeout + `.json()` parsing.
- **`core/cache.py`**: `DiskCache` — on-disk TTL cache (default 300s). Keyed by namespace + SHA256 hash. Directory from `$MISSY_CACHE_DIR` or `/tmp/missy-cache`.
- **`core/normalize.py`**: `finite_float`, `integer`, `mapping`, `safe_get`, `token_entry`, `top_level_token_metadata` — shared normalization helpers.
- **`core/display.py`**: `truncate()` — string truncation for terminal output.
- **`core/solana.py`**: Solana primitives (b58decode, discriminator extraction).

### `position_screener/position_screener.py` — Position Analysis
- CLI entry point for wallet position scanning.
- Creates `PositionScanner`, runs `scan(wallet, dex, history_pages, closure_state_path, wallet_id)`.
- Handles closure state, prints results or JSON output.

### `position_screener/screener_position_py/position_scanner.py` — Position Discovery
- **`PositionScanner`**: Fetches live on-chain positions. Meteora positions come from `getProgramAccounts`. Raydium and Orca positions are read at the derived account `["position", position_mint]`, with mints taken from the wallet's NFT-like token accounts (decimals 0, amount 1). Falls back to Helius history if `HELIUS_API_KEY` set.
- **`_fetch_current()`**: Concurrent fetch across selected DEXes. Meteora fetches PositionV2/Position accounts; Raydium/Orca fetches NFT mint mints then decodes positions.
- **`_refresh_raydium_pending()`**: Recomputes live pending fees/rewards using `raydium_pending.py` SDK math (PoolState + tick arrays). Checkpoint values stale if never claimed.
- **`_refresh_orca_pending()`**: Same for Orca via `orca_pending.py`.
- **`_refresh_meteora_pending()`**: Same for Meteora via `meteora_pending.py` (per-bin loop).
- **`_mark_degraded_providers()`**: If a provider failed, marks positions with `status="unknown"` so consumers don't treat missing data as range evidence.
- **`enrich_positions()`**: Mutates positions with pool-derived prices/USD values. Loads latest pool scan from `/data/missy-data/pool_screens/`. A single bad pool record does NOT abort the scan—errors are recorded per-position.
- **`position_features.py`**: Builds versioned feature vector (`FEATURES_VERSION=1`) with gaps for missing data. Pure function, never raises.

### `wallet_screener/wallet_screener.py` — Wallet Balance Scanning
- **`WalletScanner`**: Fetches SOL balance + token accounts via RPC, prices via Jupiter v2, calculates USD values.
- **`scan(wallet)`**: Returns `{"wallet", "wallet_id", "sol_price_usd", "total_usd", "asset_count", "assets"}`. Emits **all assets** (no dust filtering by policy; dust threshold is Sheldon's domain).
- **`BalanceCalculator`**: Simple threshold filter (kept for CLI compatibility; scanner ignores it per AGENTS.md).

### Key Orchestration Files
- **`pool_screener/run_meteora.sh`**, **`run_positions.sh`**, **`run_wallet.sh`**: Shell wrappers that cd to the right directory and run the Python CLI.

---

## 3. Detailed Step-by-Step Flow ("Every Step & Every Rail")

### Pool Screener Execution Sequence

#### Step 1: Argument Parsing & Configuration
```python
# pool_screener.py → main()
args = parse_args()  # argparse: --dex all|meteora|raydium|orca, --min-tvl, --max-fee-pct, etc.
config = FilterConfig.from_args(args)  # FilterConfig with all thresholds
whitelist = load_whitelist(args)  # Whitelist from tokens.json
rpc_url = os.environ.get("SOLANA_RPC_URL", "").strip()
rpc = RpcClient(rpc_url) if rpc_url else None  # Optional RPC client for tick/bin fetch
screener = MultiDexScreener(whitelist=whitelist, config=config, rpc=rpc)
```

#### Step 2: Fetch Pools from All Providers
```python
# MultiDexScreener.fetch_and_screen(max_pages=config.pages)
pools = self.client.fetch_pools(self.config, max_pages=max_pages)
# MultiDexClient.fetch_pools():
#   - If dex=="all": concurrently runs MeteoraClient, RaydiumClient, OrcaClient.fetch_pools()
#   - If single dex: runs that client alone
#   - Errors stored in self.client.errors (e.g., {"meteora": "..."})
#   - If all fail: raises RuntimeError
#   - Pools tagged with "_dex" field for provider identification
```

#### Step 3: Phase 1 — Cheap Normalization + Whitelist Filter
```python
# MultiDexScreener.screen_all(pools)
kept = []
for pool in pools:
    n = normalize_pool(pool, window=config.window)  # provider→common shape
    token_x = n.get("token_x") or {}
    token_y = n.get("token_y") or {}
    ok, target, paired, reason = self.whitelist.validate_pair(token_x, token_y)
    if not ok:
        rejects[reason] += 1  # counted but screening continues
        continue
    kept.append((pool, n, target, paired))  # (raw_pool, normalized, target_token, paired_token)
# Purpose: discard pools outside the target universe before expensive price/RPC fetches
```

#### Step 4: Phase 2 — Fetch Jupiter Prices for Target Mints
```python
# Collect all token mints from kept pools
price_map = self._fetch_pool_prices([pool for pool, *_ in kept])
# JupiterPriceClient.fetch_prices(mints) → {mint_address: price_usd}
# Cached via DiskCache (300s default)
# If Jupiter fetch fails: returns {}, a warning is recorded, and pools are still screened with price_source="unavailable"
```

#### Step 5: Phase 3 — Re-Normalize with Prices + RPC Tick/Bin Enrichment
```python
for pool, n, target, paired in kept:
    # Re-normalize with the fetched price map so candidates carry real token prices
    n = normalize_pool(pool, window=self.config.window, price_map=price_map)
    
    # Screen the pool through the full internal pipeline
    candidate, reason = self._screen_pool_internal(
        pool, price_map=price_map, tick_map=tick_map,
        n=n, target=target, paired=paired,
    )
    # tick_map from _prefetch_current_ticks() — batched RPC active bin/tick lookups
```

**_screen_pool_internal() deep dive:**
```python
# 1. normalize_pool() with price_map → n
# 2. whitelist.validate_pair(token_x, token_y) → (ok, target, paired, reason)
#    - If neither token is in target whitelist → return (None, "not in target token whitelist")
# 3. Canonicalize name: f"{normalize_symbol(target_symbol)}-{normalize_symbol(paired_symbol)}"
# 4. Orient pool_price:
#    - If target_address == token_x_address: oriented_price = 1/pool_price_raw (price of token_y per token_x)
#    - Else: oriented_price = pool_price_raw (price of token_y per token_x, target is token_y)
# 5. Compute economic fields:
#    - tvl, volume_window, window_days, gross_fee_window, daily_fee_usd, fee_tvl_ratio, lp_fee_share
#    - tick_spacing, pool_type, fee_pct, volatility, apr
# 6. Fetch current tick/bin:
#    - Standard pools: active_bin_id=0, current_tick_index=0
#    - Meteora DLMM: discovery API active_bin_id → or RPC single fetch (0.15s cooldown) as fallback
#    - Orca/Raydium: discovery API current_tick_index → or RPC single fetch (0.15s cooldown) → or batched prefetch value
# 7. Derive cross-DEX price ratio:
#    - tick_unit = "tick" (O/R), "bin" (Meteora), "none" (Standard)
#    - current_price_ratio = 1.0001**current_tick_index  (for tick-based)
#    - Or: (1.0 + tick_spacing/10000)**active_bin_id     (for bin-based)
#    - current_tick_equivalent = round(log(price_ratio)/log(1.0001))
# 8. Token price derivation:
#    - If token_x_price_usd missing but token_y_price_usd + pool_price available:
#      token_x_price = token_y_price * pool_price
#    - Vice versa
# 9. Pair class classification:
#    - classify_pair_from_policy(whitelisted_symbol, paired_symbol, policy) → "stable_stable"|"stable_bluechip"|"bluechip_bluechip"|"off_universe"
# 10. Scoring input construction:
#     PoolScoreInput(tvl_usd, fee_usd, volume_usd, window_days, reported_apr_pct=0.0,
#                   volatility_pct, fee_tier_pct, lp_fee_share, pool_type, pair_class, window_label)
# 11. Main scoring: self.scorer.score(score_input) → ScoreBreakdown
# 12. Fee-capture scoring: self.scorer.score_fee_capture(score_input) → dict with fc_score, fee_yield_score, etc.
# 13. Eligibility gates:
#     - tvl < min_tvl_usd → reject
#     - volume < min_volume_usd → reject
#     - fee_tvl_ratio < min_fee_tvl_ratio_pct → reject
#     - volatility_available and volatility > max_volatility_pct → reject
#     - turnover > max_turnover_ratio → reject
#     - eligible = not rejection_reasons (but pool still emitted even if ineligible)
# 14. Candidate construction with all fields + score provenance (score_model, score_version)
# 15. new_fc_score = new_breakdown["fc_score"] stored post-construction
# 16. Return (candidate, "")
```

#### Step 6: Sort & Output
```python
candidates.sort(key=attrgetter("score"), reverse=True)  # sort high score first
return candidates, rejects

# Back in pool_screener.py main():
provider_errors = getattr(screener.client, "errors", {})
for provider, error in provider_errors.items():
    print(f"Warning: {provider} provider failed: {error}", file=sys.stderr)

if args.json:
    json.dump(candidates, sys.stdout, cls=CandidateJSONEncoder, indent=2)
    print()
else:
    print_results(candidates, rejects)  # formatted terminal table
```

### Position Screener Execution Sequence

#### Step 1: Initialization
```python
# position_screener.py → main()
position_scanner = PositionScanner()  # creates with RPC, optional Helius client
# scan(wallet, dex="all", history_pages=0, closure_state_path="position_closure_state.json", wallet_id="main")
```

#### Step 2: Fetch Current On-Chain Positions
```python
# PositionScanner._fetch_current(wallet, selected=["meteora", "raydium", "orca"])
# Uses ThreadPoolExecutor across selected DEXes:
#   - Meteora: getProgramAccounts(LBProgram, memcmp filters for wallet) → decode PositionV2 accounts
#   - Raydium: fetch NFT mint mints → derive PersonalPositionState PDA per mint → batched getAccountInfo → decode positions
#   - Orca: same as Raydium but for ORCA Program
# Returns (current_positions, errors, successful_providers)
```

#### Step 3: History Fallback (if HELIUS_API_KEY set)
```python
# If history client exists, fetch Helius history webhooks, parse transactions,
# extract position open/close instructions → LiquidityPosition records with source="helius_history"
# _parse_normalized_history() lifecycle dict keyed by (dex, position_address)
# Closed positions from history merged with current positions
```

#### Step 4: Merge & Degrade
```python
# _merge_positions(): dict keyed by (dex, position_address); current overrides historical
# _mark_degraded_providers(): if errors["meteora"] or errors["raydium"] or errors["orca"],
#   mark positions with status="unknown" and record degraded_provider in pool_enrichment
# _filter_closed_positions(): using ClosureState; on first scan, all closed positions recorded as "already reported"
```

#### Step 5: Refresh Pending Fees/Rewards (best-effort, per-DEX)
```python
# _refresh_raydium_pending(): fetch PoolState + tick arrays, SDK math → live fees/rewards
# _refresh_orca_pending(): same via Orca SDK
# _refresh_meteora_pending(): fetch position accounts, per-bin SDK math → fees/rewards/amounts
# Fail-closed: errors recorded in pool_enrichment, checkpointed values preserved
```

#### Step 6: Enrich Positions with Pool Data
```python
# enrich_positions() loads latest pool scan from /data/missy-data/pool_screens/pool_scan-*.json
# For each position, _enrich_one():
#   - Loads pool record by pool_address
#   - Derives current tick/bin from pool data (or estimates from pool_price)
#   - Computes in_range, lower_price, upper_price, current_price
#   - Computes fees_usd, rewards_usd from raw owed amounts + token prices
#   - Computes current_value_usd = real_x * px + real_y * py
#   - Handles Meteora value_known=false when per-bin amounts unavailable
#   - Builds position_features vector (gaps recorded, never zeros that hide problems)
#   - One bad pool record does NOT abort the whole scan
```

#### Step 7: Output
```python
# filtered/merged positions, errors dict, history_complete flag
# PositionScan returned to position_screener.py main()
# Print formatted table or JSON output
```

### Wallet Screener Execution Sequence

#### Step 1: Initialization
```python
# wallet_screener.py → main()
scanner = WalletScanner(
    rpc_url=args.rpc_url or None,
    price_url=args.price_url,
    threshold_usd=0.0 if args.include_dust else args.threshold,
    timeout=args.timeout,
    wallet_id=args.wallet_id,
)
# WalletScanner.__init__():
#   - self.wallet_id = (wallet_id or "main").strip() or "main"
#   - self.rpc = SolanaRpcClient(rpc_url or resolve_rpc_url(), timeout=timeout)
#     - resolve_rpc_url(): if HELIUS_API_KEY set → helix-url, else default mainnet-beta
#   - self.price_fetcher = TokenPriceFetcher(base_url=price_url, timeout=timeout)
#     - TokenPriceFetcher uses Jupiter Price v2 endpoint (JUPITER_PRICE_V2_URL)
#   - self.calculator = BalanceCalculator(threshold_usd=threshold_usd)
#     - Per AGENTS.md: Missy emits all assets; dust filtering is Sheldon's domain
```

#### Step 2: Fetch Balances
```python
# scan(wallet):
#   - lamports = self.rpc.get_balance_lamports(wallet)
#   - Token accounts via self.rpc.get_token_accounts(wallet)
#   - _parse_token_account(): delegates to core.wallet_balances.parse_token_account() (jsonParsed info → TokenBalance)
#   - SOL native: TokenBalance.native_sol(lamports)
#   - TokenBalance known mints have symbols from KNOWN_MINT_SYMBOLS dict
```

#### Step 3: Fetch Prices
```python
# _fetch_prices(balances):
#   - mints = [b.mint for b in balances]
#   - self.price_fetcher.fetch_prices(mints) → {mint: price_usd} via Jupiter v2
#   - On failure: logs warning, returns {}
```

#### Step 4: Enrich & Sort
```python
# enriched = self.calculator.enrich(balances, prices)  # adds total_value_usd per asset
# enriched.sort(key=lambda b: b.total_value_usd, reverse=True)
# Ensure SOL priced (fallback fetch if needed)
# total_usd = sum(b.total_value_usd for b in enriched)
# Return dict with wallet, wallet_id, sol_price_usd, total_usd, asset_count, assets=[b.to_dict() for b in enriched]
```

---

## 4. Mathematical & Algorithmic Logic

### 4.1 Scoring Mathematics (PoolScorer.score())

#### Yield Score (25% weight)
```
adjusted_apr = realized_apr * (1 - projected_apr_weight) + trusted_projected_apr * projected_apr_weight
where:
  projected_cap = max(realized_apr + premium_cap, realized_apr * multiple_cap)
  trusted_projected_apr = min(reported_apr, projected_cap)
  yield_divergence = max(0, (reported_apr - realized_apr) / reported_apr)  if reported_apr > 0

yield_score = weight * min(1.0, log1p(adjusted_apr) / log1p(yield_anchor_apr))
# yield_anchor_apr = 50.0 (from config/policy)
```

#### Depth Score (25% weight)
```
effective_tvl = tvl * active_liquidity_factor
daily_turnover = daily_volume / effective_tvl  (if effective_tvl > 0)
depth_score = weight * min(1.0, log1p(effective_tvl) / log1p(depth_anchor_usd))
# depth_anchor_usd = 1_000_000.0
```

#### Efficiency Score (25% weight)
```
efficiency_score = weight * min(1.0, log1p(daily_turnover) / log1p(turnover_anchor_daily))
# turnover_anchor_daily = 2.0
```

#### Risk Score (25% weight)
```
risk_penalty = bounded(0.50 * volatility_risk + 0.20 * concentration_risk + 0.20 * yield_divergence + 0.10 * fee_risk)

volatility_risk = 
  if volatility_pct is None: 0.35  (default risk when no data)
  else: bounded(volatility_pct / volatility_anchor_pct)  # volatility_anchor_pct = 20.0

concentration_risk = 1.0 - active_liquidity_factor
# active_liquidity_factor: "splash"→0.90, concentrated types→0.75, standard→1.00, default→0.85

fee_risk = bounded(fee_tier_pct / high_fee_anchor_pct)  # high_fee_anchor_pct = 1.0 (i.e., 100%)

risk_score = weight * (1.0 - risk_penalty)  # weight = 25.0
```

#### Total Score
```
total = bounded(yield_score + depth_score + efficiency_score + risk_score, upper=100.0)
```

### 4.2 Fee-Capture Score (score_fee_capture()) — 0 to 100

Five sub-components:

1. **Fee-Yield Score (35%)**: log10-scaled fee_tvl_ratio normalization
   ```
   min_fee = 0.05 (5%), high_fee = 5.0 (500%)
   if fee_tvl_ratio <= min_fee: fy_norm = 0.0
   elif fee_tvl_ratio >= high_fee: fy_norm = 1.0
   else: fy_norm = (log10(fee_tvl_ratio) - log10(min_fee)) / (log10(high_fee) - log10(min_fee))
   fee_yield_score = fy_norm * 35.0
   ```

2. **Absolute Fee Score (25%)**: daily_fee_usd relative to $200/day benchmark
   ```
   abs_fee_norm = min(1.0, daily_fee_usd / 200.0)
   absolute_fee_score = abs_fee_norm * 25.0
   ```

3. **Liquidity Effectiveness (20%)**: penalty for volatility away from 15%
   ```
   vol_penalty = abs(volatility - 15.0) / 70.0
   liquidity_effectiveness = max(0.0, 1.0 - vol_penalty) * 20.0
   ```

4. **Turnover Penalty (10%)**: reward for moderate turnover, cap at 20× TVL/day
   ```
   turnover_penalty_norm = max(0.0, 1.0 - daily_turnover / 20.0)
   turnover_penalty_score = turnover_penalty_norm * 10.0
   ```

5. **LP-Share Adjustment (10%)**: proportion of fees going to a single LP
   ```
   lp_share_adjustment = lp_share * 10.0
   ```

```
fc_score = fee_yield_score + absolute_fee_score + liquidity_effectiveness + turnover_penalty_score + lp_share_adjustment
fc_score = min(100.0, fc_score)
```

### 4.3 Tick/Bin Price Derivation

#### Orca/Raydium CLMM tick → price ratio
```
current_price_ratio = 1.0001 ** current_tick_index
```
*(The 1.0001 factor is the constant-product constant; each tick moves sqrt_price by ~0.01%)*

#### Meteora DLMM bin → price ratio
```
current_price_ratio = (1.0 + tick_spacing / 10000.0) ** active_bin_id
```
*(tick_spacing is in basis points; e.g., 100 = 1% per bin)*

#### Reverse derivation (price ratio → tick/bin)
```
current_tick_equivalent = round(log(current_price_ratio) / log(1.0001))
```

### 4.4 APR Annualization
```
_yield_over_tvl_apr(fee_usd, tvl_usd, window):
    days = {"day": 1.0, "week": 7.0, "month": 30.0}[window]
    return (fee_usd / tvl_usd) * 100.0 * (365.0 / days)
```

---

## 5. Key Takeaways & System Constraints

### 5.1 Implicit Assumptions
- **Public API data is cached/indexed**: Suitable for screening, not transaction settlement. Future trade paths must re-read on-chain state via RPC before signing.
- **Jupiter prices are authoritative for USD valuation**: Token prices come from Jupiter v2 price endpoint; missing prices cause orientation fallback but not hard failure.
- **Policy is the single source of truth**: `missy_policy.json` gates + scoring config. In-code defaults exist as fallback but the JSON file overrides them.
- **150ms inter-page delay**: Pagination across all three providers respects ~150ms between pages to avoid rate limiting.
- **WSOL normalizes to SOL**: Token whitelist includes WSOL aliases; WSOL → SOL conversion handled in token mapping.

### 5.2 Provider Pagination & Rate Limits
| Provider | Cursor | Timeframes | Page Max |
|----------|--------|------------|----------|
| Meteora | `after_key` | 5m, 30m, 24h | 100 |
| Raydium | `nextPageId` | day, week, month | 1000 |
| Orca | `meta.next` | 24h, 7d, 30d | 1000 |

### 5.3 Error Handling & Fallbacks
- **Single provider failure**: Other providers' data is unaffected; error printed as warning.
- **All providers fail**: `RuntimeError` with "All selected providers failed: ..."
- **Malformed pool payload**: Caught via `TypeError/ValueError/OverflowError`; pool counted in rejects; screening continues.
- **RPC timeout/failure**: Batched getAccountInfo with retries (6 attempts, exponential backoff). Single-fetch fallback after 150ms cooldown.
- **One bad pool in enrichment**: `enrichment.py` catches per-position exceptions; records error in `pool_enrichment`; continues scan.
- **Missing tick/bin data**: Uses `UNKNOWN_TICK = None` sentinel; 0 treated as valid value; consumers must handle None explicitly.

### 5.4 Scoring & Policy Constraints
- **Score range**: 0–100 (main model); 0–100 (fee-capture model).
- **Default policy gates**: min_tvl=$25K, min_volume=$5K, min_fee_tvl_ratio=0.05%, max_volatility=50%, max_turnover=50.
- **Eligible pools still emitted with `eligible=false`**: Audit trail preserved; downstream consumers can filter on `eligible=true`.
- **Fee-capture model is experimental**: Stored in `candidate.new_fc_score` and `candidate.new_score_breakdown`; score model tag `"new_scoring_model_for_pool"`.

### 5.5 Data Flow Constraints
- **No API keys required**: All three DEX discovery APIs are public-keyless. Helius RPC optional via `HELIUS_API_KEY`.
- **Cache TTL**: Default 300s (5 minutes) via `DiskCache`. Controlled by `MISSY_CACHE_TTL_SECONDS` env var or `$MISSY_CACHE_DIR`. In `--watch` mode the TTL is capped at the poll interval.
- **Wallet ID**: Default `"main"`; passed through via `$WALLET_ID` env var or `--wallet-id` CLI flag.
- **Position features versioned**: `FEATURES_VERSION=1`; gaps recorded so consumers can apply fail-closed policies.

### 5.6 Codebase Constraints (per AGENTS.md)
- **Python standard library only**: No external dependencies beyond what's already in the repo.
- **Provider payload conversion in `normalize_pool()`**: Keeps filtering provider-independent.
- **`threshold_usd` compat plumbing intact**: Wallet scans emit all assets; dust filtering is separate policy.
- **Position enrichment reads latest pool scan**: One bad pool record must not abort the scan.
- **Live pending fees/rewards from SDK ports**: Raydium CLMM PoolState + tick arrays, Meteora DLMM per-bin loop, Orca Whirlpool growth-inside quotes.
- **Position carries versioned `position_features` block**: Features only, gaps listed, no verdict.
- **Every scan carries `wallet_id`**: Default `main`; `run_positions.sh` / `run_wallet.sh` pass through `$WALLET_ID`.
- **Orca feeRate is millionths; Raydium feeRate is decimal fraction; Meteora already supplies `fee_pct`**: Conversion handled in `normalize_pool()`.
- **Wallet scans price tokens via Jupiter Price endpoint**: `JUPITER_PRICE_V2_URL` default `https://api.jup.ag/price/v3`.

---
*End of Architecture Explanation*