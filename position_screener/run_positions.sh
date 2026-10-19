#!/bin/bash
# Missy position screener cronjob wrapper.
# Scans active Orca LP positions from the executor wallet via Solana RPC,
# writes timestamped JSON output.
set -u
# Resolve the screener directory from this script so the wrapper works from
# the workspace clone (/data/missy-agent does not exist on this host).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR="${POSITION_SCAN_DIR:-/data/missy-data/position_scans}"
LOG="${POSITION_SCAN_LOG:-$OUTDIR/cron.log}"
mkdir -p "$OUTDIR"
# Use Asia/Shanghai (UTC+8) for all timestamps and filenames.
export TZ=Asia/Shanghai
# Schedulers that do not inject the OpenClaw secret store (command payloads,
# plain cron) read the public wallet from this file instead.
PIPELINE_ENV_FILE="${PIPELINE_ENV_FILE:-/data/missy-data/pipeline.env}"
if [ -z "${SOLANA_PUBLIC_WALLET:-}" ] && [ -r "$PIPELINE_ENV_FILE" ]; then
  . "$PIPELINE_ENV_FILE"
fi
WALLET="${SOLANA_PUBLIC_WALLET:?SOLANA_PUBLIC_WALLET is required}"
# wallet_id tags every scan/position record for the multi-wallet mirror.
# MAIN is the policy wallet (Sheldon sizes from it); mirror wallets scan
# with WALLET_ID=main overridden. Default: main.
WALLET_ID="${WALLET_ID:-main}"
# RPC credentials come from the injected environment (OpenClaw secrets store)
# when available. Fall back to the public endpoint so the hourly pipeline can
# still produce a scan; Raydium/Orca inventory lookups may be rate limited.
if [ -z "${SOLANA_RPC_URL:-}" ]; then
  echo "[$(date +%FT%T%z)] WARN: SOLANA_RPC_URL not set; using public mainnet RPC" >> "$LOG"
  SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"
fi
export SOLANA_RPC_URL
if [ -z "${HELIUS_API_KEY:-}" ] && [[ "${SOLANA_RPC_URL}" == *"api-key="* ]]; then
  # Derive the key from the injected URL so SOLANA_RPC_URL is the single source.
  HELIUS_API_KEY=${SOLANA_RPC_URL##*api-key=}
  HELIUS_API_KEY=${HELIUS_API_KEY%%[!A-Za-z0-9_-]*}
fi
export HELIUS_API_KEY
: "${HELIUS_API_KEY:-}" >/dev/null || true  # empty key only disables history scan
TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUTDIR/position_scan-$TS.json"
cd "$REPO" || exit 1
# Buffer stderr so failure context can be embedded in the .failed record.
ERRTMP=$(mktemp)
python3 position_screener.py --wallet "$WALLET" --wallet-id "$WALLET_ID" --dex all --json > "$OUT" 2> "$ERRTMP"
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
