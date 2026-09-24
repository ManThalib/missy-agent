#!/bin/bash
# Missy wallet scanner cronjob wrapper.
# Scans the executor wallet balances, prices them via Jupiter, and writes
# timestamped JSON output.
#
# Required environment (set by the caller or automation):
#   SOLANA_RPC_URL   - Solana RPC endpoint (e.g. Helius)
#   WALLET_PUBLIC_KEY - Wallet to scan
#
# Example:
#   SOLANA_RPC_URL='https://...' WALLET_PUBLIC_KEY='...' ./run_wallet.sh
set -u
REPO=/data/missy-agent/wallet_scanner
OUTDIR=/data/missy-data/wallet_scans
LOG=/data/missy-data/wallet_scans/cron.log
BALANCE_CACHE=/data/missy-data/wallet_balances.json
# Use Asia/Shanghai (UTC+8) for all timestamps and filenames.
export TZ=Asia/Shanghai

if [ -z "${SOLANA_RPC_URL:-}" ]; then
  echo "Error: SOLANA_RPC_URL is not set" >&2
  exit 1
fi
if [ -z "${WALLET_PUBLIC_KEY:-}" ]; then
  echo "Error: WALLET_PUBLIC_KEY is not set" >&2
  exit 1
fi

mkdir -p "$OUTDIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUTDIR/wallet_scan-$TS.json"
LATEST="$OUTDIR/wallet_scan-latest.json"
cd "$REPO" || exit 1
python3 main.py --json > "$OUT" 2>> "$LOG"
STATUS=$?
if [ $STATUS -ne 0 ]; then
  echo "[$(date +%FT%T%z)] FAILED exit=$STATUS out=$OUT" >> "$LOG"
  mv "$OUT" "$OUT.failed" 2>/dev/null
  exit $STATUS
fi
python3 -c "import json;json.load(open('$OUT'))" >> "$LOG" 2>&1 || {
  echo "[$(date +%FT%T%z)] INVALID JSON out=$OUT" >> "$LOG"
  mv "$OUT" "$OUT.invalid" 2>/dev/null
  exit 1
}
# Write Sheldon/George-compatible balance cache keyed by symbol.
python3 - <<PY >> "$LOG" 2>&1
import json
out = "$OUT"
bal = "$BALANCE_CACHE"
with open(out) as fh:
    data = json.load(fh)
cache = {}
for asset in data.get("assets", []):
    sym = (asset.get("symbol") or "").upper()
    if sym:
        cache[sym] = {"usd_value": asset.get("total_value_usd", 0.0)}
with open(bal, "w") as fh:
    json.dump(cache, fh)
print(f"balance_cache={bal} assets={len(cache)}")
PY
ln -sf "$OUT" "$LATEST"
echo "[$(date +%FT%T%z)] OK out=$OUT" >> "$LOG"
echo "$OUT"
