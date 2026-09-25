#!/bin/bash
# Missy position screener cronjob wrapper.
# Scans active LP positions from the executor wallet via Solana RPC,
# writes timestamped JSON output.
set -u
REPO=/data/missy-agent/position_screener
OUTDIR=/data/missy-data/position_scans
LOG=/data/missy-data/position_scans/cron.log
# Use Asia/Shanghai (UTC+8) for all timestamps and filenames.
export TZ=Asia/Shanghai
WALLET="${SOLANA_PUBLIC_WALLET:?SOLANA_PUBLIC_WALLET is required}"
export SOLANA_RPC_URL="${SOLANA_RPC_URL:-https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY:?HELIUS_API_KEY is required}}"
export HELIUS_API_KEY="${HELIUS_API_KEY:?HELIUS_API_KEY is required}"
mkdir -p "$OUTDIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUTDIR/position_scan-$TS.json"
cd "$REPO" || exit 1
python3 position_screener.py --wallet "$WALLET" --json > "$OUT" 2>> "$LOG"
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
echo "[$(date +%FT%T%z)] OK out=$OUT" >> "$LOG"
echo "$OUT"
