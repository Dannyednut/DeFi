"""
Enhanced Price Oracle
===================
Multi-source price aggregation:
1. CoinGecko (current - slow but comprehensive)
2. DEX price aggregation (fast, accurate)
3. CEX feeds (Binance API)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional
from dataclasses import dataclass

import aiohttp

from log import get_logger
log = get_logger("prices")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

_PLATFORM_IDS: dict[int, str] = {
    1: "ethereum",
    56: "binance-smart-chain",
    137: "polygon-pos",
    8453: "base",
    42161: "arbitrum-one",
    324: "zksync",
}

_NATIVE_COINGECKO_IDS: dict[int, str] = {
    1: "ethereum",
    56: "binancecoin",
    137: "matic-network",
    8453: "ethereum",
    42161: "ethereum",
    324: "ethereum",
}

_NATIVE_BINANCE_IDS: dict[int, str] = {
    1: "ethusdt",
    56: "bnbusdt",
    137: "maticusdt",
    8453: "ethusdt",
    42161: "ethusdt",
    324: "ethusdt",
}

_BASE_URL_CG = "https://api.coingecko.com/api/v3"
_BASE_URL_BINANCE = "https://api.binance.com/api/v3"

_BATCH_SIZE = 250
_CACHE_TTL = 60.0
_MIN_CALL_GAP_CG = 2.1  # CoinGecko: 30 calls/min
_MIN_CALL_GAP_BINANCE = 0.1  # Binance: generous limits
_BACKOFF_CAP = 60.0


# ══════════════════════════════════════════════════════════════════════════════
# PRICE DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PriceSource:
    """Represents a price source with weight and freshness."""
    name: str
    price: Optional[float]
    timestamp: float
    weight: float  # Higher = more trusted
    latency_ms: float


@dataclass
class AggregatedPrice:
    """Aggregated price from multiple sources."""
    token: str
    price_usd: float
    sources: list[PriceSource]
    confidence: float  # 0-1 based on source agreement
    
    @property
    def is_fresh(self) -> bool:
        """Check if any source data is fresh."""
        return time.time() - max(s.timestamp for s in self.sources) < _CACHE_TTL


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED PRICE ORACLE
# ══════════════════════════════════════════════════════════════════════════════

class PriceOracle:
    """
    Multi-source async price oracle.
    
    Sources (in priority order):
    1. Binance - fastest, most reliable for major tokens
    2. DEX aggregation - on-chain, accurate
    3. CoinGecko - comprehensive but slower
    """
    
    def __init__(
        self,
        chain_id: int,
        native_price_usd: float,
        wrapped_native: str,
        stablecoins: list[str],
    ):
        self._chain_id = chain_id
        self._platform = _PLATFORM_IDS.get(chain_id, "ethereum")
        self._native_usd = native_price_usd
        
        # Price cache: addr -> (price, timestamp, sources_dict)
        # sources_dict: {source_name: (price, timestamp)}
        self._cache: dict[str, tuple[float, float, dict]] = {}
        
        # Seed with known prices
        wn = wrapped_native.lower()
        self._cache[wn] = (native_price_usd, time.time(), {"config": (native_price_usd, time.time())})
        self._wrapped_native = wn
        
        for addr in stablecoins:
            a = addr.lower()
            self._cache[a] = (1.0, time.time(), {"config": (1.0, time.time())})
        
        self._stablecoins = {a.lower() for a in stablecoins}
        
        # Rate limiting
        self._last_call_cg = 0.0
        self._last_call_binance = 0.0
        self._backoff_cg = 0.0
        self._backoff_binance = 0.0
        
        # Stats
        self._calls_cg = 0
        self._calls_binance = 0
        self._errors_cg = 0
        self._errors_binance = 0
        
        # Background task
        self._refresh_task: Optional[asyncio.Task] = None
        self._pending_addrs: set[str] = set()
        
        # Fetch native price on init
        self._fetch_native_price_sync()
    
    # ══════════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════════════════
    
    def get_price(self, addr: str) -> Optional[float]:
        """Get cached USD price for a token."""
        entry = self._cache.get(addr.lower())
        if entry is None:
            self._pending_addrs.add(addr.lower())
            return None
        
        price, fetched_at, _ = entry
        if time.time() - fetched_at > _CACHE_TTL:
            self._pending_addrs.add(addr.lower())
        
        return price

    def set_dex_price(self, addr: str, price: float) -> None:
        """Manually set a price derived from DEX subgraphs."""
        self._update_price(addr, price, "dex", time.time())
    
    def get_price_with_sources(self, addr: str) -> Optional[AggregatedPrice]:
        """Get price with source information."""
        entry = self._cache.get(addr.lower())
        if entry is None:
            return None
        
        price, _, sources_dict = entry
        
        sources = [
            PriceSource(name=name, price=p, timestamp=t, weight=self._get_source_weight(name), latency_ms=0)
            for name, (p, t) in sources_dict.items()
        ]
        
        # Calculate confidence based on source agreement
        prices = [s.price for s in sources if s.price]
        if len(prices) > 1:
            mean_price = sum(prices) / len(prices)
            max_deviation = max(abs(p - mean_price) / mean_price for p in prices) if mean_price > 0 else 0
            confidence = max(0, 1 - max_deviation * 10)  # 10% deviation = 0 confidence
        else:
            confidence = 0.5
        
        return AggregatedPrice(
            token=addr.lower(),
            price_usd=price,
            sources=sources,
            confidence=confidence,
        )
    
    def _get_source_weight(self, source: str) -> float:
        """Get weight for a price source."""
        weights = {
            "binance": 0.5,
            "dex": 0.3,
            "coingecko": 0.15,
            "config": 0.05,
        }
        return weights.get(source, 0.1)
    
    def get_native_price(self) -> float:
        entry = self._cache.get(self._wrapped_native)
        return entry[0] if entry else self._native_usd
    
    # ══════════════════════════════════════════════════════════════════════════════
    # BACKGROUND REFRESH
    # ══════════════════════════════════════════════════════════════════════════════
    
    def start_background_refresh(
        self,
        graph,
        interval_seconds: float = 300.0,
    ) -> asyncio.Task:
        """Launch background price refresh task."""
        async def _loop():
            first = True
            while True:
                try:
                    tokens = list(graph.tokens)
                    if tokens:
                        await self.refresh_all(tokens)
                    await self._refresh_native_price()
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    log.warning(f"Background refresh error: {e}")
                
                if first:
                    first = False
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(interval_seconds)
        
        self._refresh_task = asyncio.create_task(_loop(), name="price_oracle_refresh")
        return self._refresh_task
    
    async def refresh_all(self, addresses: list[str]) -> None:
        """Refresh prices for all addresses using multiple sources."""
        unique = list({a.lower() for a in addresses})
        if not unique:
            return
        
        # Fetch from all sources in parallel
        tasks = [
            self._refresh_from_binance(unique),
            self._refresh_from_coingecko(unique),
        ]
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def refresh_pending(self) -> None:
        """Refresh all pending addresses."""
        if not self._pending_addrs:
            return
        batch = list(self._pending_addrs)
        self._pending_addrs.clear()
        await self.refresh_all(batch)
    
    # ══════════════════════════════════════════════════════════════════════════════
    # COINGECKO SOURCE
    # ══════════════════════════════════════════════════════════════════════════════
    
    async def _refresh_from_coingecko(self, addresses: list[str]) -> None:
        """Fetch prices from CoinGecko."""
        await self._wait_rate_limit_cg()
        
        url = f"{_BASE_URL_CG}/simple/token_price/{self._platform}"
        params = {
            "contract_addresses": ",".join(addresses),
            "vs_currencies": "usd",
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    self._calls_cg += 1
                    self._last_call_cg = time.time()
                    
                    if resp.status == 429:
                        self._backoff_cg = min((self._backoff_cg or 4) * 2, _BACKOFF_CAP)
                        log.warning(f"CG rate limited, backing off {self._backoff_cg:.0f}s")
                        return
                    
                    if resp.status != 200:
                        self._errors_cg += 1
                        return
                    
                    self._backoff_cg = 0
                    data = await resp.json()
                    
                    now = time.time()
                    for addr in addresses:
                        entry = data.get(addr.lower())
                        if entry and "usd" in entry:
                            price = float(entry["usd"])
                            if price > 0:
                                self._update_price(addr, price, "coingecko", now)
        
        except Exception as e:
            log.debug(f"CoinGecko error: {e}")
            self._errors_cg += 1
    
    async def _wait_rate_limit_cg(self) -> None:
        """Wait for CoinGecko rate limit."""
        if self._backoff_cg > 0:
            await asyncio.sleep(self._backoff_cg)
        
        elapsed = time.time() - self._last_call_cg
        if elapsed < _MIN_CALL_GAP_CG:
            await asyncio.sleep(_MIN_CALL_GAP_CG - elapsed)
    
    async def _refresh_native_price_coingecko(self) -> None:
        """Fetch native token price from CoinGecko."""
        await self._wait_rate_limit_cg()
        
        coin_id = _NATIVE_COINGECKO_IDS.get(self._chain_id)
        if not coin_id:
            return
        
        url = f"{_BASE_URL_CG}/simple/price"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params={"ids": coin_id, "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    self._calls_cg += 1
                    self._last_call_cg = time.time()
                    
                    if resp.status == 200:
                        data = await resp.json()
                        price = data.get(coin_id, {}).get("usd")
                        if price:
                            self._native_usd = float(price)
                            now = time.time()
                            self._update_price(self._wrapped_native, float(price), "coingecko", now)
        except Exception as e:
            log.debug(f"CG native price error: {e}")
    
    # ══════════════════════════════════════════════════════════════════════════════
    # BINANCE SOURCE
    # ══════════════════════════════════════════════════════════════════════════════
    
    async def _refresh_from_binance(self, addresses: list[str]) -> None:
        """Fetch prices from Binance (for major tokens only)."""
        # Binance only has major tokens, check if we have any
        # Map token addresses to Binance symbols
        token_to_binance = self._get_binance_symbols()
        
        addresses_to_fetch = []
        for addr in addresses:
            if addr.lower() in token_to_binance:
                addresses_to_fetch.append(addr.lower())
        
        if not addresses_to_fetch:
            return
        
        await self._wait_rate_limit_binance()
        
        # Fetch all prices from Binance ticker
        url = f"{_BASE_URL_BINANCE}/ticker/price"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    self._calls_binance += 1
                    self._last_call_binance = time.time()
                    
                    if resp.status != 200:
                        self._errors_binance += 1
                        return
                    
                    data = await resp.json()
                    binance_prices = {item["symbol"]: float(item["price"]) for item in data}
                    
                    now = time.time()
                    for addr in addresses_to_fetch:
                        symbol = token_to_binance.get(addr)
                        if symbol and symbol in binance_prices:
                            price = binance_prices[symbol]
                            if price > 0:
                                self._update_price(addr, price, "binance", now)
        
        except Exception as e:
            log.debug(f"Binance error: {e}")
            self._errors_binance += 1
    
    def _get_binance_symbols(self) -> dict[str, str]:
        """Map token addresses to Binance symbols."""
        # Major tokens that exist on Binance
        return {
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "ETHUSDT",
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDCUSDT",
            "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDTUSDC",
            "0x6b175474e89094c44da98b954eedeac495271d0f": "DAIUSDT",
            "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTCUSDT",
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "BNBUSDT",
            "0x55d398326f99059ff775485246999027b3197955": "USDTUSDC",
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDCUSDT",
        }
    
    async def _wait_rate_limit_binance(self) -> None:
        """Wait for Binance rate limit."""
        if self._backoff_binance > 0:
            await asyncio.sleep(self._backoff_binance)
        
        elapsed = time.time() - self._last_call_binance
        if elapsed < _MIN_CALL_GAP_BINANCE:
            await asyncio.sleep(_MIN_CALL_GAP_BINANCE - elapsed)
    
    async def _refresh_native_price(self) -> None:
        """Refresh native price from Binance (faster than CG)."""
        symbol = _NATIVE_BINANCE_IDS.get(self._chain_id)
        if not symbol:
            await self._refresh_native_price_coingecko()
            return
        
        await self._wait_rate_limit_binance()
        
        url = f"{_BASE_URL_BINANCE}/ticker/price"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    self._calls_binance += 1
                    self._last_call_binance = time.time()
                    
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data["price"])
                        if price > 0:
                            self._native_usd = price
                            now = time.time()
                            self._update_price(self._wrapped_native, price, "binance", now)
        except:
            # Fallback to CoinGecko
            await self._refresh_native_price_coingecko()
    
    # ══════════════════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════════════════════════
    
    def _update_price(self, addr: str, price: float, source: str, timestamp: float) -> None:
        """Update price from a source."""
        addr = addr.lower()
        
        if addr in self._cache:
            current_price, current_time, sources = self._cache[addr]
            
            # If new price is significantly different, use it
            # Otherwise keep weighted average
            if source in sources:
                old_price, old_time = sources[source]
                if old_time > timestamp:
                    return  # Old data
            
            sources[source] = (price, timestamp)
            
            # Calculate weighted average
            total_weight = 0
            weighted_sum = 0
            for src, (p, t) in sources.items():
                weight = self._get_source_weight(src)
                # Decay old prices
                age = timestamp - t
                if age > _CACHE_TTL * 2:
                    weight *= 0.1
                weighted_sum += p * weight
                total_weight += weight
            
            avg_price = weighted_sum / total_weight if total_weight > 0 else price
            self._cache[addr] = (avg_price, timestamp, sources)
        else:
            self._cache[addr] = (price, timestamp, {source: (price, timestamp)})
    
    def _fetch_native_price_sync(self) -> None:
        """Blocking native price fetch for pre-event-loop initialization."""
        # Try Binance first as it's faster and has more generous limits
        symbol = _NATIVE_BINANCE_IDS.get(self._chain_id)
        if symbol:
            try:
                import urllib.request, json
                url = f"{_BASE_URL_BINANCE}/ticker/price?symbol={symbol.upper()}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    price = float(data["price"])
                    if price > 0:
                        self._native_usd = price
                        now = time.time()
                        self._cache[self._wrapped_native] = (price, now, {"binance": (price, now)})
                        log.info(f"Native price (Binance): ${price:.2f}")
                        return
            except Exception as e:
                log.debug(f"Binance sync fetch failed: {e}")

        # Fallback to CoinGecko
        coin_id = _NATIVE_COINGECKO_IDS.get(self._chain_id)
        if not coin_id:
            return
        
        try:
            import urllib.request, json
            url = f"{_BASE_URL_CG}/simple/price?ids={coin_id}&vs_currencies=usd"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                price = data.get(coin_id, {}).get("usd")
                if price:
                    self._native_usd = float(price)
                    now = time.time()
                    self._cache[self._wrapped_native] = (float(price), now, {"coingecko": (float(price), now)})
                    log.info(f"Native price (CoinGecko): ${price:.2f}")
                    return
        except Exception as e:
            log.warning(f"Native price sync failed (using default ${self._native_usd}): {e}")
    
    # ══════════════════════════════════════════════════════════════════════════════
    # STATS
    # ══════════════════════════════════════════════════════════════════════════════
    
    @property
    def stats(self) -> dict:
        cached = len(self._cache)
        fresh = sum(
            1 for _, (_, ts, _) in self._cache.items()
            if time.time() - ts < _CACHE_TTL
        )
        return {
            "cached_tokens": cached,
            "fresh": fresh,
            "stale": cached - fresh,
            "pending": len(self._pending_addrs),
            "calls_cg": self._calls_cg,
            "calls_binance": self._calls_binance,
            "errors_cg": self._errors_cg,
            "errors_binance": self._errors_binance,
            "native_price": round(self._native_usd, 2),
        }


