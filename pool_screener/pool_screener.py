import argparse
import json
import os
import sys
import time

from screener_py import (
    DEFAULT_CONFIG,
    CandidateJSONEncoder,
    FilterConfig,
    MultiDexScreener,
    Whitelist,
    print_results,
)

def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Multi-DEX Solana Pool Screener: Meteora DLMM, Raydium, and "
            "Orca Whirlpools. Public read APIs require no key."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dex",
        type=str,
        default=DEFAULT_CONFIG.dex,
        choices=["all", "meteora", "raydium", "orca"],
        help="Provider to scan (all queries all three)",
    )
    parser.add_argument(
        "-c",
        "--config-file",
        type=str,
        default="tokens.json" if os.path.exists("tokens.json") else "",
        help="Path to JSON file containing target_tokens and allowed_paired_tokens",
    )
    parser.add_argument(
        "-tokens",
        "--tokens",
        type=str,
        default="",
        help="Comma-separated whitelisted target token symbols or mints",
    )
    parser.add_argument(
        "--paired-tokens",
        type=str,
        default="",
        help="Comma-separated allowed paired/quote token symbols or mints",
    )
    parser.add_argument(
        "--min-tvl",
        type=float,
        default=DEFAULT_CONFIG.min_tvl,
        help="Minimum pool TVL in USD",
    )
    parser.add_argument(
        "--max-tvl",
        type=float,
        default=DEFAULT_CONFIG.max_tvl,
        help="Maximum pool TVL (0 = no cap)",
    )
    parser.add_argument(
        "--min-fee-tvl",
        type=float,
        default=DEFAULT_CONFIG.min_fee_tvl,
        help="Minimum daily Fee/TVL ratio %%",
    )
    parser.add_argument(
        "--min-daily-fee",
        type=float,
        default=DEFAULT_CONFIG.min_daily_fee,
        help="Minimum daily fee USD",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=DEFAULT_CONFIG.min_volume_usd,
        help="Minimum window volume USD",
    )
    parser.add_argument(
        "--min-apr",
        type=float,
        default=DEFAULT_CONFIG.min_apr,
        help="Minimum risk-adjusted annualized APR %%",
    )
    parser.add_argument(
        "--min-bin-step",
        type=int,
        default=DEFAULT_CONFIG.min_bin_step,
        help="Minimum CLMM tickSpacing (Standard pools skip this gate; 0 = no floor)",
    )
    parser.add_argument(
        "--max-bin-step",
        type=int,
        default=DEFAULT_CONFIG.max_bin_step,
        help="Maximum CLMM tickSpacing (0 = no cap)",
    )
    parser.add_argument(
        "--min-fee-pct",
        type=float,
        default=DEFAULT_CONFIG.min_fee_pct,
        help="Minimum pool fee tier %% (0 = any)",
    )
    parser.add_argument(
        "--max-fee-pct",
        type=float,
        default=DEFAULT_CONFIG.max_fee_pct,
        help="Maximum pool fee tier %% (0 = any)",
    )
    parser.add_argument(
        "--max-volatility",
        type=float,
        default=DEFAULT_CONFIG.max_volatility,
        help="Maximum price-excursion %% over window",
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default=DEFAULT_CONFIG.timeframe,
        choices=["24h", "7d", "30d"],
        help="Provider statistics window (Meteora supports 24h in this CLI)",
    )
    parser.add_argument(
        "--pool-type",
        type=str,
        default=DEFAULT_CONFIG.pool_type,
        choices=["all", "concentrated", "standard"],
        help="Pool category (provider support varies)",
    )
    parser.add_argument(
        "--pages",
        type=_positive_int,
        default=DEFAULT_CONFIG.pages,
        help="Cursor pages to scan per pool type",
    )
    parser.add_argument(
        "--page-size",
        type=_positive_int,
        default=DEFAULT_CONFIG.page_size,
        help="Pools per page (provider maximums still apply)",
    )
    parser.add_argument(
        "--sort",
        type=str,
        default=DEFAULT_CONFIG.sort_field,
        choices=["liquidity", "volume24h", "fee24h", "apr24h"],
        help="API sort field",
    )
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        help="Watch mode: poll interval in seconds",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output raw JSON instead of table"
    )

    args = parser.parse_args(argv)
    if args.json and args.watch:
        parser.error("--json cannot be combined with --watch")
    if args.dex == "meteora" and args.timeframe != "24h":
        parser.error("Meteora CLI scans support only the 24h timeframe")
    for minimum, maximum, label in (
        (args.min_tvl, args.max_tvl, "TVL"),
        (args.min_bin_step, args.max_bin_step, "bin/tick spacing"),
        (args.min_fee_pct, args.max_fee_pct, "fee percentage"),
    ):
        if minimum < 0 or maximum < 0:
            parser.error(f"{label} limits cannot be negative")
        if maximum and minimum > maximum:
            parser.error(f"minimum {label} cannot exceed maximum {label}")
    for value, label in (
        (args.min_fee_tvl, "minimum fee/TVL"),
        (args.min_daily_fee, "minimum daily fee"),
        (args.min_volume, "minimum volume"),
        (args.min_apr, "minimum APR"),
        (args.max_volatility, "maximum volatility"),
    ):
        if value < 0:
            parser.error(f"{label} cannot be negative")
    return args

def load_whitelist(args) -> Whitelist:
    whitelist = (
        Whitelist.from_file(args.config_file) if args.config_file else Whitelist()
    )

    if args.tokens:
        whitelist.targets.replace(
            token for token in map(str.strip, args.tokens.split(",")) if token
        )

    if args.paired_tokens:
        whitelist.paired.replace(
            token for token in map(str.strip, args.paired_tokens.split(",")) if token
        )

    return whitelist

def main() -> int:
    args = parse_args()
    try:
        whitelist = load_whitelist(args)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    config = FilterConfig.from_args(args)
    screener = MultiDexScreener(whitelist=whitelist, config=config)

    def run_cycle() -> bool:
        current_time = time.strftime("%H:%M:%S")
        output = sys.stderr if args.json else sys.stdout
        print(
            f"\n[{current_time}] Scanning {config.dex} providers "
            f"({config.timeframe}, pages={config.pages})...",
            file=output,
        )
        try:
            candidates, rejects = screener.fetch_and_screen(max_pages=config.pages)
        except Exception as exc:
            print(f"Error during screening cycle: {exc}", file=sys.stderr)
            return False

        provider_errors = getattr(screener.client, "errors", {})
        for provider, error in provider_errors.items():
            print(f"Warning: {provider} provider failed: {error}", file=sys.stderr)

        if args.json:
            json.dump(
                candidates,
                sys.stdout,
                cls=CandidateJSONEncoder,
                indent=2,
                allow_nan=False,
            )
            print()
            return True

        print_results(candidates, rejects)
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
