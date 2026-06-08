import json
import os
import time
import tempfile
import asyncio
import copy
from pathlib import Path
from thegraph import get_uniswap_v2_data, get_uniswap_v3_data, get_sushiswap_v2_data

POOLS_FILE = "cache/pools.json"
POOLS_1_FILE = "cache/pools_1.json"
BACK_UP_FILE = "cache/pools_1_backup.json"

# --- Pool Logic ---

def load_pools(filepath: str) -> dict:
    """Load pools from JSON file, return empty dict or basic schema if file doesn't exist."""
    path = Path(filepath)
    if not path.exists():
        if filepath == POOLS_1_FILE:
             return {"chain_id": 1, "schema_version": 3, "pools": {}}
        return {"pools": {}}
    
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return {"pools": {}}

def save_json_atomic(filepath: str, data: dict) -> None:
    """Atomic save — prevents corruption on crash."""
    path = Path(filepath)
    dir_name = path.parent
    dir_name.mkdir(parents=True, exist_ok=True)
    
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    
    # On Windows, os.replace works like a move
    try:
        if path.exists():
            path.unlink() # Safer for Windows sometimes
        os.rename(tmp_path, str(path))
    except Exception as e:
        print(f"Error saving {filepath}: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def add_pool(
    pools_dict: dict,
    address: str,
    token0: str,
    token1: str,
    dex: str,
    version: int,
    feeTier: int,
    reserve0: float = 0,
    reserve1: float = 0,
    sqrt_price_x96: int = 0,
    liquidity: int = 0,
    tvl_usd: float = 0.0
) -> bool:
    """Add a single new pool to the dictionary. Returns True if added, False if skipped."""
    address = address.lower()

    if address in pools_dict:
        return False

    pools_dict[address] = {
        "token0":         token0.lower(),
        "token1":         token1.lower(),
        "dex":            dex,
        "version":        version,
        "fee_bps":        int(feeTier / 100),
        "r0":             reserve0,
        "r1":             reserve1,
        "sqrt_price_x96": sqrt_price_x96,
        "liquidity":      liquidity,
        "tvl_usd":        tvl_usd,
        "last_updated":   time.time(),
        "discovered_at":  time.time(),
        "is_pending":     False,
    }
    return True

def add_pools_batch(pools_dict: dict, new_pools_data: list[dict], dex: str, version: int) -> tuple[int, int]:
    """Add multiple pools at once from subgraph response."""
    added, skipped = 0, 0

    for p in new_pools_data:
        address = p["id"].lower()

        # Extract values safely
        t0 = p["token0"]
        t1 = p["token1"]
        
        # decimals are needed to convert string reserves to numbers
        d0 = int(t0.get("decimals", 18))
        d1 = int(t1.get("decimals", 18))

        success = add_pool(
            pools_dict,
            address,
            token0         = t0["id"],
            token1         = t1["id"],
            dex            = dex,
            version        = version,
            feeTier        = int(p.get("feeTier") or 3000),
            reserve0       = float(p.get("reserve0") or 0) * (10**d0) if version == 2 else 0,
            reserve1       = float(p.get("reserve1") or 0) * (10**d1) if version == 2 else 0,
            liquidity      = int(p.get("liquidity") or 0),
            sqrt_price_x96 = int(p.get("sqrtPrice") or 0),
            tvl_usd        = float(p.get("totalValueLockedUSD") or p.get("reserveUSD") or 0),
        )
        
        if success:
            added += 1
        else:
            skipped += 1

    print(f"[{dex}] Added: {added} | Skipped: {skipped}")
    return added, skipped

# --- Query Building ---

def v3_query(skip, existing_addresses):
    # GraphQL doesn't like huge arrays in id_not_in, we'll limit it if needed
    # but for now we'll just use the list.
    return f"""
{{
  pools(first: 1000, skip: {skip}, where: {{totalValueLockedUSD_gt: 1000, liquidity_gt: 0, id_not_in: {json.dumps(existing_addresses)}}}) {{
    id
    token0 {{ id symbol decimals }}
    token1 {{ id symbol decimals }}
    liquidity
    sqrtPrice
    totalValueLockedUSD
    feeTier
  }}
}}
"""

