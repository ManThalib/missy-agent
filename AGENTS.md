# Multi-DEX Pool Screener

## Communication Protocol

- Mr. Man does not message Missy directly, and Missy never alerts Mr. Man
  directly. All traffic to or from Mr. Man is bridged by **Miraa** (Executive
  Communicator).
- Clarifications, errors, and scheduled reminders go to Miraa in the backend;
  Miraa consolidates and briefs Mr. Man in a single, organized message when
  multiple agents have updates.

## Scope

- `pool_screener/pool_screener.py`, `position_screener/position_screener.py`, and `wallet_screener/main.py` are the CLI entrypoints. Shared logic lives in the top-level `core/` package; `pool_screener/screener_py/` and `position_screener/screener_position_py/` own provider-specific logic, and `wallet_screener/` keeps one class per file.
- Pool features v1 (`pool_screener/screener_py/pool_features.py`) is the safety-gate + fee-capture contract: `safety_score`/`opportunity_score`, `safety`, `data_quality`, `fee_capture`, `expected_in_range_frac`, `il_estimate_pct`, `slippage_estimate_bps`, P2 rug signals. It is pure (clock injected), additive, and documented in `docs/pool_features_v1*`. Never gate on `opportunity_score`; never remove or rename legacy fields.
- Admission floors and feature tunables live in `pool_screener/missy_policy.json` (`version: 2`). Lowering a floor must never require a code change; a floor of `0` disables that gate.
- The default `--dex all` queries Meteora DLMM, Raydium Standard/CLMM, and Orca Whirlpools. Keep provider payload conversion in `normalize_pool()` (`pool_screener/screener_py/normalizers.py`) so filtering remains provider-independent. Live tick/bin lookups live in `tick_fetch.py`, and gating/scoring stays in `multi_dex_screener.py`.
- Missy emits facts plus a default score; Sheldon owns strategy policy. `MultiDexScreener.screen_pool()` enforces the `missy_policy.json` eligibility gates but still emits failing pools with `eligible=false` and `rejected_reason`, and scores every candidate with the policy default model (plus the experimental fee-capture model on `new_fc_score`). `WalletScanner.scan()` emits all assets (no dust filtering). Keep the `threshold_usd` compat plumbing intact.
- This project uses only the Python standard library. Do not introduce a dependency or requirements file for ordinary HTTP/CLI work.

## Commands

- Run from the `pool_screener/` directory so the default `tokens.json` is found: `python3 pool_screener.py`.
- Focus one provider while debugging: `python3 pool_screener.py --dex meteora|raydium|orca --pages 1 --page-size 25`.
- Run all tests (each from its own directory, core from the repo root):
  ```bash
  cd pool_screener && python3 -m unittest test_screener test_pool_features
  cd ../position_screener && python3 -m unittest test_positions
  cd ../wallet_screener && python3 -m unittest test_wallet_screener
  cd .. && python3 -m unittest core.test_core
  ```
- Run one test: `python3 -m unittest test_screener.TestScreener.test_orca_pool_normalization`.
- Eligibility diff after a policy change (from `pool_screener/`): `python3 tools/diff_eligibility.py --raw tests/fixtures/provider_pools_2026-10-29.json --prices-from tests/fixtures/pool_scan_2026-10-29.json`.
- Verify argument wiring after CLI edits: `python3 pool_screener.py --help`, `python3 position_screener.py --help`, or `python3 main.py --help` (wallet; `python3 -m wallet_screener.main --help` from the repo root).

## Provider Details

