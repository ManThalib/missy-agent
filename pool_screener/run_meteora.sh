#!/bin/bash
# Missy multi-DEX pool screener cronjob wrapper.
# Runs the Meteora + Raydium + Orca pool screener, writes timestamped JSON output.
set -u
REPO=/data/missy-agent/pool_screener
OUTDIR=/data/missy-data/pool_screens
LOG=/data/missy-data/pool_screens/cron.log
# Use Asia/Shanghai (UTC+8) for all timestamps and filenames.
export TZ=Asia/Shanghai

# Enable on-chain active-bin / current-tick enrichment.
# SOLANA_RPC_URL should be exported by the caller (automation / cron).
# Falls back to the OpenClaw shared store env value.
: "${HELIUS_API_KEY:?HELIUS_API_KEY is required}"
export SOLANA_RPC_URL="${SOLANA_RPC_URL:-https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY}}"

mkdir -p "$OUTDIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUTDIR/pool_scan-$TS.json"
cd "$REPO" || exit 1
python3 pool_screener.py --dex all --json --pages 1 > "$OUT" 2>> "$LOG"
STATUS=$?
if [ $STATUS -ne 0 ]; then
  echo "[$(date +%FT%T%z)] FAILED exit=$STATUS out=$OUT" >> "$LOG"
  mv "$OUT" "$OUT.failed" 2>/dev/null
  exit $STATUS
fi
# Sanity check: output must be valid JSON
python3 -c "import json;json.load(open('$OUT'))" >> "$LOG" 2>&1 || {
  echo "[$(date +%FT%T%z)] INVALID JSON out=$OUT" >> "$LOG"
  mv "$OUT" "$OUT.invalid" 2>/dev/null
  exit 1
}
echo "[$(date +%FT%T%z)] OK out=$OUT" >> "$LOG"
echo "$OUT"
