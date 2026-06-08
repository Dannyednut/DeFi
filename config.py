"""
Enhanced Config Module
=====================
Multi-RPC support, comprehensive chain configurations, and improved settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

CHAIN_ID = int(os.getenv("CHAIN_ID", "1"))
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "demo")

def get_alchemy_network(chain_id: int) -> str:
    mapping = {
        1: "eth-mainnet",
        56: "bnb-mainnet",
        137: "polygon-mainnet",
        8453: "base-mainnet",
        42161: "arb-mainnet",
        324: "zksync-mainnet",
        11155111: "eth-sepolia",
    }
    return mapping.get(chain_id, "eth-mainnet")

def get_alchemy_ws_url(chain_id: int) -> str:
    if chain_id == CHAIN_ID and os.getenv("RPC_WS"):
        return os.getenv("RPC_WS")
    net = get_alchemy_network(chain_id)
    return f"wss://{net}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

def get_alchemy_http_url(chain_id: int) -> str:
    if chain_id == CHAIN_ID and os.getenv("RPC_HTTP"):
        return os.getenv("RPC_HTTP")
    net = get_alchemy_network(chain_id)
    return f"https://{net}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-RPC ARCHITECTURE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RPCConfig:
    """Configuration for a single RPC endpoint."""
    url: str
    name: str
    priority: int = 1  # Lower = higher priority
    max_rps: float = 10.0  # Max requests per second
    is_ws: bool = False
    chain_id: Optional[int] = None
    
    def __hash__(self):
        return hash(self.url)


# Default RPCs - can be overridden by environment
DEFAULT_RPCS: dict[int, list[RPCConfig]] = {
    1: [  # Ethereum
        RPCConfig(
            url=get_alchemy_ws_url(1),
            name="Alchemy WS",
            priority=8,
            is_ws=True,
            chain_id=1
        ),
        RPCConfig(
            url="wss://0xrpc.io/eth",
            name="0xRPC WS",
            priority=3,
            chain_id=1,
            is_ws=True
        ),
        RPCConfig(
            url=get_alchemy_http_url(1),
            name="Alchemy HTTP",
            priority=7,
            chain_id=1
        ),
        # RPCConfig(
        #     url="https://ethereum-rpc.publicnode.com",
        #     name="PublicNode",
        #     priority=5,
        #     chain_id=1,
        #     max_rps=5.0
        # ),
        RPCConfig(
            url="wss://ethereum-rpc.publicnode.com",
            name="PublicNode",
            priority=3,
            chain_id=1,
            is_ws=True,
        ),
        RPCConfig(
            url="wss://ethereum.drpc.org",
            name="0xRPC",
            priority=1,
            chain_id=1,
            is_ws=True,
        ),
        RPCConfig(
            url="https://0xrpc.io/eth",
            name="0xRPC",
            priority=3,
            chain_id=1,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://ethereum.drpc.org/",
            name="drpc",
            priority=4,
            chain_id=1,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://api.zan.top/node/v1/eth/mainnet/9eda158242244573a313a7308149a3c0",
            name="Logs",
            priority=2,
            chain_id=1,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://eth.api.pocket.network/",
            name="pocket",
            priority=6,
            chain_id=1,
            max_rps=5.0
        ),
    ],
    56: [  # BSC
        # RPCConfig(
        #     url="https://public-bsc-mainnet.fastnode.io/",
        #     name="Binance",
        #     priority=5,
        #     chain_id=56
        # ),
        # RPCConfig(
        #     url="https://bsc-dataseed1.binance.org",
        #     name="Binance",
        #     priority=7,
        #     chain_id=56
        # ),
        #  RPCConfig(
        #     url="https://bsc-dataseed2.binance.org",
        #    name="Binance Backup",
        #     priority=6,
        #     chain_id=56
        # ),
        RPCConfig(
            url=get_alchemy_http_url(56),
            name="Alchemy HTTP",
            priority=6,
            chain_id=56,
            max_rps=5
        ),
        RPCConfig(
            url="wss://bsc.drpc.org",
            name="dRPC WS",
            priority=2,
            chain_id=56,
            is_ws=True
        ),
        RPCConfig(
            url="wss://bsc.api.pocket.network/",
            name="pocket",
            priority=3,
            chain_id=56,
            is_ws=True
        ),
        RPCConfig(
            url="https://bsc.api.pocket.network/",
            name="pocket",
            priority=3,
            chain_id=56,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://bsc.drpc.org",
            name="pocket",
            priority=2,
            chain_id=56,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://api.zan.top/node/v1/bsc/mainnet/9eda158242244573a313a7308149a3c0",
            name="pocket",
            priority=1,
            chain_id=56,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://bnb.rpc.subquery.network/public",
            name="subquery",
            priority=8,
            chain_id=56,
            max_rps=5.0
        ),
    ],
    137: [  # Polygon
        RPCConfig(
            url="https://polygon-rpc.com",
            name="Polygon",
            priority=1,
            chain_id=137
        ),
    ],
    8453: [  # Base
        RPCConfig(
            url=get_alchemy_http_url(8453),
            name="Alchemy Base",
            priority=1,
            chain_id=8453
        ),
        RPCConfig(
            url="https://mainnet.base.org",
            name="Base Mainnet",
            priority=2,
            chain_id=8453
        ),
    ],
    42161: [  # Arbitrum
        RPCConfig(
            url="https://arb1.arbitrum.io/rpc",
            name="Arbitrum",
            priority=1,
            chain_id=42161
        ),
    ],
    324: [  # zkSync Era
        RPCConfig(
            url="https://mainnet.era.zksync.io",
            name="zkSync",
            priority=1,
            chain_id=324
        ),
    ],
    11155111: [  # Sepolia
        # RPCConfig(
        #     url=get_alchemy_ws_url(11155111),
        #     name="Alchemy WS",
        #     priority=7,
        #     is_ws=True,
        #     chain_id=11155111
        # ),
        RPCConfig(
            url=get_alchemy_http_url(11155111),
            name="Alchemy HTTP",
            priority=6,
            chain_id=11155111
        ),
        RPCConfig(
            url="https://ethereum-sepolia-rpc.publicnode.com",
            name="PublicNode",
            priority=5,
            chain_id=11155111,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://0xrpc.io/sep",
            name="0xRPC",
            priority=2,
            chain_id=11155111,
            max_rps=5.0
        ),
        RPCConfig(
            url="wss://0xrpc.io/sep",
            name="0xRPC",
            priority=1,
            chain_id=11155111,
            max_rps=5.0,
            is_ws=True
        ),
        RPCConfig(
            url="https://sepolia.drpc.org/",
            name="drpc",
            priority=3,
            chain_id=11155111,
            max_rps=5.0
        ),
        RPCConfig(
            url="https://ethereum-sepolia.rpc.subquery.network/public",
            name="subquery",
            priority=4,
            chain_id=11155111,
            max_rps=5.0
        ),
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED ABIs
# ══════════════════════════════════════════════════════════════════════════════

UNISWAP_V2_FACTORY_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "token0", "type": "address"},
        {"indexed": True, "name": "token1", "type": "address"},
        {"indexed": False, "name": "pair", "type": "address"},
        {"indexed": False, "name": "", "type": "uint256"},
    ], "name": "PairCreated", "type": "event"},
    {"inputs": [{"name": "", "type": "uint256"}], "name": "allPairs",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "allPairsLength",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
     "name": "getPair", "outputs": [{"name": "pair", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

UNISWAP_V2_PAIR_ABI = [
        {
            "constant": True,
            "inputs": [],
            "name": "getReserves",
            "outputs": [
                {"internalType": "uint112", "name": "_reserve0", "type": "uint112"},
                {"internalType": "uint112", "name": "_reserve1", "type": "uint112"},
                {"internalType": "uint32", "name": "_blockTimestampLast", "type": "uint32"}
            ],
            "payable": False,
            "stateMutability": "view",
            "type": "function"
        },
        {
            "constant": True,
            "inputs": [],
            "name": "token0",
            "outputs": [{"internalType": "address", "name": "", "type": "address"}],
            "payable": False,
            "stateMutability": "view",
            "type": "function"
        },
        {
            "constant": True,
            "inputs": [],
            "name": "token1",
            "outputs": [{"internalType": "address", "name": "", "type": "address"}],
            "payable": False,
            "stateMutability": "view",
            "type": "function"
        }
    ]

UNISWAP_V3_FACTORY_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "token0", "type": "address"},
        {"indexed": True, "name": "token1", "type": "address"},
        {"indexed": True, "name": "fee", "type": "uint24"},
        {"indexed": False, "name": "tickSpacing", "type": "int24"},
        {"indexed": False, "name": "pool", "type": "address"},
    ], "name": "PoolCreated", "type": "event"},
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}, {"name": "fee", "type": "uint24"}],
     "name": "getPool", "outputs": [{"name": "pool", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

UNISWAP_V3_POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee", "outputs": [{"type": "uint24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "", "type": "uint16"},
        {"name": "", "type": "uint16"},
        {"name": "", "type": "uint16"},
        {"name": "", "type": "uint8"},
        {"name": "", "type": "bool"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function"},
    # Added for enhanced V3 analysis
    {"inputs": [{"name": "owner", "type": "address"}], "name": "positions", "outputs": [{"type": "uint128"}], 
     "stateMutability": "view", "type": "function"},
]

PANCAKESWAP_V3_POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee", "outputs": [{"type": "uint24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "", "type": "uint16"},
        {"name": "", "type": "uint16"},
        {"name": "", "type": "uint16"},
        {"name": "", "type": "uint32"},
        {"name": "", "type": "bool"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function"},
    # Added for enhanced V3 analysis
    {"inputs": [{"name": "owner", "type": "address"}], "name": "positions", "outputs": [{"type": "uint128"}], 
     "stateMutability": "view", "type": "function"},
]

ERC20_ABI = [
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], 
     "stateMutability": "view", "type": "function"},
]

AAVE_POOL_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}],
     "name": "getUserAccountData",
     "outputs": [
         {"name": "totalCollateralBase", "type": "uint256"},
         {"name": "totalDebtBase", "type": "uint256"},
         {"name": "availableBorrowsBase", "type": "uint256"},
         {"name": "currentLiquidationThreshold", "type": "uint256"},
         {"name": "ltv", "type": "uint256"},
         {"name": "healthFactor", "type": "uint256"},
     ], "stateMutability": "view", "type": "function"},
]

# Uniswap V3 Non-Fungible Position Manager for positions
UNISWAP_V3_POSITION_ABI = [
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "positions", 
     "outputs": [
         {"name": "nonce", "type": "uint256"},
         {"name": "operator", "type": "address"},
         {"name": "token0", "type": "address"},
         {"name": "token1", "type": "address"},
         {"name": "fee", "type": "uint24"},
         {"name": "tickLower", "type": "int24"},
         {"name": "tickUpper", "type": "int24"},
         {"name": "liquidity", "type": "uint128"},
         {"name": "feeGrowthInside0LastX128", "type": "uint256"},
         {"name": "feeGrowthInside1LastX128", "type": "uint256"},
         {"name": "tokensOwed0", "type": "uint128"},
         {"name": "tokensOwed1", "type": "uint128"},
     ], "stateMutability": "view", "type": "function"},
]

ARB_EXEC_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "amountIn", "type": "uint256"},
                {"name": "minProfit", "type": "uint256"},
                {"name": "tokens", "type": "address[]"},
                {"name": "pools", "type": "address[]"},
                {"name": "fees", "type": "uint24[]"},
                {"name": "tokenIn", "type": "address"},
                {"name": "mode", "type": "uint8"},
            ],
            "internalType": "struct ArbData", "name": "arb", "type": "tuple"
        }],
        "name": "yieldOut",
        "outputs": [
            {"components": [
                {"name": "amountIn", "type": "uint256"},
                {"name": "minProfit", "type": "uint256"},
                {"name": "tokens", "type": "address[]"},
                {"name": "pools", "type": "address[]"},
                {"name": "fees", "type": "uint24[]"},
                {"name": "tokenIn", "type": "address"},
                {"name": "mode", "type": "uint8"},
            ], "internalType": "uint256", "name": "ad", "type": "tuple"},
            {"name": "profit", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
      "inputs": [{
          "components": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "minProfit", "type": "uint256"},
            {"name": "tokens", "type": "address[]"},
            {"name": "pools", "type": "address[]"},
            {"name": "fees", "type": "uint24[]"},
            {"name": "tokenIn", "type": "address"},
            {"name": "mode", "type": "uint8"},
          ],
          "internalType": "struct ArbData",
          "name": "arb",
          "type": "tuple"
        },
        {"internalType": "bool","name": "forceBalancer", "type": "bool"},
        {"internalType": "uint256","name": "validUntilBlock", "type": "uint256"}
      ],
      "name": "swap",
      "outputs": [],
      "stateMutability": "nonpayable",
      "type": "function"
    },
    {
		"inputs": [
			{"internalType": "bytes[]", "name": "calls", "type": "bytes[]"}
		],
		"name": "multiCall",
		"outputs": [
			{"internalType": "uint256", "name": "successful", "type": "uint256"},
			{"internalType": "uint256", "name": "failed", "type": "uint256"}
		],
		"stateMutability": "nonpayable",
		"type": "function"
	},
    {
		"anonymous": False,
		"inputs": [
			{"indexed": False, "internalType": "uint256", "name": "successful", "type": "uint256"},
			{"indexed": False, "internalType": "uint256", "name": "failed", "type": "uint256"}
		],
		"name": "BATCH",
		"type": "event"
	},
	{
		"anonymous": False,
		"inputs": [
			{"indexed": True, "internalType": "address", "name": "token", "type": "address"},
			{"indexed": False, "internalType": "uint256", "name": "amt", "type": "uint256"}
		],
		"name": "DONE",
		"type": "event"
	},
]


# ══════════════════════════════════════════════════════════════════════════════
# DEX CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DexConfig:
    name: str
    factory: str
    router: str  # NEW: router address for mempool monitoring
    version: int  # 2 or 3
    fee_bps: int = 30
    deploy_block: int = 0
    position_manager: str = ""  # V3 position manager for liquidity data


@dataclass
class ChainConfig:
    chain_id: int
    name: str
    native_symbol: str
    wrapped_native: str
    dexes: list[DexConfig] = field(default_factory=list)
    aave_pool: Optional[str] = None
    quoter_v2: Optional[str] = None
    quoter_v3: Optional[str] = None
    arb_exec_address: Optional[str] = None
    stablecoins: list[str] = field(default_factory=list)
    seed_pools: list[dict] = field(default_factory=list)
    # NEW: Additional data sources
    subgraph_url: Optional[str] = None
    subgraph_providers: dict[str, list[dict]] = field(default_factory=dict)
    coingecko_id: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# COMPREHENSIVE CHAIN CONFIGURATIONS
# ══════════════════════════════════════════════════════════════════════════════

CHAINS: dict[int, ChainConfig] = {
    # ═══════════════════════════════════════════════════════════════════════
    # ETHEREUM
    # ═══════════════════════════════════════════════════════════════════════
    1: ChainConfig(
        chain_id=1,
        name="ethereum",
        native_symbol="ETH",
        wrapped_native="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        quoter_v2="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        quoter_v3="0x5e55c9e631fae526cd4b0526c4818d6e0a9ef0e3",
        aave_pool="0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
        arb_exec_address=os.getenv(f"ARB_EXEC_ADDRESS_{CHAIN_ID}", "").strip() or None,
        subgraph_url="https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3",
        coingecko_id="ethereum",
        stablecoins=[
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
        ],
        dexes=[
            DexConfig("UniswapV2", "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", 
                     "0x7a250d5630B4CF539739DF2C5dAcb4c659F2488d", 2, 30),
            DexConfig("SushiswapV2", "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",
                     "0xd9e1cE17f2641f24ae83637ab66a2cca9c378B9f", 2, 25),
            DexConfig("SushiswapV3", "0xbACEB8eC6b9355Dfc0269C18bac9d6E2Bdc29C4F",
                     "0xd9e1cE17f2641f24ae83637ab66a2cca9c378B9f", 3, 30),
            DexConfig("UniswapV3", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                     "0xE592427A0AEce92De3EdEE1F18E0157C05861564", 3, 30, 
                     deploy_block=12369621,
                     position_manager="0xC36442b4a4522E871399CD717aBDD847Ab11Fe88"),
            DexConfig("PancakeV2", "0x1097053Fd2ea711dad45caCcc45EfF7548fCB362",
                     "0xEfF92A263d31888d860bD50809A8D171709b7b1c", 2, 30,),
            DexConfig("PancakeV3", "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
                     "0x1fcCCf7c937e65fa3D7A73E8bAe0F5b8e4F77baa", 3, 30,
                     deploy_block=17356890),
        ],
        seed_pools=[
            {"addr": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", "token0": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 
             "token1": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "dex": "UniswapV3", "version": 3, "fee_bps": 5},
            {"addr": "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8", "token0": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
             "token1": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "dex": "UniswapV3", "version": 3, "fee_bps": 30},
            {"addr": "0x4e68ccd3e89f51c3074ca5072bbac773960dfa36", "token0": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
             "token1": "0xdac17f958d2ee523a2206206994597c13d831ec7", "dex": "UniswapV3", "version": 3, "fee_bps": 5},
            {"addr": "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc", "token0": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
             "token1": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "dex": "UniswapV2", "version": 2, "fee_bps": 30},
            {"addr": "0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852", "token0": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
             "token1": "0xdac17f958d2ee523a2206206994597c13d831ec7", "dex": "UniswapV2", "version": 2, "fee_bps": 30},
        ],
        subgraph_providers={
            "uniswap_v3_ext": [
                {"type": "graph", "subgraph_id": "4cKy6QQMc5tpfdx8yxfYeb9TLZmgLQe44ddW1G7NwkA6", "deployment_id": "Qmc9TiHtLDgsbgqvyfXKiyndZDnjWdrfdvETgarZbg3StY"},
                {"type": "goldsky", "endpoint": "https://api.goldsky.com/api/public/project_cmmu3ggpve9uj01w1geck5zk3/subgraphs/uniswap-v3-ethereum/4.0.0/gn"}
            ],
            "uniswapv3": [
                {"type": "graph", "subgraph_id": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV", "deployment_id": "QmTZ8ejXJxRo7vDBS4uwqBeGoxLSWbhaA7oXa1RvxunLy7"},
                {"type": "graph", "subgraph_id": "8e4dRt4P4WHXnKbEq7STaQfU2g99WZ5S4w39f2PcUTjD", "deployment_id": "QmXDAaE7sT2bVe4prmZgdSXi34EGRjpULTnF9bKi3qrwFB"},
                {"type": "graph", "subgraph_id": "9fWsevEC9Yz4WdW9QyUvu2JXsxyXAxc1X4HaEkmyyc75", "deployment_id": "QmXDAaE7sT2bVe4prmZgdSXi34EGRjpULTnF9bKi3qrwFB"},
                {"type": "goldsky", "endpoint": "https://api.goldsky.com/api/public/project_cmmu3ggpve9uj01w1geck5zk3/subgraphs/uniswap-v3/1.0.0/gn"}
            ],
            "uniswapv2": [
                {"type": "graph", "subgraph_id": "A3Np3RQbaBA6oKJgiwDJeo5T3zrYfGHPWFYayMwtNDum", "deployment_id": "QmZzsQGDmQFbzYkv2qx4pVnD6aVnuhKbD3t1ea7SAvV7zE"},
                {"type": "graph", "subgraph_id": "EYCKATKGBKLWvSfwvBjzfCBmGwYNdVkduYXVivCsLRFu", "deployment_id": "QmZzsQGDmQFbzYkv2qx4pVnD6aVnuhKbD3t1ea7SAvV7zE"},
                {"type": "graph", "subgraph_id": "GmSczqdCDZ3hJeYY9JphwsADn5rePUzUKm8EZcVuhRAm", "deployment_id": "QmZk7ThfQVkwhwckCPtqXmxC8SRD99gLeF9pxfYfEiVwV1"},
                {"type": "goldsky", "endpoint": "https://api.goldsky.com/api/public/project_cmmu3ggpve9uj01w1geck5zk3/subgraphs/uniswap-v2/2.0.0/gn"}
            ],
            "sushiswapv2": [
                {"type": "graph", "subgraph_id": "GyZ9MgVQkTWuXGMSd3LXESvpevE8S8aD3uktJh7kbVmc", "deployment_id": "QmaR2nAMF6dCHBL1eFNQ4F5nGpJQs7V11PZobJB2FgQtbt"}
            ]
        },
    ),
    
    # ═══════════════════════════════════════════════════════════════════════
    # BSC
    # ═══════════════════════════════════════════════════════════════════════
    56: ChainConfig(
        chain_id=56,
        name="bsc",
        native_symbol="BNB",
        wrapped_native="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        quoter_v3="0x5e55c9e631fae526cd4b0526c4818d6e0a9ef0e3",
        aave_pool=None,
        arb_exec_address=os.getenv(f"ARB_EXEC_ADDRESS_{CHAIN_ID}", "").strip() or None,
        coingecko_id="binancecoin",
        subgraph_url="https://api.thegraph.com/subgraphs/name/pancakeswap/pancake-v3-bsc",
        stablecoins=[
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",  # USDC
            "0x55d398326f99059fF775485246999027B3197955",  # USDT
        ],
        dexes=[
            DexConfig("PancakeV2", "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
                     "0x10ed43c718714eb63d5aa57b78b54704e256024e", 2, 25),
            DexConfig("BiswapV2", "0x858E3312ed3A876947EA49d572A7C42DE08af7EE",
                     "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8", 2, 10),
            DexConfig("UniswapV3", "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
                     "0x1F98431c8aD98523631AE4a59f267346ea31F984", 3, 30,
                     deploy_block=26324014),
            DexConfig("PancakeV3", "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
                     "0x13f4ea83d0bd40e75c8222255bc855a974568dd4", 3, 30,
                     deploy_block=26956207),
        ],
        seed_pools=[
            {"addr": "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae", "token0": "0x55d398326f99059ff775485246999027b3197955",
             "token1": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "dex": "PancakeV2", "version": 2, "fee_bps": 25},
            {"addr": "0x58f876857a02d6762e0101bb5c46a8c1ed44dc16", "token0": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
             "token1": "0xe9e7cea3dedca5984780bafc599bd69add087d56", "dex": "PancakeV2", "version": 2, "fee_bps": 25},
        ],
        subgraph_providers={
            "pancakev2": [
                {"type": "nodereal", "endpoint": "https://open-platform.nodereal.io/fb1c559400a74d2fb71f9b6ba325706a/pancakeswap-free/graphql"}
            ]
        }
    ),
    
    # ═══════════════════════════════════════════════════════════════════════
    # POLYGON
    # ═══════════════════════════════════════════════════════════════════════
    137: ChainConfig(
        chain_id=137,
        name="polygon",
        native_symbol="MATIC",
        wrapped_native="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        aave_pool="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        coingecko_id="matic-network",
        subgraph_url="https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3-polygon",
        stablecoins=[
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC
            "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",  # USDT
        ],
        dexes=[
            DexConfig("QuickswapV2", "0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32",
                     "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff", 2, 30),
            DexConfig("SushiswapV2", "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
                     "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506", 2, 25),
            DexConfig("UniswapV3", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                     "0xE592427A0AEce92De3EdEE1F18E0157C05861564", 3, 30,
                     deploy_block=22757547),
        ],
        seed_pools=[
            {"addr": "0x45dda9cb7c25131df268515131f647d726f50608", "token0": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
             "token1": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", "dex": "UniswapV3", "version": 3, "fee_bps": 5},
        ],
    ),
    
    # ═══════════════════════════════════════════════════════════════════════
    # BASE
    # ═══════════════════════════════════════════════════════════════════════
    8453: ChainConfig(
        chain_id=8453,
        name="base",
        native_symbol="ETH",
        wrapped_native="0x4200000000000000000000000000000000000006",
        aave_pool="0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
        coingecko_id="ethereum",
        subgraph_url="https://api.thegraph.com/subgraphs-name/base-org/base-uniswap-v3",
        stablecoins=[
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
            "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",  # DAI
        ],
        dexes=[
            DexConfig("BaseswapV2", "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB",
                     "0x327df1e6de05895d2ab08513aadd9313fe505d86", 2, 30),
            DexConfig("UniswapV3", "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
                     "0x2626664c2603336e57b271c5c0b26f421741e481", 3, 30,
                     deploy_block=1371680),
            DexConfig("AerodromeV2", "0x420DD381b31aEf6683db6B902084cB0FFECe40D",
                     "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43", 2, 20),
        ],
        seed_pools=[
            {"addr": "0xd0b53d9277642d899df5c87a3966a349a798f224", "token0": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
             "token1": "0x4200000000000000000000000000000000000006", "dex": "UniswapV3", "version": 3, "fee_bps": 5},
            {"addr": "0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59", "token0": "0x4200000000000000000000000000000000000006",
             "token1": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "dex": "AerodromeV2", "version": 2, "fee_bps": 5},
        ],
    ),
    
    # ═══════════════════════════════════════════════════════════════════════
    # ARBITRUM
    # ═══════════════════════════════════════════════════════════════════════
    42161: ChainConfig(
        chain_id=42161,
        name="arbitrum",
        native_symbol="ETH",
        wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        aave_pool="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        coingecko_id="ethereum",
        subgraph_url="https://api.thegraph.com/subgraphs-name/uniswap/uniswap-v3-arbitrum-one",
        stablecoins=[
            "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",  # USDC.e
            "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT
        ],
        dexes=[
            DexConfig("SushiswapV2", "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
                     "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506", 2, 25),
            DexConfig("UniswapV3", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                     "0xE592427A0AEce92De3EdEE1F18E0157C05861564", 3, 30,
                     deploy_block=165),
            DexConfig("CamelotV2", "0x6EcCab422D763aC031210895C81787E87B43A652",
                     "0xc873fecbd354f5a56e00e710b90ef4201db2448d", 2, 30),
        ],
        seed_pools=[
            {"addr": "0xc6962004f452be9203591991d15f6b388e09e8d0", "token0": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
             "token1": "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8", "dex": "UniswapV3", "version": 3, "fee_bps": 5},
        ],
    ),
    
    # ═══════════════════════════════════════════════════════════════════════
    # ZKSYNC ERA
    # ═══════════════════════════════════════════════════════════════════════
    324: ChainConfig(
        chain_id=324,
        name="zksync",
        native_symbol="ETH",
        wrapped_native="0x5AEa5775959fBC2557Cc8789bC1bf90A239D9a91",
        aave_pool=None,
        coingecko_id="ethereum",
        stablecoins=[
            "0x3355df6D4c9C3035724Fd0e3914dE96A5a83aaf4",  # USDC
        ],
        dexes=[
            DexConfig("MuteV2", "0x40be1cBa6C5B47cDF9da7f963B6F761F4C60627",
                     "0x8b791913eb07c32779a16750e3868aa8495f5964", 2, 30),
            DexConfig("UniswapV3", "0x8FdA5a7a8dCA67BBcDd10F02Fa0649A937215422",
                     "0x5c60faba4a6b5a5c8a444a4b7e30c3c8d3ad54c", 3, 30,
                     deploy_block=3704075),
            DexConfig("SyncswapV2", "0xf2DAd89f2788a8CD54625C60b55cD3d2D0ACa7Cb",
                     "0x9b5def958d0f3b6955cbea4d5b7809b2fb26b059", 2, 10),
        ],
        seed_pools=[
            {"addr": "0x80115c708e12edd42e504c1cd52aea96c547c05c", "token0": "0x3355df6d4c9c3035724fd0e3914de96a5a83aaf4",
             "token1": "0x5aea5775959fbc2557cc8789bc1bf90a239d9a91", "dex": "SyncswapV2", "version": 2, "fee_bps": 10},
        ],
    ),
    
    # ═══════════════════════════════════════════════════════════════════════
    # SEPOLIA
    # ═══════════════════════════════════════════════════════════════════════
    11155111: ChainConfig(
        chain_id=11155111,
        name="sepolia",
        native_symbol="ETH",
        wrapped_native="0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
        quoter_v2="0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3",
        quoter_v3="0x5523d3cff6ad511858481448b714bec865db9c59",
        aave_pool="0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951",
        arb_exec_address=os.getenv(f"ARB_EXEC_ADDRESS_{CHAIN_ID}", "").strip() or None,
        subgraph_url="https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3",
        coingecko_id="ethereum",
        stablecoins=[
            "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
            "0x7169D38820dfd117C3FA1f22a697dBA58d90BA06"
            "0x3e622317f8C93f7328350cF0B56d9eD4C620C5d6"
        ],
        dexes=[
            DexConfig("UniswapV2", "0xF62c03E08ada871A0bEb309762E260a7a6a880E6", 
                     "0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3", 2, 30),
            DexConfig("UniswapV3", "0x0227628f3F023bb0B980b67D528571c95c6DaC1c",
                     "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E", 3, 30, 
                     deploy_block=12369621,
                     position_manager="0x1238536071E1c677A632429e3655c799b22cDA52"),
            DexConfig("SushiswapV2", "0x734583f62Bb6ACe3c9bA9bd5A53143CA2Ce8C55A",
                      "0xeaBcE3E74EF41FB40024a21Cc2ee2F5dDc615791", 2, 25),
        ],
        seed_pools=[],
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# ARB_EXEC_ADDRESS = os.getenv("ARB_EXEC_ADDRESS", "").strip() or None
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip() or None
EXECUTE_ONCHAIN = os.getenv("EXECUTE_ONCHAIN", "false").lower() == "true"

# Enhanced settings
MIN_PROFIT_USD = float(os.getenv("MIN_PROFIT_USD", "1.0"))
REPORT_INTERVAL = int(os.getenv("REPORT_INTERVAL_BLOCKS", "50"))
MAX_HOPS = int(os.getenv("MAX_HOPS", "4"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_POOL_LIQUIDITY_USD", "10000"))  # Alias for compatibility
MIN_POOL_LIQUIDITY_USD = float(os.getenv("MIN_POOL_LIQUIDITY_USD", "10000"))
NATIVE_PRICE_USD = float(os.getenv("NATIVE_TOKEN_PRICE_USD", "3000.0"))
LOG_DIR = os.getenv("LOG_DIR", "./logs")

# NEW: Enhanced configuration
CACHE_DIR = os.getenv("CACHE_DIR", "./cache")
MAX_PENDING_BLOCKS = int(os.getenv("MAX_PENDING_BLOCKS", "20"))

# Rate limiting
MAX_RPC_REQUESTS_PER_SECOND = float(os.getenv("MAX_RPC_RPS", "10.0"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "50"))

# Mempool settings
MEMPOOL_MAX_FETCHES_PER_SECOND = float(os.getenv("MEMPOOL_MAX_FPS", "5.0"))
MEMPOOL_QUEUE_SIZE = int(os.getenv("MEMPOOL_QUEUE_SIZE", "500"))

# Price oracle settings
PRICE_CACHE_TTL = float(os.getenv("PRICE_CACHE_TTL", "60.0"))
PRICE_UPDATE_INTERVAL = float(os.getenv("PRICE_UPDATE_INTERVAL", "300.0"))

# Crawler settings
CRAWLER_V3_CHUNK = int(os.getenv("CRAWLER_V3_CHUNK", "10"))
CRAWLER_V2_BATCH = int(os.getenv("CRAWLER_V2_BATCH", "20"))
CRAWLER_TICK_SLEEP = float(os.getenv("CRAWLER_TICK_SLEEP", "0.5"))
STALE_REFRESH_INTERVAL = int(os.getenv("STALE_REFRESH_INTERVAL", "3600"))

# Gas estimation
DEFAULT_GAS_UNITS = int(os.getenv("DEFAULT_GAS_UNITS", "350000"))
GAS_MULTIPLIER = float(os.getenv("GAS_MULTIPLIER", "1.2"))

FLASHBOTS_RPCS: dict[int, str] = {
    1:    "https://relay.flashbots.net",
    8453: "https://relay.flashbots.net",  # Base uses same Flashbots relay
    11155111: "https://relay-sepolia.flashbots.net",
}

def get_ws_url(chain_id: int):
    if chain_id in DEFAULT_RPCS:
        rpcs = [rpc for rpc in DEFAULT_RPCS[chain_id] if rpc.is_ws]
        # Sort by priority and return the highest one
        if rpcs:
            rpcs.sort(key=lambda x: x.priority)
            return rpcs[0].url
        else:
            get_alchemy_ws_url(chain_id)
    else:
        return get_alchemy_ws_url(chain_id)
    
def get_http_url(chain_id: int):
    if chain_id in DEFAULT_RPCS:
        rpcs = [rpc for rpc in DEFAULT_RPCS[chain_id] if not rpc.is_ws]
        # Sort by priority and return the highest one
        if rpcs:
            rpcs.sort(key=lambda x: x.priority)
            return rpcs[0].url
        else:
            get_alchemy_http_url(chain_id)
    else:
        return get_alchemy_http_url(chain_id)
    
# Export correctly constructed endpoints for current chain
RPC_WS = get_ws_url(CHAIN_ID)
RPC_HTTP = get_http_url(CHAIN_ID)

def get_chain() -> ChainConfig:
    """Get chain configuration for the current chain ID."""
    if CHAIN_ID not in CHAINS:
        raise ValueError(f"Chain {CHAIN_ID} not configured. Add it to config.py.")
    return CHAINS[CHAIN_ID]


def get_rpc_configs() -> list[RPCConfig]:
    """Get RPC configurations for the current chain."""
    return DEFAULT_RPCS.get(CHAIN_ID, [])

def get_log_rpc_config() -> RPCConfig:
    rpcs = DEFAULT_RPCS.get(CHAIN_ID, [])
    if rpcs:
        for rpc in rpcs:
            if "logs" in rpc.name.lower():
                return rpc
    return None

def get_primary_rpc() -> str:
    """Get the primary RPC URL (highest priority, HTTP)."""
    configs = get_rpc_configs()
    if not configs:
        raise ValueError(f"No RPC configured for chain {CHAIN_ID}")
    
    # Prefer HTTP for reliability, fallback to any available
    http_configs = [c for c in configs if not c.is_ws]
    if http_configs:
        return sorted(http_configs, key=lambda x: x.priority)[0].url
    
    return sorted(configs, key=lambda x: x.priority)[0].url


def get_ws_rpc() -> Optional[str]:
    """Get WebSocket RPC URL if available."""
    configs = get_rpc_configs()
    ws_configs = [c for c in configs if c.is_ws]
    if ws_configs:
        return sorted(ws_configs, key=lambda x: x.priority)[0].url
    return None

def get_subgraph_providers(chain_id: int, dex_name: str = None) -> list[dict]:
    """Get subgraph providers for a DEX name."""
    chain = CHAINS.get(chain_id)
    if not chain: return []
    if not dex_name: return chain.subgraph_providers
    for dex in chain.subgraph_providers.keys():
        if dex == dex_name:
            return chain.subgraph_providers[dex]
    return []

def get_factory_by_name(chain_id: int, name: str) -> Optional[str]:
    """Get factory address for a DEX name."""
    chain = CHAINS.get(chain_id)
    if not chain: return None
    for dex in chain.dexes:
        if dex.name == name:
            return dex.factory.lower()
    return None
