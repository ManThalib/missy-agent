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
- The default `--dex all` queries Meteora DLMM, Raydium Standard/CLMM, and Orca Whirlpools. Keep provider payload conversion in `normalize_pool()` so filtering remains provider-independent.
- Missy emits facts; Sheldon owns policy. `MultiDexScreener.screen_pool()` no longer rejects on whitelist/TVL/volume/fee-tier/spacing/volatility/APR, and `WalletScanner.scan()` emits all assets (no dust filtering). Keep the neutral `score=0.0` placeholder and `threshold_usd` compat plumbing intact.
- This project uses only the Python standard library. Do not introduce a dependency or requirements file for ordinary HTTP/CLI work.

## Commands

- Run from the `pool_screener/` directory so the default `tokens.json` is found: `python3 pool_screener.py`.
- Focus one provider while debugging: `python3 pool_screener.py --dex meteora|raydium|orca --pages 1 --page-size 25`.
- Run all tests (each from its own directory, core from the repo root):
  ```bash
  cd pool_screener && python3 -m unittest test_screener
  cd ../position_screener && python3 -m unittest test_positions
  cd ../wallet_screener && python3 -m unittest test_wallet_screener
  cd .. && python3 -m unittest core.test_core
  ```
- Run one test: `python3 -m unittest test_screener.TestScreener.test_orca_pool_normalization`.
- Verify argument wiring after CLI edits: `python3 pool_screener.py --help`, `python3 position_screener.py --help`, or `python3 main.py --help` (wallet; `python3 -m wallet_screener.main --help` from the repo root).

## Provider Details

- Pool APIs require no API key. Optional base URL overrides are `METEORA_API_BASE` (default `https://pool-discovery-api.datapi.meteora.ag`), `RAYDIUM_API_BASE` (default `https://api-v3.raydium.io`), and `ORCA_API_BASE` (default `https://api.orca.so/v2/solana`); wallet scans use `SOLANA_RPC_URL` or Helius RPC when `HELIUS_API_KEY` is set.
- Provider pagination differs: Meteora uses `after_key`, Raydium uses `nextPageId`, and Orca uses `meta.next`. Preserve the 150 ms delay between pages.
- Raydium API time windows are `day/week/month`, Orca stats are `24h/7d/30d`, and Meteora only supports `5m/30m/24h`. The default `24h` works across all three.
- A failure from one provider must not discard data from the others; `MultiDexClient.errors` is printed as warnings. Raise only when every selected provider fails.
- Position-provider and history failures are warnings and must not discard screened pools or positions returned by other protocols. A failed position provider's records are marked `status=unknown`.
- Position enrichment reads the latest `/data/missy-data/pool_screens/pool_scan-*.json`; one bad pool record must not abort the scan. Raydium CLMM pending fees/rewards come from the stdlib SDK port in `raydium_pending.py`.
- Every scan and every position record carries `wallet_id` (default `main`); `run_positions.sh` / `run_wallet.sh` pass through `$WALLET_ID`.
- Orca `feeRate` is millionths; Raydium `feeRate` is a decimal fraction. Meteora already supplies `fee_pct`.
- Wallet scans price tokens via the Jupiter Price endpoint (`JUPITER_PRICE_V2_URL`, default `https://api.jup.ag/price/v3`) by default. The endpoint is keyless; override with the environment variable if needed.

## Filtering

- `tokens.json` carries token metadata (`asset`/`symbol`/`mint`/`address`/`value` plus `aliases`). Symbols and mint addresses are accepted; `WSOL` normalizes to `SOL`.
- `--min-bin-step`/`--max-bin-step` mean Meteora bin step or concentrated-pool tick spacing but are currently parsed, not enforced (Sheldon policy). Raydium Standard pools skip this gate.
- Public API data is cached/indexed and is suitable for screening, not transaction settlement; any future trade path must re-read on-chain state through RPC before signing.
