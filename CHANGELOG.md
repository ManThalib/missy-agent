# Changelog

All notable changes to the Missy-agent Multi-DEX Screener Suite are documented here.

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

#### Wallet Scanner

- **New `wallet_scanner/` module** built on `core/` with one class per file:
  - `config.py` — `USD_THRESHOLD` (default `$0.10`), endpoints, program IDs,
    and `resolve_rpc_url()` / `resolve_wallet()` helpers.
  - `models.py` — `TokenBalance` dataclass with `to_dict()` and a
    `native_sol()` factory.
  - `rpc_client.py` — `SolanaRpcClient` with linear-backoff retries for native
    SOL and SPL/Token-2022 token accounts.
  - `price_fetcher.py` — `TokenPriceFetcher` batching Jupiter Price API v2 calls.
  - `balance_calculator.py` — `BalanceCalculator` for USD valuation and
    threshold filtering.
  - `wallet_scanner.py` — `WalletScanner` orchestrator returning assets sorted
    by descending USD value.
  - `main.py` — CLI with `--wallet`, `--threshold`, `--json`, `--watch`,
    `--include-dust`, and `--rpc-url`/`--price-url` overrides.
- **Wallet scanner tests** (`wallet_scanner/test_wallet_scanner.py`).

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
