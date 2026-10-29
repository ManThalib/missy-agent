# Changelog

All notable changes to the Missy-agent Multi-DEX Screener Suite are documented here.

## [1.1.0] - 2026-10-29 - Pool features v1 (safety gate + fee-capture contract)

Baseline: commit `4e68494`. All suites green: pool_screener 121, position_screener 91,
wallet_screener 16, core 45 (41 new tests in `pool_screener/test_pool_features.py`).
Design: `docs/pool_features_v1_design.md`. Schema: `docs/pool_features_v1.md`.
Handoff one-pager: `docs/missy_sheldon_contract_v1.md`.

Splits Missy's single opaque `score` into a **safety gate** (Missy's job) and **fee-capture / net-yield
inputs** (Sheldon's ranking job), so Sheldon can stop gating on an opportunity score. Every existing
field is untouched: `score`, `eligible`, `rejected_reason`, `score_breakdown`, `pair_class` and all
provider fields keep their names, types and meaning.

### Added

- `safety_score` (liquidity + peg + data quality + pool age, **no fee terms**) and
  `opportunity_score` (gross fee yield only, **no depth**, strictly monotone in APR).
- `safety` block: `eligible`, `reject_codes` (fixed taxonomy + precedence), `metrics`, and
  `thresholds_used` (the exact floors applied, plus policy version and file path).
- `data_quality` block: `complete`, `missing`, `range_ready`, `prices_known`, and
  `missing_optional` for P2 gaps (never affects `complete`).
- `fee_capture` block: `realized_fee_apr`, `volume_window`, `tvl`, `fee_tvl_ratio`, `fee_tier`,
  `bin_step`, `tick_spacing`.
- `expected_in_range_frac` at the **deployable** reference width (mirrors Sheldon's
  `adaptive_half_width`: `0.5×vol_daily` held to a 1% price floor, 34-bin Meteora cap), with
  self-describing provenance (`reference_half_width_pct`, `_units`, `width_unit`, `width_source`,
  `vol_daily_pct`, `sigma_daily_pct`, `model`, and a 1/3/7-day `curve`).
- `il_estimate_pct` (divergence loss at the band edge, horizon-independent).
- `slippage_estimate_bps` at `$100`/`$1,000` plus the depth curve (`slippage_model.depth_proxy_usd`).
- `pool_age_days`, `lp_lock` (`locked_pct`, `unlock_at`, `kind`, `source`), `tvl_trend_24h_pct`,
  `owner_concentration_pct` (always `null` in v1 — no provider exposes it).
- `pair_confidence` (0–1, deterministic rule table).
- `pool_features_version`, `as_of` (Asia/Shanghai ISO), `scan_age_seconds`.
- `blacklisted` reject code for Meteora `is_blacklisted` (config-gated, fail-closed).
- `MultiDexScreener(clock=...)` injection so a scan is byte-reproducible in tests.
- `pool_screener/tools/diff_eligibility.py`: three-way eligibility diff (legacy / v1-old-floors /
  v1-active-policy) for shadow mode.
- Fixtures: `pool_screener/tests/fixtures/provider_pools_2026-10-29.json` (raw) and
  `pool_scan_2026-10-29.json` (real scan, frozen clock).

### Changed

- **Admission floors are lower and fully config-driven** (`missy_policy.json` version 2):
  `min_tvl_usd 10k`, `min_volume_usd 1k`, `min_fee_tvl_ratio_pct 0.02`, `max_volatility_pct 75`,
  `max_turnover_ratio 100`; new `min_pool_age_days 0`, `max_stable_depeg_pct 0.5`,
  `max_scan_age_seconds 900`. A floor of `0` disables that gate. Lowering a floor needs no code change
  (covered by `test_lowering_min_tvl_needs_config_only`).
- Eligibility evaluation now emits structured codes and a legacy text reason generated from the same
  decision, so the two cannot disagree. The five existing gate strings are unchanged.
- `pool_features` weights/anchors/model tunables added to the policy, with validation that fails closed
  (`PolicyError`) on malformed values.
- `screened_at` now comes from the injectable clock (same epoch-seconds semantics); production is
  unchanged because the default clock is `time.time`.
- Terminal table gains `SAFE`/`OPP` columns; the policy-failure summary counts reject codes instead of
  reason text.

### Fixed

- **Meteora `tvl_change_pct` was read as a percent delta.** It is a ratio to the prior window
  (100 = flat: the observed distribution clusters on 100). It is now converted with `- 100`, and `0`
  (no prior baseline) becomes `null` instead of a fabricated `+0%`. The first fixture capture emitted
  `+94.6%` for a stable whale pool because of this.
- **Orca `lockedPercentage` unit ambiguity** resolved explicitly (fraction → ×100) and tagged with
  `source`/`kind`; out-of-range provider values become `null` rather than being clamped.
- **`fee_known` no longer conflates "zero fees" with "missing fee data".** Dead pools with a known fee
  tier are rejected by `low_fee_tvl`, not by `missing_data`.

### Migration: `pool_features_version` 0 → 1

- v0 = any record without `pool_features_version`. Consumers must keep working on v0 scans: gate on
  `score`/`eligible` as before and ignore the v1 block.
- v1 is purely additive. No field was removed or renamed; no consumer change is required to keep
  running. The version is per record, so mixed files stay readable.
- New consumers should gate on `safety.eligible` (+ `data_quality.range_ready`) and rank on the
  fee-capture block; `score` remains available for audit and backtests.

### Eligibility diff on the fixture (67 whitelisted pools)

| Comparison | Eligible before → after | Flips |
|---|---|---|
| legacy gates → v1 evaluator, historical floors held | 19 → 19 | **0** |
| v1 historical floors → v1 active floors | 19 → 26 | 7 (all newly eligible, none lost) |

The contract change alone moves nobody; the 7 additions are the intended policy widening.
Reproduce with `cd pool_screener && python3 tools/diff_eligibility.py --raw
tests/fixtures/provider_pools_2026-10-29.json --prices-from tests/fixtures/pool_scan_2026-10-29.json`.

### Verification against Sheldon (read-only, no changes made there)

- Replayed the fixture through **Sheldon's live consumer** (`lp_scplic.score_pool_missy`): 67/67 pools
  scored with no exceptions (18 `OPEN_CANDIDATE`, 10 `WATCH`, 39 `IGNORE`). `run_cycle.gates.
  _filter_open_candidates()` and `_range_center()` also run unchanged; the new block is ignored as
  intended.
- Observation for Sheldon (pre-existing, not caused by this change): `scoring/pool/score.py::score_pool_missy`
  imports `load_scoring_policy` from `scoring.config`, which does not define it (`ImportError`). The
  live path (`lp_scplic.py`) is unaffected. Flagged in `docs/missy_sheldon_contract_v1.md`.

### Shadow mode (next)

Ship v1 as-is and leave Sheldon unchanged for 24 hourly cycles; watch scan size, runtime, JSON
validity, and `safety.eligible` vs Sheldon's current gate. Only then remove `min_open_score`, the
dual-model agreement gate, the adaptive-threshold re-tag and the legacy TVL/volume backstop.

## [Unreleased] - 2026-10-29 - Audit hardening

Baseline: commit `40b357b`. Test suites: 176 passing before, **227 passing** after
(51 new regression tests in `core/test_hardening.py`, `pool_screener/test_hardening.py`,
`position_screener/test_hardening.py`, and `wallet_screener/test_hardening.py`).
Live checks: `pool_screener.py --dex meteora` and `--dex all` ran against the real
provider APIs; a live Jupiter price call returned SOL and USDC prices.

### Fixed

- **Orca pending fees always fell back to checkpoints.** `_fetch_position_data`
  called `RpcClient()` with no URL (a `TypeError`, swallowed). Position accounts
  are now read in one batch through the configured RPC.
- **Position-scan wallet valuation was always zero.** Raw RPC token entries were
  indexed as flat records, so `wallet_balances` was empty, and `sol_price_usd` /
  `wallet_total_usd` were never set. Balances now parse via
  `core.wallet_balances.parse_token_account` and are priced through Jupiter.
- **"Closed since last scan" never fired.** The closure state was refreshed twice
  (scanner, then CLI). The scanner is now the single owner and returns
  `newly_closed_keys` on the scan result.
- **Fee-capture score used window fees as daily fees.** For `--timeframe 7d/30d`
  `new_fc_score` was overstated by about 7x or 30x. It now converts to daily fees.
- **Watch mode served stale data.** HTTP responses were cached for a hardcoded
  300 s regardless of the poll interval. The TTL now follows
  `MISSY_CACHE_TTL_SECONDS` and is capped at the `--watch` interval.
- **Ineligible pools were printed as "QUALIFIED".** The table lists only eligible
  pools; policy failures are summarized separately by gate.
- **Look-alike tokens could pass the whitelist.** Matching was by symbol alone. A
  canonical mint is now authoritative when the token supplies an address.
  Symbol-only matching remains for symbols that have no canonical mint.
- **Invalid canonical mints.** The WBTC constant was 43 characters and the WETH
  constant was 45 characters, so neither could match by address. Corrected to
  `3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh` (Wormhole WBTC) and
  `7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs` (Wormhole WETH). Both validate as
  base58 addresses.
- **Negative tick truncation.** Tick derivation used `int()` (rounds toward zero),
  giving an off-by-one tick for negative-tick pools. It now uses `math.floor`.
- **Meteora pool identity.** The normalizer falls back to `address` when
  `pool_address` is absent, so RPC lookups and position matching keep the pool id.
- **Meteora "prices" without a bin step.** Bin IDs were reported as prices. They
  are now reported as unknown (`0.0`).
- **Dead RPC cost.** A failed tick batch backed off about 95 s, then retried every
  pool individually (about 1.5 s each). Batch backoff is now capped at a 30 s
  planned-sleep budget, and a fully failed batch skips per-pool fallbacks.
- **Needless Meteora RPC calls.** Pools whose discovery payload already carried the
  active bin were still fetched over RPC. They are now skipped.
- **Raydium target-mint mode ignored `--pages`.** Only the first page per mint was
  fetched. It now pages up to `--pages`, pausing before each request.
- **Wallet CLI crashed on an unwritable output directory.** A failed screen-file
  write is now a warning.
- **Wallet scan output lacked `threshold_usd`.** It is now emitted (AGENTS.md
  compatibility), and the "no assets" message no longer claims a threshold.
- **Retries hit non-transient errors.** Wallet and RPC retries now apply only to
  transient failures (timeouts, 429, 5xx), honoring a capped `Retry-After`.
- **Enrichment hid pool-scan problems.** An unreadable or missing pool scan is now
  recorded in `errors["pool_scan"]`.
- **Secrets in error text.** HTTP error messages redact `api-key` and similar
  query parameters (`core.http_client.redact_url`).
- **Token amounts lost precision** above 2**53 raw units. They now use `Decimal`.
- **Meteora cron wrapper** had a hardcoded `/data/missy-agent` path. It now resolves
  its own directory, as the other wrappers do.

### Changed (behavior and output)

| Area | Previous | Current |
|---|---|---|
| Fee-capture `new_fc_score` (7d/30d) | Window fees treated as daily | Daily fee (`fee / window_days`) |
| `price_source` label | `jupiter_v2` | `jupiter_v3` (the API in use) |
| Ineligible pools in the table | Listed as "QUALIFIED" | Listed under a policy-rejection summary |
| Watch-mode cache TTL | Fixed 300 s | `min(MISSY_CACHE_TTL_SECONDS, --watch)` |
| Token matching | Symbol or mint | Mint authoritative when present; symbol-only only without a canonical mint |
| Raydium target mints | 1 request per mint | Up to `--pages` requests per mint |
| NFT position discovery | One `getProgramAccounts` scan per NFT | Derived PDA `["position", mint]`, batched `getAccountInfo` |
| Position closure detection | Broken (refreshed twice) | One owner; `newly_closed_keys` on the scan |
| Position wallet valuation | Empty balances, zero totals | Parsed balances, priced totals |
| Wallet balances commitment (position scan) | `finalized` | `confirmed` (same view as NFT discovery) |
| Wallet SOL token label | `wSOL` | `SOL` (shared mint map) |
| Enrichment position value key | `position_value_usd` only | Also `position_value_quote` (what analytics reads) |
| Meteora bin-step filter | Sent server-side, documented as not enforced | Documented as server-side for Meteora |
| Threshold flags (`--min-fee-tvl`, `--min-apr`, ...) | Silently ignored | Warned on stderr; policy gates apply |
| Empty-wallet position scan | 2 retries (6 s) and 2 extra provider scans | 1 retry (3 s) and 1 extra scan |
| Cache file names | Unversioned keys | Length-prefixed keys (old entries become misses) |
| HTTP connections | New TCP/TLS per request | Persistent per host |
| `pool_screener.py --watch -5` | Accepted silently | Rejected |

Note: `position_value_quote` is currently the USD position value. Fee APR still
needs a window fee delta, so `fee_value_delta_quote` remains unset.

### Added

- `core/retry.py`: transient-only retries with jittered backoff and capped
  `Retry-After`.
- `core/rpc.py`: `RpcRequestError` (carries `status_code` and `transient`) and
  `RpcClient.batch_partial()` (per-item results, so one failing account does not
  fail the batch).
- `core/http_client.py`: persistent per-host connections, redirect following,
  `HttpStatusError`, `redact_url()`, per-client `cache_ttl_seconds`.
- `core/cache.py`: `cache_ttl_from_env()`; atomic writes; directory created `0700`.
- `core/constants.py`: `WBTC_MINT`, `WETH_MINT`, `TAO_MINT`, `KNOWN_MINT_SYMBOLS`.
- `core/wallet_balances.py`: `parse_token_account()`; `fetch_token_accounts(commitment=...)`.
- `pool_screener/screener_py/normalizers.py` and `tick_fetch.py`: split from
  `multi_dex_screener.py`.
- `MISSY_POOL_SCAN_DIR`: overrides the pool-scan directory used by enrichment.
- `PositionScan.newly_closed_keys` (serialized in JSON output).

### Removed (dead or duplicate code)

- `_inject_token_prices`, duplicate `_range_volatility`, `_volatility`, unused
  `defaultdict` import, `_PRICE_HISTORY_SAMPLE_HOURS`, `DEFAULT_RATE_LIMIT_SECONDS`.
- `_decode_orca_pool_rewards`, `_fetch_position_data`, `_sha256`, a dead store in
  `_is_on_curve`, the duplicated per-DEX discriminator helpers (now
  `core.solana.discriminator`), and the duplicated retry block in `scan()`.
- The urllib opener in the HTTP client (`_opener`, `_IPv4HTTPHandler`,
  `_IPv4HTTPSHandler`). The IPv4-only connection classes remain.
- `BalanceCalculator.enrich` no-op branch and the wallet's duplicate
  `KNOWN_MINT_SYMBOLS`.
- A live network call that ran at import time in `test_raydium.py` (now a
  `__main__` script).

### Refactored

- `multi_dex_screener.py` (1093 lines) split into `normalizers.py` (normalization
  only), `tick_fetch.py` (RPC tick/bin lookup and backoff), and
  `multi_dex_screener.py` (policy, gating, scoring, assembly). Public names are
  unchanged: `normalize_pool`, `MultiDexScreener`, `RaydiumScreener`, and
  `MeteoraScreener`.
- Each pool is normalized once per phase (previously about five times).
- `MultiDexClient` uses one concurrent path for single and multi-provider scans.
- Position scans read Orca and Meteora accounts through one shared batch helper.
- Pending-fee reads now use `batch_partial` instead of `rpc._post`.
- Meteora decoder offsets computed once.
- Typing: implicit `Optional` defaults fixed, and the `import math` placement in
  `pool_scorer.py` corrected.

### Deferred (with reasons)

- **`screener_py/__init__.py` `sys.path` mutation** is kept. Removing it needs path
  setup in about ten test and entry files and would change the documented run commands.
- **Fee APR** (`_fee_apr_pct`, analytics `fee_value_delta_quote`) stays unavailable.
  A window fee delta needs snapshot history; `fees_owed` is cumulative since the
  last claim and cannot be turned into an honest window delta.
- **Threshold enforcement** (`--min-fee-tvl`, `--min-apr`, ...) is still Sheldon's
  decision. They now warn instead of silently doing nothing.
- **Dev linters** (`ruff`, `flake8`) were not added, because AGENTS.md forbids new
  dependencies without approval.
- **Long lines and style debt** in the position modules are not mass-reformatted.
- **Live RPC validation** of the derived-PDA NFT lookup, the Orca batch path, and
  the closure flow has not been run. These are covered by mocked tests only.

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
