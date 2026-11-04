import json
from pathlib import Path

def merge_pools():
    pool_json_path = Path('cache/pool.json')
    pools_1_json_path = Path('cache/pools_1.json')
    
    if not pools_1_json_path.exists():
        print(f"Error: {pools_1_json_path} does not exist.")
        return
    
    if not pool_json_path.exists():
        print(f"Error: {pool_json_path} does not exist.")
        return

    print(f"Loading {pools_1_json_path}...")
    with open(pools_1_json_path, 'r') as f:
        pools_1_data = json.load(f)
    
    print(f"Loading {pool_json_path}...")
    with open(pool_json_path, 'r') as f:
        pool_data = json.load(f)
    
    initial_count = len(pools_1_data.get('pools', {}))
    print(f"Initial pool count in pools_1.json: {initial_count}")
    
    # Ensure 'pools' key exists in pools_1_data
    if 'pools' not in pools_1_data:
        pools_1_data['pools'] = {}
        
    new_pools_added = 0
    duplicates_found = 0
    
    for addr, pool_info in pool_data.items():
        addr_lower = addr.lower()
        if addr_lower not in pools_1_data['pools']:
            pools_1_data['pools'][addr_lower] = pool_info
            new_pools_added += 1
        else:
            duplicates_found += 1
            
    print(f"Added {new_pools_added} new pools from pool.json")
    print(f"Skipped {duplicates_found} duplicates")
    
    final_count = len(pools_1_data['pools'])
    print(f"Final pool count in pools_1.json: {final_count}")
    
    print(f"Saving merged data to {pools_1_json_path}...")
    # Use separators to save space if needed or pretty print? 
    # The original was minified, so let's keep it compact.
    with open(pools_1_json_path, 'w') as f:
        json.dump(pools_1_data, f, separators=(',', ':'))
    
    print("Done.")

if __name__ == "__main__":
    merge_pools()
