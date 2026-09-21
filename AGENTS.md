# Multi-DEX Pool Screener

## Scope

- `pool_screener/pool_screener.py` and `position_screener/position_screener.py` are the CLI entrypoints; `screener_py/` and `screener_position_py/` own their respective logic.
- The default `--dex all` queries Meteora DLMM, Raydium Standard/CLMM, and Orca Whirlpools. Keep provider payload conversion in `normalize_pool()` so filtering remains provider-independent.
- This project uses only the Python standard library. Do not introduce a dependency or requirements file for ordinary HTTP/CLI work.

## Commands

- Run from this directory so the default `tokens.json` is found: `python3 pool_screener.py`.
- Focus one provider while debugging: `python3 pool_screener.py --dex meteora|raydium|orca --pages 1 --page-size 25`.
- Run all tests: `python3 test_screener.py (in pool_screener/) and python3 test_positions.py (in position_screener/)`; run one test: `python3 -m unittest test_screener.TestScreener.test_orca_pool_normalization`.
- Verify argument wiring after CLI edits: `python3 pool_screener.py --help`.

## Provider Details

- Pool APIs require no API key. Optional base URL overrides are `METEORA_API_BASE`, `RAYDIUM_API_BASE`, and `ORCA_API_BASE`; wallet scans use `SOLANA_RPC_URL` or Helius RPC when `HELIUS_API_KEY` is set.
- Provider pagination differs: Meteora uses `after_key`, Raydium uses `nextPageId`, and Orca uses `meta.next`. Preserve the 150 ms delay between pages.
- Raydium API time windows are `day/week/month`, Orca stats are `24h/7d/30d`, and Meteora only supports `5m/30m/24h`. The default `24h` works across all three.
- A failure from one provider must not discard data from the others; `MultiDexClient.errors` is printed as warnings. Raise only when every selected provider fails.
- Position-provider and history failures are warnings and must not discard screened pools or positions returned by other protocols.
- Orca `feeRate` is millionths; Raydium `feeRate` is a decimal fraction. Meteora already supplies `fee_pct`.

## Filtering

- `tokens.json` is both the target whitelist and paired-token allowlist. Symbols and mint addresses are accepted; `WSOL` normalizes to `SOL`.
- `--min-bin-step`/`--max-bin-step` mean Meteora bin step or concentrated-pool tick spacing. Raydium Standard pools skip this gate.
- Public API data is cached/indexed and is suitable for screening, not transaction settlement; any future trade path must re-read on-chain state through RPC before signing.
