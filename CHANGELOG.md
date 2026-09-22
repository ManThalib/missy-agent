# Changelog

All notable changes to the Missy-agent Multi-DEX Screener Suite are documented here.

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
