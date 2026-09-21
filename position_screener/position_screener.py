import argparse
import json
import sys
import time

from screener_position_py import PositionScanner
from screener_position_py.analytics.closure_state import scan_closure_state
from screener_position_py.display import print_positions

def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Solana Position Screener.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--wallet",
        type=str,
        required=True,
        help="Solana wallet whose active and historical LP positions are analyzed",
    )
    parser.add_argument(
        "--dex",
        type=str,
        default="all",
        choices=["all", "meteora", "raydium", "orca"],
        help="Provider to scan (all queries all three)",
    )
    parser.add_argument(
        "--position-history-pages",
        type=_non_negative_int,
        default=0,
        help=(
            "Maximum Helius history pages (100 transactions each; "
            "0 scans all history)"
        ),
    )
    parser.add_argument(
        "--show-inactive",
        action="store_true",
        help="Show closed/inactive positions in the position listing",
    )
    parser.add_argument(
        "--watch",
        type=_non_negative_int,
        default=0,
        help="Watch mode: poll interval in seconds",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output raw JSON instead of table"
    )

    args = parser.parse_args(argv)
    if args.json and args.watch:
        parser.error("--json cannot be combined with --watch")
    return args

def main() -> int:
    args = parse_args()
    position_scanner = PositionScanner()

    def run_cycle() -> bool:
        current_time = time.strftime("%H:%M:%S")
        output = sys.stderr if args.json else sys.stdout
        print(f"\n[{current_time}] Scanning positions for {args.wallet} on {args.dex}...", file=output)

        try:
            position_scan = position_scanner.scan(
                args.wallet,
                dex=args.dex,
                history_pages=args.position_history_pages,
            )
        except Exception as exc:
            print(f"Error during position scan: {exc}", file=sys.stderr)
            return False

        for source, error in position_scan.errors.items():
            print(f"Warning: position {source}: {error}", file=sys.stderr)

        newly_closed = set()
        try:
            _, newly_closed_keys = scan_closure_state(position_scan.positions)
            newly_closed = set(newly_closed_keys)
            if newly_closed_keys:
                print(
                    f"Note: {len(newly_closed_keys)} position(s) "
                    "closed since last scan",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(
                f"Warning: closure state could not be saved: {exc}",
                file=sys.stderr,
            )

        if args.json:
            payload = position_scan.to_dict()
            json.dump(
                payload,
                sys.stdout,
                indent=2,
            )
            print()
            return True

        print_positions(
            position_scan,
            show_inactive=args.show_inactive,
            newly_closed=newly_closed,
        )
        return True

    succeeded = run_cycle()
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
                run_cycle()
        except KeyboardInterrupt:
            print("\nStopped by user.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
