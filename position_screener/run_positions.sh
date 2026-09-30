#!/bin/bash
# Missy position screener cronjob wrapper.
# Scans active Orca LP positions from the executor wallet via Solana RPC,
# writes timestamped JSON output.
set -u
REPO=/data/missy-agent/position_screener
OUTDIR=/data/missy-data/position_scans
LOG=/data/missy-data/position_scans/cron.log
# Use Asia/Shanghai (UTC+8) for all timestamps and filenames.
export TZ=Asia/Shanghai
WALLET="${SOLANA_PUBLIC_WALLET:?SOLANA_PUBLIC_WALLET is required}"
# RPC credentials come from the injected environment (OpenClaw secrets store):
# SOLANA_RPC_URL must be the full Helius endpoint including its api-key parameter.
export SOLANA_RPC_URL="${SOLANA_RPC_URL:?SOLANA_RPC_URL is required (inject from secrets store)}"
if [ -z "${HELIUS_API_KEY:-}" ]; then
  # Derive the key from the injected URL so SOLANA_RPC_URL is the single source.
  HELIUS_API_KEY=${SOLANA_RPC_URL##*api-key=}
  HELIUS_API_KEY=${HELIUS_API_KEY%%[!A-Za-z0-9_-]*}
fi
export HELIUS_API_KEY
: "${HELIUS_API_KEY:-}" >/dev/null || true  # empty key only disables history scan
mkdir -p "$OUTDIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUTDIR/position_scan-$TS.json"
cd "$REPO" || exit 1
# Buffer stderr so failure context can be embedded in the .failed record.
ERRTMP=$(mktemp)
python3 position_screener.py --wallet "$WALLET" --dex orca --json > "$OUT" 2> "$ERRTMP"
STATUS=$?
cat "$ERRTMP" >> "$LOG"
if [ $STATUS -ne 0 ]; then
  ERRMSG=$(grep -m1 'Error during position scan' "$ERRTMP" || tail -n 1 "$ERRTMP" || true)
  ERRMSG=${ERRMSG#Error during position scan: }
  # Single line, JSON-safe, bounded.
  ERRMSG=$(printf '%s' "$ERRMSG" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\\\"/g' | head -c 300)
  echo "[$(date +%FT%T%z)] FAILED exit=$STATUS out=$OUT" >> "$LOG"
  # The stdout file is empty on failure; write an error record instead of
  # moving a zero-byte file so .failed output always carries context.
  printf '{"scan_status":"failed","exit_code":%s,"timestamp":"%s","wallet":"%s","error":"%s"}\n' \
    "$STATUS" "$(date +%FT%T%z)" "$WALLET" "$ERRMSG" > "$OUT.failed"
  rm -f "$OUT" "$ERRTMP"
  exit $STATUS
fi
rm -f "$ERRTMP"
python3 -c "import json;json.load(open('$OUT'))" >> "$LOG" 2>&1 || {
  echo "[$(date +%FT%T%z)] INVALID JSON out=$OUT" >> "$LOG"
  mv "$OUT" "$OUT.invalid" 2>/dev/null
  exit 1
}
echo "[$(date +%FT%T%z)] OK out=$OUT" >> "$LOG"
echo "$OUT"
