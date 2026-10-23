# Latest Scan Report — Missy Agent

Date (UTC): 2026-10-08
Wallet scanned: `QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj` (wallet_id: `main`)
RPC: `https://mainnet.helius-rpc.com/?api-key=****` (Helius, injected via `SOLANA_RPC_URL`)
DEX scope: `all` (meteora / raydium / orca)

Commands used:
```bash
cd pool_screener && SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=****" python3 pool_screener.py --dex all --pages 1 --page-size 25 --json
cd ../position_screener && SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=****" HELIUS_API_KEY="****" python3 position_screener.py --wallet QNsxSBe2tHb7RSQYh6TUBUQA2wD93PvPP5Jg3qUxPvj --wallet-id main --dex all --json
```

---

## 1. Latest active position scan (live)

Result: **1 active position, 0 errors, history_complete=true**.

| Field | Value |
|---|---|
| dex | raydium |
| status | active |
| pool_address | `3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv` |
| position_address | `9X3StbggkAiqwMDmYgGynwkAKcsY96AZbgnpNLEnJt1U` |
| position_mint | `CXBQbhJHHXJ13N73ji2cjh9v6v6FQzEaBjRRVK8gtgsQ` |
| liquidity_raw | 3816337536 |
| range (tick lower → upper) | -22044 → -20640 |
| in_range | false |
| fees_owed_raw | [3258556, 375726] |
| pending_fees_source | computed (live PoolState + tick arrays, not checkpoint) |
| rewards_owed_raw | [0, 0, 0] |
| reward_mints | `4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R` (6dp, likely RAY), + 2 inactive slots |
| opened_signature | `A32DecJ3nmcq9qo4nAU4RpSXWhXLa1xoVAQEVWc7nVZVY6XsjfdkTm8XoR3KwmzLMwp5DLAVKcwUs4FTCEwMYBA` |
| opened_at | 1790981072 |
| has_claimable | true |
| fees_usd / rewards_usd / current_value_usd | 0.0 locally (see note below) |
| position_features | null locally (see note below) |

Pool join (from same-day pool scan, same `pool_address`):
- SOL-USDC, Raydium Concentrated, tick_spacing 1
- tvl ~$7.78M, fee_tvl_ratio 0.129%, daily_fee ~$10,022, volume ~$25.05M
- volatility 9.54%, APR 46.99%, score 92.30, eligible=true
- current_tick -21812 (RPC batched), price_source jupiter_v2

Note on local `null`s: `enrichment.py:_latest_pool_scan()` joins against `/data/missy-data/pool_screens/pool_scan-*.json`. That path does not exist on this laptop, so enrichment skipped pool-derived USD math and `position_features` stayed null. On prod cron (see `run_positions.sh`) the path exists and the same position emits a full `position_features` block plus `fees_usd`, `current_value_usd`, `in_range`, `current_price/lower_price/upper_price`.

Raw JSON saved during this run: `/var/folders/dk/9hx6x0kd67dcq45mvbbllkhr0000gn/T/opencode/position_latest.json` (1 position).

---

## 2. New indicator: `position_features` v1

Source: `position_screener/screener_position_py/position_features.py`, `FEATURES_VERSION = 1`.

Design rule (owner decision 2026-10-04): **Missy owns discovery + feature vector; Sheldon owns scoring policy and decisions.** The block carries **features only, no score, no verdict**. Missing inputs are never zero-filled — they are listed in `gaps` so consumers apply their own unknown-data policy (fail-closed).

Fields emitted per position:

**Range geometry (unit-agnostic bins/ticks):**
- `current_bin_id`, `range_lower`, `range_upper`, `range_width`
- `bins_to_lower`, `bins_to_upper`, `edge_distance_frac` (0 at edge, 1 dead-center)
- `in_range` (bool, null when unknown)

**Side composition (value-weighted):**
- `side_bias_x` (0–1, share of value in token X)
- `single_sided` (true when all value is one side)

