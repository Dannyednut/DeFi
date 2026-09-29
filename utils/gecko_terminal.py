import requests
import json
import time
import threading

from config import CHAIN_ID

from log import get_logger
log = get_logger("gecko_terminal")

NETWORKS: dict[int, str] = {
    1: "eth",
    56: "bsc",
    137: "pos",
    8453: "base",
    42161: "arbitrum-one",
    324: "zksync",
    11155111: "sepolia-testnet",
}

class GeckoTerminal():

    def __init__(self):
        self.dexes = {
            "eth": {
                "1": {"name": "UniswapV2", "version": 2},
                "6": {"name": "SushiswapV2", "version": 2},
                "28": {"name": "UniswapV3", "version": 3},
                "2644": {"name": "PancakeV2", "version": 2},
                "5021": {"name": "PancakeV3", "version": 3},
                "11125": {"name": "SushiswapV3", "version": 3},
            },
            "sepolia-testnet": {
                "9871": {"name": "UniswapV3", "version": 3},
                "9872": {"name": "UniswapV2", "version": 2},
            },
            "bsc": {
                "2": {"name": "PancakeV2", "version": 2},
                "5020": {"name": "PancakeV3", "version": 3},
                "4657": {"name": "UniswapV3", "version": 3},
                "43": {"name": "BiswapV2", "version": 2},
                "48": {"name": "SushiswapV2", "version": 2},
                "896": {"name": "BakeryswapV2", "version": 2},
                "33": {"name": "ApeswapV2", "version": 2},
                "84": {"name": "BabyswapV2", "version": 2},
                "6340": {"name": "SushiswapV3", "version": 3},
            },
        }
        self.base_url = "https://app.geckoterminal.com/api/p1"
        self._pool_cache = {}
        self._cache_ttl = 300.0
        self._cache_lock = threading.Lock()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def pool_info(self, chain, address, dex=None):
        network = NETWORKS.get(chain, None)
        if not network:
            log.error(f"Unsupported chain ID: {chain}")
            return {}
        if dex:
            # Return early if provided dex is not available for the network in self.dexes
            if dex not in [d['name'] for d in self.dexes.get(network, {}).values()]:
                log.warning(f"Dex {dex} is not available for network {network}")
                return {}
        url = f"{self.base_url}/{network}/pools/{address}"

        try:
            cache_key = (int(chain), str(address).lower())
            now = time.time()
            with self._cache_lock:
                cached = self._pool_cache.get(cache_key)
                if cached and now - cached[0] < self._cache_ttl:
                    return dict(cached[1])

            response = requests.get(url, headers=self.headers, timeout=8)
            
            if response.status_code == 200:
                data = response.json()
                # Extract the reserve_in_usd from the official v2 API response architecture
                p = {
                    "address": data['data']['attributes']['address'].lower(),
                    "token0_Id": list(data['data']['attributes']['token_reserves'].keys())[0],
                    "token1_Id": list(data['data']['attributes']['token_reserves'].keys())[1],
                    "tvl_usd": data['data']['attributes']['reserve_in_usd'],
                    "fee": int(float((data['data']['attributes'].get('pool_fee', '0.3') or '0.3').replace("%", "")) * 100),
                    "dex": self.dexes.get(network, {}).get(data['data']['relationships']['dex']['data']['id'], {}).get('name', 'Unknown'),
                    "version": self.dexes.get(network, {}).get(data['data']['relationships']['dex']['data']['id'], {}).get('version', 'Unknown'),
                    "error": None
                }

                with self._cache_lock:
                    self._pool_cache[cache_key] = (now, dict(p))
                return p
            else:
                return {"error": response.status_code}
        except Exception as e:
            log.error(f"Exception occurred while fetching info for {address} on {network}: {e}")
            return {}


    def pool_tvl_usd(self, chain, address, dex=None):
        """Return GeckoTerminal's pool TVL when available, using the same TTL cache."""
        info = self.pool_info(chain, address, dex=dex)
        value = info.get("tvl_usd") if isinstance(info, dict) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def related_pools(self, chain, address):
        network = NETWORKS.get(chain, None)
        if not network:
            log.error(f"Unsupported chain ID: {chain}")
            return []
        url = f"{self.base_url}/{network}/pools/{address}/related_pools"

        try:
            pool_info = self.pool_info(chain, address)
            if not pool_info:
                log.error(f"Failed to fetch pool info for {address} on {network}")
                return []
            
            t0Id = pool_info.get('token0_Id')
            t1Id = pool_info.get('token1_Id')

            if not t0Id or not t1Id:
                return []

            cache_key = (int(chain), str(address).lower())
            now = time.time()
            with self._cache_lock:
                cached = self._pool_cache.get(cache_key)
                if cached and now - cached[0] < self._cache_ttl:
                    return dict(cached[1])

            response = requests.get(url, headers=self.headers, timeout=8)
            
            if response.status_code == 200:
                data = response.json()
                # Extract the reserve_in_usd from the official v2 API response architecture
                l = data.get("data", {})
                p = []
                for i in l:
                    dex = i['relationships']['dex']['data']['id']
                    tokens = [t['id'] for t in i['relationships']['tokens']['data']]
                    if dex in self.dexes.get(network) and t0Id in tokens and t1Id in tokens:
                        info = self.pool_info(chain, i['attributes']['address'])
                        p.append(info)
                return p
            else:
                return []
        except Exception as e:
            log.error(f"Exception occurred while fetching related pools for {address} on {network}: {e}")
            return []

    
