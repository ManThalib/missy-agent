import sys
import time
sys.path.insert(0, '.')

from pool_screener.screener_py.clients.raydium_client import RaydiumClient
from pool_screener.screener_py.filter_config import FilterConfig

print("Creating RaydiumClient...")
client = RaydiumClient(timeout=10.0)
print("Creating FilterConfig...")
config = FilterConfig()
print("Calling fetch_pools...")
start = time.time()
try:
    pools = client.fetch_pools(config, max_pages=1)
    elapsed = time.time() - start
    print(f"Success! Got {len(pools)} pools in {elapsed:.2f} seconds")
    if pools:
        print("First pool:", pools[0])
except Exception as e:
    elapsed = time.time() - start
    print(f"Error after {elapsed:.2f} seconds: {e}")
    import traceback
    traceback.print_exc()