**Value / yield:**
- `value_usd`, `value_known`
- `fees_usd`, `rewards_usd`
- `days_open`, `fees_apr_pct` (= fees/value/days*365*100, only if days_open >= 1h)

**Pool context (joined from pool scan):**
- `pool_realized_fee_apr`, `pool_volatility_pct`, `pool_tvl_usd`, `pool_bin_step`
- `pool_score`, `pool_score_model`, `pool_score_version`, `pair_class`

**Data-quality:**
- `gaps`: e.g. `pool_record_missing`, `range_unknown`, `side_unknown`, `value_unknown`, `fees_unknown`, `fees_apr_unknown`, `days_open_unknown`, `no_pool_metrics`
- `version`, `dex`, `position_address`, `pool_address`, `wallet_id`, `status`

Pool-side companion (already live): `new_fc_score` on every pool candidate (`pool_screener/screener_py/pool_scorer.py:score_fee_capture`), 0–100 with `fee_yield_score` (35), `absolute_fee_score` (25), `liquidity_effectiveness` (20), `turnover_penalty_score` (10), `lp_share_adjustment` (10).

---

## 3. How to use it (send this to the next agent)

```text
You receive a PositionScan JSON where each position has a `position_features` object (version 1, see position_features.py).
Treat it as the single source of truth for position state. Do NOT re-derive chain math.
Scoring/policy lives with you (Sheldon); Missy only gives facts + gaps.

Suggested consumer logic:
1. If `status == "unknown"` → skip (degraded provider, see _mark_degraded_providers).
2. If `gaps` contains `value_unknown` or `fees_unknown` → mark UNVERIFIABLE, do not close/reopen on APR alone.
3. Else compute: edge risk = 1 - edge_distance_frac; capital efficiency = fees_apr_pct vs pool_realized_fee_apr; drift = side_bias_x vs 0.5.
4. Combine with pool `new_fc_score` breakdown to rank HOLD / REBALANCE / CLOSE candidates.
5. Log which gap triggered any fail-closed decision for audit.
```

---

## 4. How it improves decision-making

1. **Kills false REBALANCE on missing data:** before, absent ticks defaulted `in_range=false` and looked verifiably out-of-range (2026-10-03 incident). Now degraded providers become `status=unknown` + `gaps`, so policy can fail-closed instead of acting on silence.
2. **Separates facts from policy:** Missy no longer embeds a verdict. Sheldon can A/B scoring models (`missy-default` vs `new_fc_score` vs position-level policy) without re-scanning chain.
3. **Range-risk becomes comparable cross-DEX:** `edge_distance_frac` and `bins_to_lower/upper` normalize Meteora bins and Orca/Raydium ticks into one geometry, so one rule works for DLMM + Whirlpool + CLMM.
4. **Realized vs projected yield:** `fees_apr_pct` (what *this* position actually earned) vs `pool_realized_fee_apr` (what the pool paid) exposes mis-positioned ranges even when pool APR looks high.
5. **Side-drift / IL early warning:** `side_bias_x` + `single_sided` shows when a position has gone 100% one token (out-of-range), before value collapses.
6. **Auditability:** `version` + `gaps` + `pending_fees_source` (`computed` vs `raw_checkpoint` vs `raw_per_bin_sum`) tells exactly how much to trust each dollar figure.

---

## 5. What to provide for full results next time

- `SOLANA_RPC_URL` (full Helius endpoint with `?api-key=`): enables batched tick/bin fetch + live pending-fee SDK math. Without it, ticks stay unknown and fees fall back to stale checkpoints.
- `HELIUS_API_KEY` (or derivable from the URL): enables closed-position history; without it `errors.history` notes history was skipped.
- `SOLANA_PUBLIC_WALLET` + `WALLET_ID`: wallet to scan and logical tag (`main` = policy wallet).
- Prod pool-scan path `/data/missy-data/pool_screens/pool_scan-*.json`: required for `fees_usd`, `current_value_usd`, and `position_features` enrichment. One bad pool record never aborts the scan.