- Pool APIs require no API key. Optional base URL overrides are `METEORA_API_BASE` (default `https://pool-discovery-api.datapi.meteora.ag`), `RAYDIUM_API_BASE` (default `https://api-v3.raydium.io`), and `ORCA_API_BASE` (default `https://api.orca.so/v2/solana`); wallet scans use `SOLANA_RPC_URL` or Helius RPC when `HELIUS_API_KEY` is set.
- Provider pagination differs: Meteora uses `after_key`, Raydium uses `nextPageId`, and Orca uses `meta.next`. Preserve the 150 ms delay between pages.
- Raydium API time windows are `day/week/month`, Orca stats are `24h/7d/30d`, and Meteora only supports `5m/30m/24h`. The default `24h` works across all three.
- A failure from one provider must not discard data from the others; `MultiDexClient.errors` is printed as warnings. Raise only when every selected provider fails.
- Position-provider and history failures are warnings and must not discard screened pools or positions returned by other protocols. A failed position provider's records are marked `status=unknown`.
- Position enrichment reads the latest `/data/missy-data/pool_screens/pool_scan-*.json`; one bad pool record must not abort the scan. Live pending fees/rewards come from the stdlib SDK ports: `raydium_pending.py` (CLMM PoolState + tick arrays), `meteora_pending.py` (DLMM per-bin loop), `orca_pending.py` (Whirlpool growth-inside quotes). Every position also carries a versioned `position_features` block (`position_features.py`, features only, gaps listed, no verdict).
- Provider JSON fetches and pending-fee RPC reads go through `core/cache.py` `DiskCache` (default TTL 300 s from `MISSY_CACHE_TTL_SECONDS`, `$MISSY_CACHE_DIR` or `/tmp/missy-cache`, directory mode `0700`, atomic writes). In `pool_screener.py --watch N` the HTTP cache TTL is capped at N seconds so polls never serve stale data.
- HTTP providers use persistent per-host connections and retry only transient failures (timeouts, 429, 5xx) through `core/retry.py`, honoring a capped `Retry-After`. Other 4xx responses fail immediately. Error text redacts `api-key` query values.
- RPC: `RpcClient.batch_partial()` returns per-item `(result, error)`. Use it when one failing account must not fail the batch (tick/bin lookups, position account reads). `RpcRequestError.transient` marks retryable transport failures.
- Position discovery: Raydium and Orca NFT positions are read at the derived account `["position", position_mint]` (batched `getAccountInfo`). Meteora positions still come from `getProgramAccounts`. The scanner owns closure state: `PositionScan.newly_closed_keys` is the only newly-closed source, and the CLI does not refresh the state file again.
- Every scan and every position record carries `wallet_id` (default `main`); `run_positions.sh` / `run_wallet.sh` pass through `$WALLET_ID`.
- Orca `feeRate` is millionths; Raydium `feeRate` is a decimal fraction. Meteora already supplies `fee_pct`.
- Wallet scans price tokens via the Jupiter Price endpoint (`JUPITER_PRICE_V2_URL`, default `https://api.jup.ag/price/v3`) by default. The endpoint is keyless; override with the environment variable if needed.

## Filtering

- `tokens.json` carries token metadata (`asset`/`symbol`/`mint`/`address`/`value` plus `aliases`). Symbols and mint addresses are accepted; `WSOL` normalizes to `SOL`.
- Token matching: a token that supplies an address matches a configured symbol only when that address is the symbol's canonical mint (SOL, USDC, USDT, WBTC, WETH, TAO in `core/constants.py`). Symbols with no canonical mint still match by symbol, so add their mints to `tokens.json` when spoofing matters.
- `--min-bin-step`/`--max-bin-step` are sent to the Meteora discovery API as `dlmm_bin_step` filters, so Meteora enforces them server-side (Sheldon owns the policy behind them). Raydium and Orca do not apply them, and the CLI warns when they are set for those DEXes.
- Other threshold flags (`--min-fee-tvl`, `--min-daily-fee`, `--min-apr`, `--max-volatility`, `--min-fee-pct`, `--max-fee-pct`) are parsed but not enforced locally. The CLI warns when they are non-default. Policy gates in `missy_policy.json` are the only local thresholds. `--min-tvl`/`--max-tvl` reach Meteora and Orca server-side only, and Raydium ignores them (warned).
- Pool-screen output: only `eligible=true` candidates count as qualified. Policy-failed pools are emitted with `eligible=false` and summarized by gate in the table.
- Public API data is cached/indexed and is suitable for screening, not transaction settlement; any future trade path must re-read on-chain state through RPC before signing.
