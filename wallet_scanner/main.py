"""CLI entry point for the Solana wallet scanner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from wallet_scanner.config import (
        JUPITER_PRICE_URL,
        USD_THRESHOLD,
        WALLET_ENV,
        resolve_wallet,
    )
    from wallet_scanner.wallet_scanner import WalletScanner
else:
    from .config import (
        JUPITER_PRICE_URL,
        USD_THRESHOLD,
        WALLET_ENV,
        resolve_wallet,
    )
    from .wallet_scanner import WalletScanner

SEPARATOR = "-" * 118


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Solana wallet scanner: fetch SOL/SPL/Token-2022 balances, price them "
            "in USD via Jupiter, and hide dust below the value threshold."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--wallet",
        type=str,
        default="",
        help=f"Wallet public key (falls back to ${WALLET_ENV})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=USD_THRESHOLD,
        help="Minimum total USD value required to display an asset",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default="",
        help="Solana RPC URL (defaults to Helius when HELIUS_API_KEY is set)",
    )
    parser.add_argument(
        "--price-url",
        type=str,
        default=JUPITER_PRICE_URL,
        help="Jupiter Price API v2 base URL",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        help="Watch mode: poll interval in seconds",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output raw JSON instead of a table"
    )
    parser.add_argument(
        "--include-dust",
        action="store_true",
        help="Show assets at or below the threshold (disables filtering)",
    )
    args = parser.parse_args(argv)
    if args.json and args.watch:
        parser.error("--json cannot be combined with --watch")
    if args.threshold < 0:
        parser.error("--threshold cannot be negative")
    if args.watch < 0:
        parser.error("--watch cannot be negative")
    return args


def print_scan(result: Dict[str, Any]) -> None:
    assets = result.get("assets", [])
    print(SEPARATOR)
    print(f"WALLET ASSETS  {result.get('wallet', '')}")
    print(SEPARATOR)
    if not assets:
        print(
            "No assets above the "
            f"${result.get('threshold_usd', 0):.2f} threshold."
        )
        print(SEPARATOR)
        return
    print(f"{'SYMBOL':<10} | {'MINT':<44} | {'AMOUNT':>18} | {'PRICE $':>12} | {'VALUE $':>12}")
    print(SEPARATOR)
    for asset in assets:
        symbol = asset.get("symbol") or ("SOL" if asset.get("is_native_sol") else "-")
        print(
            f"{symbol[:10]:<10} | "
            f"{asset.get('mint', ''):<44} | "
            f"{asset.get('amount_ui', 0.0):>18.6f} | "
            f"{asset.get('price_usd', 0.0):>12.6f} | "
            f"{asset.get('total_value_usd', 0.0):>12.2f}"
        )
    print(SEPARATOR)
    print(
        f"Total USD: ${result.get('total_usd', 0.0):,.2f} "
        f"across {result.get('asset_count', 0)} asset(s)"
    )
    print(SEPARATOR)


def run_cycle(args: argparse.Namespace, scanner: WalletScanner) -> bool:
    wallet = resolve_wallet(args.wallet)
    if not wallet:
        print(
            f"Error: provide --wallet or set ${WALLET_ENV}",
            file=sys.stderr,
        )
        return False
    timestamp = time.strftime("%H:%M:%S")
    output = sys.stderr if args.json else sys.stdout
    print(f"\n[{timestamp}] Scanning wallet {wallet}...", file=output)
    try:
        result = scanner.scan(wallet)
    except Exception as exc:
        print(f"Error during wallet scan: {exc}", file=sys.stderr)
        return False
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return True
    print_scan(result)
    return True


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    scanner = WalletScanner(
        rpc_url=args.rpc_url or None,
        price_url=args.price_url,
        threshold_usd=0.0 if args.include_dust else args.threshold,
        timeout=args.timeout,
    )

    succeeded = run_cycle(args, scanner)
    if not succeeded and not args.watch:
        return 1

    if args.watch > 0:
        print(
            f"\nWatch mode active. Polling every {args.watch} seconds... "
            "(Ctrl+C to stop)"
        )
        try:
            while True:
                time.sleep(args.watch)
                run_cycle(args, scanner)
        except KeyboardInterrupt:
            print("\nStopped by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
