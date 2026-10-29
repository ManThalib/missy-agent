"""Manual live check of the Raydium pool API (network access required).

Not a unit test: it calls the public API, so it runs only as a script:
    python3 test_raydium.py
"""

import sys
import time
import traceback

sys.path.insert(0, ".")

from pool_screener.screener_py.clients.raydium_client import RaydiumClient
from pool_screener.screener_py.filter_config import FilterConfig


def main() -> int:
    client = RaydiumClient(timeout=10.0)
    config = FilterConfig()
    start = time.time()
    try:
        pools = client.fetch_pools(config, max_pages=1)
    except Exception as exc:
        print(f"Error after {time.time() - start:.2f} seconds: {exc}")
        traceback.print_exc()
        return 1
    print(f"Success! Got {len(pools)} pools in {time.time() - start:.2f} seconds")
    if pools:
        print("First pool:", pools[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