def v2_query(skip, existing_addresses):
    return f"""
{{
    pairs(first: 1000, skip: {skip}, where: {{reserveUSD_gt: 1000, reserve0_gt: 0, reserve1_gt: 0, id_not_in: {json.dumps(existing_addresses)}}}) {{
        id
        token0 {{ id symbol decimals }}
        token1 {{ id symbol decimals }}
        reserve0
        reserve1
        reserveUSD
    }}
}}
"""

# --- Main Logic ---

async def main():
    # 1. Load existing data
    data = load_pools(BACK_UP_FILE)
    if "pools" not in data: data["pools"] = {}
    
    pools_1_data = load_pools(POOLS_1_FILE)
    if "pools" not in pools_1_data: pools_1_data["pools"] = {}

    # # Initial state
    # existing_pools = {a:p for a,p in pools_1_data["pools"].items() if not (p.get('is_pending') == False and (p['r0'] % 1e6 > 0 or p['r1'] % 1e6 > 0))} # data["pools"]
    # old_pools = existing_pools.copy()
    # existing_addresses = list(existing_pools.keys()) # list(set(list(existing_pools.keys()) + list(pools_1_data["pools"].keys())))
    
    print(f"Currently tracking {len(data['pools'])} pools in pools.json")
    print(f"Combined unique addresses to skip: {len(pools_1_data['pools'])}")

    skip = 0
    total_added = 0
    
    # 2. Fetch Loop
    # while True:
    #     try:
    #         print(f"\nFetching batch (skip={skip})...")
            
    #         # Fetch from all 3 subgraphs in parallel
    #         tasks = [
    #             get_uniswap_v3_data(v3_query(skip, existing_addresses)),
    #             get_uniswap_v2_data(v2_query(skip, existing_addresses)),
    #             get_sushiswap_v2_data(v2_query(skip, existing_addresses))
    #         ]
            
    #         results = await asyncio.gather(*tasks)
    #         new_v3, new_v2, new_sushi = results

    #         batch_added = 0
    #         a, _ = add_pools_batch(existing_pools, new_v3, "UniswapV3", 3)
    #         batch_added += a
    #         a, _ = add_pools_batch(existing_pools, new_v2, "UniswapV2", 2)
    #         batch_added += a
    #         a, _ = add_pools_batch(existing_pools, new_sushi, "SushiswapV2", 2)
    #         batch_added += a

    #         total_added += batch_added
            
    #         if not new_v3 and not new_v2 and not new_sushi:
    #             print("No more new pools found.")
    #             break
                
    #         # If we've hit the skip limit (usually 5000), we should probably stop or change strategy
    #         # if skip >= 5000:
    #         #     print("Skip limit (5000) reached. Stopping to avoid subgraph limitations.")
    #         #     break

    #         skip += 1000
    #         # Wait to avoid rate limits
    #         await asyncio.sleep(1)

    #     except Exception as e:
    #         print(f"Error during fetch: {e}")
    #         break

    # # 3. Save pools.json
    # print(f"\nSummary: Added {total_added} new pools.")
    # save_json_atomic(POOLS_FILE, data)
    # print(f"Saved {len(existing_pools)} pools to {POOLS_FILE}")

    # # 4. Merge into pools_1.json
    # print(f"\nMerging results into {BACK_UP_FILE}...")
    # merged_count = 0
    # for addr, pool_info in existing_pools.items():
    #     if addr not in old_pools:
    #         pools_1_data["pools"][addr] = pool_info
    #         merged_count += 1
            
    # if merged_count > 0:
    #     save_json_atomic(BACK_UP_FILE, pools_1_data)
    #     print(f"Merged {merged_count} new pools into {BACK_UP_FILE}")
    #     print(f"Total pools in {BACK_UP_FILE}: {len(pools_1_data['pools'])}")
    # else:
    #     print("No new pools to merge.")

    # print("\nProcessing complete.")

if __name__ == "__main__":
    asyncio.run(main())
