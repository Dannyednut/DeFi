"""
utils/graph_client.py — Client for fetching DEX state from The Graph.
"""
from __future__ import annotations

import httpx
import asyncio
import json
from typing import Optional, Any

from config import CHAIN_ID, get_subgraph_providers
from log import get_logger
log = get_logger("graph_client")


GRAPH_PLAYGROUND_URL = "https://thegraph.com/explorer/api/playground/"
CHAIN_NATIVES = {1: "ETH", 56: "BNB"}

class GraphClient:
    """Async client for querying The Graph subgraphs."""
    
    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self._providers = get_subgraph_providers(CHAIN_ID)
        self._indices = {dex: 0 for dex in self._providers}
        
    async def query(self, func: str, dex_name: str, query: str) -> Optional[dict]:
        """Execute a GraphQL query against a DEX using round-robin provider selection and automatic retry/failover."""
        dex_key = dex_name.lower()
        candidates = self._providers.get(dex_key)
        if not candidates:
            log.warning(f"No providers configured for DEX: {dex_name}")
            return None
            
        max_retries = 3
        # Attempt query across providers
        for attempt in range(max_retries + 1):
            # Pick next provider in round-robin fashion
            idx = self._indices[dex_key]
            config = candidates[idx]
            self._indices[dex_key] = (idx + 1) % len(candidates)
            
            provider_type = config["type"]
            
            if provider_type == "graph":
                url = GRAPH_PLAYGROUND_URL + config["deployment_id"]
                headers = {
                    "accept": "application/json, multipart/mixed",
                    "content-type": "application/json",
                    "origin": "https://thegraph.com",
                    "referer": f"https://thegraph.com/explorer/subgraphs/{config['subgraph_id']}?view=Query&chain=arbitrum-one",
                    "user-agent": "Mozilla/5.0 (Antigravity DeFi Researcher)",
                }
            else:  # goldsky
                url = config["endpoint"]
                headers = {
                    "content-type": "application/json",
                }

            try:
                payload = {"query": query}
                response = await self._client.post(url, headers=headers, json=payload)
                
                if response.status_code == 429:
                    if attempt < max_retries:
                        delay = (attempt + 1) * 2
                        log.warning(f"Graph/Goldsky 429 for {dex_key} ({provider_type}) (atmp {attempt+1}/{max_retries}), switching provider and retrying in {delay}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        log.error(f"Graph query failed {func} ({dex_key}) after {max_retries} retries across providers.")
                        return None

                response.raise_for_status()
                return response.json()
                
            except Exception as e:
                if attempt < max_retries:
                    log.debug(f"Attempt {attempt+1} failed for {dex_key} on {provider_type}: {e}. Retrying next provider...")
                    continue
                log.error(f"Graph query error overall for {func} ({dex_key}): {e}")
                return None
        return None

    async def get_pools_data(self, dex_name: str, pool_addresses: list[str], intersect: bool = True) -> list[dict]:
        """Fetch top pools by volume/liquidity, chunked to prevent subgraph limits."""
        native = CHAIN_NATIVES.get(CHAIN_ID, "ETH")
        chunk_size = 1000 if intersect else len(pool_addresses)
        all_results = []
        log.info(f"Batching {len(pool_addresses)} pools for {dex_name}")

        async def q(dex_name, chunk_json: str, intersect) -> str:
            if "v3_ext" in dex_name.lower():
                query = f"""
                {{
                    liquidityPools(where: {{{"id_in" if intersect else "id_not_in"}: {chunk_json}}}) {{
                        id
                        fees(where: {{feeType: FIXED_TRADING_FEE}}){{feePercentage}}
                        inputTokens{{id symbol decimals lastPriceUSD}}
                        inputTokenWeights
                        inputTokenBalances
                        inputTokenBalancesUSD
                        activeLiquidity
                        totalLiquidity
                        totalValueLockedUSD
                    }}
                }}
                """
                result = await self.query('get_top_pools', dex_name, query)
                data = result.get("data", {}).get("liquidityPools", []) if result else []
            elif "v3" in dex_name.lower():
                query = f"""
                {{
                    pools(where: {{{"id_in" if intersect else "id_not_in"}: {chunk_json}}}) {{
                        id
                        token0 {{ id symbol decimals derivedETH }}
                        token1 {{ id symbol decimals derivedETH }}
                        token0Price
                        token1Price
                        liquidity
                        sqrtPrice
                        volumeUSD
                        totalValueLockedUSD
                    }}
                }}
                """
                result = await self.query("get_top_pools", dex_name, query)
                data = result.get("data", {}).get("pools", []) if result else []
            else:
                query = f"""
                {{
                  pairs(where: {{{"id_in" if intersect else "id_not_in"}: {chunk_json}}}) {{
                    id
                    token0 {{ id symbol decimals derivedETH }}
                    token1 {{ id symbol decimals derivedETH }}
                    reserve0
                    reserve1
                    reserveUSD
                    volumeUSD
                    token0Price
                    token1Price
                  }}
                }}
                """
                result = await self.query("get_top_pools", dex_name, query)
                data = result.get("data", {}).get("pairs", []) if result else []
            return data

        if not pool_addresses:
            # If no specific pool addresses provided, fetch all pools in one go
            data = await q(dex_name, '["0x0000000000000000000000000000000000000000"]', False)  # Empty list to fetch all pools
            all_results.extend(data)
        else:
            for i in range(0, len(pool_addresses), chunk_size):
                chunk = pool_addresses[i:i + chunk_size]
                chunk_json = json.dumps(chunk)

                data = await q(dex_name, chunk_json, intersect)
                all_results.extend(data)
                
                if len(pool_addresses) > chunk_size and i + chunk_size < len(pool_addresses):
                    await asyncio.sleep(1) # Gentle rate limit prevention between chunks
        log.info(f"Fetched {len(all_results)} pools for {dex_name}")     
        return all_results

    async def get_pool_data(self, dex_name: str, pool_address: str) -> list[dict]:
        """Fetch top pools by volume/liquidity."""
        native = CHAIN_NATIVES.get(CHAIN_ID, "ETH")
        if "uniswap_v3_ext" in dex_name.lower():
            query = f"""
            {{
                liquidityPool(id: "{pool_address}") {{
                    id
                    fees(where: {{feeType: FIXED_TRADING_FEE}}){{feePercentage}}
                    inputTokens{{id symbol decimals lastPriceUSD}}
                    inputTokenWeights
                    inputTokenBalances
                    inputTokenBalancesUSD
                    activeLiquidity
                    totalLiquidity
                    totalValueLockedUSD
                }}
            }}
            """
            result = await self.query("get_pool_data", dex_name, query)
            data = (result or {}).get("data", {}).get("liquidityPool", {})
            if not data:
                log.debug(f"Pool data (ext) not found for {pool_address} on {dex_name}")
            return data
        elif "v3" in dex_name.lower():
            query = f"""
            {{
                pool(id: "{pool_address}") {{
                    id  
                    token0 {{ id symbol decimals derived{native} }}
                    token1 {{ id symbol decimals derived{native} }}
                    token0Price
                    token1Price
                    liquidity
                    sqrtPrice
                    volumeUSD
                    totalValueLockedUSD
                }}
            }}
            """
            result = await self.query("get_pool_data", dex_name, query)
            data = (result or {}).get("data", {}).get("pool", {})
            if not data:
                log.debug(f"Pool data (v3) not found for {pool_address} on {dex_name}")
            return data
        else:
            query = f"""
            {{
              pair(id: "{pool_address}") {{
                id
                token0 {{ id symbol decimals derived{native} }}
                token1 {{ id symbol decimals derived{native} }}
                reserve0
                reserve1
                reserveUSD
                volumeUSD
                token0Price
                token1Price
              }}
            }}
            """
            result = await self.query("get_pool_data", dex_name, query)
            data = (result or {}).get("data", {}).get("pair", {})
            if not data:
                log.debug(f"Pair data (v2) not found for {pool_address} on {dex_name}")
            return data

    async def get_tokens_data(self, dex_name: str, token_addresses: list[str]) -> list[dict]:
        """Fetch metadata for specific tokens."""
        if not token_addresses:
            return []
            
        import json
        ids = [addr.lower() for addr in token_addresses]
        native = CHAIN_NATIVES.get(CHAIN_ID, "ETH")
        
        if "v3_ext" in dex_name.lower():
            query = f"""
            {{
                tokens(where: {{id_in: {json.dumps(ids)}}}) {{
                    id symbol decimals lastPriceUSD
                }}
            }}
            """
        else:
            query = f"""
            {{
                tokens(where: {{id_in: {json.dumps(ids)}}}) {{
                    id symbol decimals derived{native}
                }}
            }}
            """
            
        result = await self.query("get_tokens_data", dex_name, query)
        return (result or {}).get("data", {}).get("tokens", [])

    # async def get_swap_data(self, dex_name: str, tx_hash: str) -> Optional[dict]:
    #     """Fetch swap data for a specific transaction hash (Deprecated for Native RPC)"""
        if "v3_ext" in dex_name.lower():
            query = f"""
            {{
            swaps(where: {{hash: "{tx_hash}"}}) {{
                id
                amountIn
                amountOut
                tick
                tokenIn{{id decimals}}
                tokenOut{{id decimals}}
                pool {{id}}
                reserveAmounts
                timestamp
            }}
            }}
            """
        elif "v3" in dex_name.lower():
            query = f"""
            {{
            swaps(where: {{transaction: "{tx_hash}"}}) {{
                id
                amount0
                amount1
                token0{{id decimals}}
                token1{{id decimals}}
                pool{{id tick liquidity sqrtPrice}}
                timestamp
            }}
            }}
            """
        else:
            query = f"""
            {{
            swaps(where: {{transaction: "{tx_hash}"}}) {{
                id
                amount0In 
                amount1In
                amount0Out
                amount1Out
                pair {{id reserve0 reserve1 token0{{id decimals}} token1{{id decimals}}}}
                timestamp
            }}
            }}
            """
    #     result = await self.query("get_swap_data", dex_name, query)
    #     return (result or {}).get("data", {}).get("swaps", [])

    async def close(self):
        await self._client.aclose() 



async def main():
    client = GraphClient()
    
    # Example 1: Get top pools for Uniswap V3 (Extended)
    print("Fetching top pools for Uniswap V3 (Extended)...")
    pools = await client.get_top_pools("uniswap_v3", count=1)
    if pools:
        print(f"Found {len(pools)} pools.")
        for pool in pools:
            print(f"  Pool: {pool.get('id')}, TVL: {pool.get('totalValueLockedUSD')}")
    else:
        print("No pools found.")
        
    # Example 2: Get specific pool data for Uniswap V3
    print("\nFetching specific pool data for 0xc2e9f25be6257c210d7adf0d4cd6e3e881ba25f8...")
    pool_data = await client.get_pool_data("uniswap_v3", "0xc2e9f25be6257c210d7adf0d4cd6e3e881ba25f8")
    if pool_data:
        print(f"Pool data: {pool_data}")
    else:
        print("No pool data found.")
        
    # Example 3: Get swap data for a transaction hash
    # print("\nFetching swap data for transaction 0x...")
    # swap_data = await client.get_swap_data("uniswap_v3", "0x...")
    # if swap_data:
    #     print(f"Swap data: {swap_data}")
    # else:
    #     print("No swap data found.")
        
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())