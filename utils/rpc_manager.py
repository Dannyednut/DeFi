"""
Multi-RPC Manager
================
Distributes requests across multiple RPC providers for:
- Rate limit distribution
- Higher throughput
- Redundancy and failover
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from collections import deque

from web3 import Web3
from web3.middleware import geth_poa_middleware
from web3.providers import HTTPProvider, WebsocketProvider
from web3.providers.base import JSONBaseProvider
from web3._utils.caching import generate_cache_key
from web3.types import RPCEndpoint, RPCResponse
import json

from log import get_logger
logger = get_logger("rpc_manager")

# MEV OPTIMIZATION: Connection pool settings for lower latency
_HTTP_KWARGS = {
    "request_kwargs": {
        "timeout": 30,  # Increased from default 10s
    }
}

_WS_KWARGS = {
    "websocket_kwargs": {
        "ping_interval": 30,
        "ping_timeout": 15,
        "close_timeout": 10,
    }
}


class FailoverProvider(JSONBaseProvider):
    """
    Web3 provider that routes all RPC calls through MultiRPCManager's failover logic.
    
    Usage:
        manager = MultiRPCManager.from_config(rpc_configs)
        w3 = manager.get_failover_web3()  # Returns Web3 with this provider
        
        # All calls now automatically failover across providers:
        result = w3.eth.call(tx)  # Will retry on different RPCs if one fails
    """
    
    def __init__(self, rpc_manager: MultiRPCManager, prefer_ws: bool = False):
        super().__init__()
        self.rpc_manager = rpc_manager
        self.prefer_ws = prefer_ws
        self._request_cache: dict = {}
    
    def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        """Execute RPC request with automatic failover across providers."""
        request_data = self.encode_rpc_request(method, params)
        
        # Try each provider in order with circuit breaker and rate limiting
        last_error = None
        
        for attempt in range(len(self.rpc_manager.providers)):
            provider = self.rpc_manager._select_provider(prefer_ws=self.prefer_ws)
            if not provider:
                time.sleep(0.1)  # Brief backoff when all busy
                continue
            
            try:
                # Create appropriate provider based on URL scheme and preference
                if provider.url.startswith("wss://") or provider.url.startswith("ws://"):
                    ws_provider = WebsocketProvider(provider.url, **_WS_KWARGS)
                    response = ws_provider.make_request(method, params)
                else:
                    http_provider = HTTPProvider(provider.url, **_HTTP_KWARGS)
                    response = http_provider.make_request(method, params)
                
                # Track success
                provider.requests_made += 1
                self.rpc_manager.total_requests += 1
                self.rpc_manager._circuit_breaker.record_success(provider.name)
                
                return response
                
            except Exception as e:
                # Track failure
                provider.requests_failed += 1
                provider.last_error = str(e)
                last_error = e
                self.rpc_manager._circuit_breaker.record_failure(provider.name)
                
                # Handle rate limit errors
                error_str = str(e)
                if "429" in error_str or "rate" in error_str.lower():
                    provider.is_healthy = False
                    # Schedule health restoration using threading (sync-safe)
                    def restore():
                        time.sleep(5)
                        provider.is_healthy = True
                        logger.info(f"RPC {provider.name} restored to healthy state")
                    threading.Timer(5.0, restore).start()
                
                logger.debug(f"RPC {provider.name} failed for {method}: {e}")
                continue
        
        # All providers exhausted
        self.rpc_manager.total_failures += 1
        
        if last_error:
            raise last_error
        raise Exception(f"All RPC providers failed for {method}")
    
    def is_connected(self, show_traceback: bool = False) -> bool:
        """Check if any provider is connected."""
        for provider in self.rpc_manager.providers:
            if provider.is_healthy and self.rpc_manager._circuit_breaker.can_request(provider.name):
                try:
                    http_provider = HTTPProvider(provider.url, **_HTTP_KWARGS)
                    w3 = Web3(http_provider)
                    if w3.is_connected():
                        return True
                except:
                    if show_traceback:
                        import traceback
                        traceback.print_exc()
                    continue
        return False


class CircuitBreaker:
    """
    Circuit breaker pattern for RPC providers.
    Stops hammering failing RPCs and allows gradual recovery.
    """
    FAILURE_THRESHOLD = 1
    RECOVERY_TIME = 30  # seconds
    HALF_OPEN_REQUESTS = 3  # test requests in half-open state
    
    def __init__(self):
        self._states: dict[str, str] = {}  # provider_name -> state
        self._failure_counts: dict[str, int] = {}
        self._last_failure: dict[str, float] = {}
        self._test_requests: dict[str, int] = {}  # for half-open state
    
    def record_success(self, provider_name: str) -> None:
        """Record a successful request."""
        state = self._states.get(provider_name, "CLOSED")
        
        if state == "HALF_OPEN":
            self._test_requests[provider_name] = self._test_requests.get(provider_name, 0) + 1
            if self._test_requests[provider_name] >= self.HALF_OPEN_REQUESTS:
                # Success! Close the circuit
                self._states[provider_name] = "CLOSED"
                self._failure_counts[provider_name] = 0
                del self._test_requests[provider_name]
                logger.info(f"Circuit breaker CLOSED for {provider_name} - recovery confirmed")
        elif state == "CLOSED":
            # Reset failure count on success
            if provider_name in self._failure_counts:
                del self._failure_counts[provider_name]
    
    def record_failure(self, provider_name: str) -> None:
        """Record a failed request and potentially open the circuit."""
        now = time.time()
        self._last_failure[provider_name] = now
        
        state = self._states.get(provider_name, "CLOSED")
        
        if state == "CLOSED":
            self._failure_counts[provider_name] = self._failure_counts.get(provider_name, 0) + 1
            if self._failure_counts[provider_name] >= self.FAILURE_THRESHOLD:
                # Open the circuit
                self._states[provider_name] = "OPEN"
                logger.warning(f"Circuit breaker OPENED for {provider_name} - {self.FAILURE_THRESHOLD} failures")
        elif state == "HALF_OPEN":
            # Test request failed, go back to open
            self._states[provider_name] = "OPEN"
            del self._test_requests[provider_name]
            logger.warning(f"Circuit breaker back to OPEN for {provider_name} - recovery test failed")
    
    def can_request(self, provider_name: str) -> bool:
        """Check if a request can be made to this provider."""
        state = self._states.get(provider_name, "CLOSED")
        
        if state == "CLOSED":
            return True
        elif state == "OPEN":
            # Check if recovery time has passed
            last_fail = self._last_failure.get(provider_name, 0)
            if time.time() - last_fail > self.RECOVERY_TIME:
                # Move to half-open and allow test request
                self._states[provider_name] = "HALF_OPEN"
                self._test_requests[provider_name] = 0
                logger.info(f"Circuit breaker HALF_OPEN for {provider_name} - testing recovery")
                return True
            return False
        elif state == "HALF_OPEN":
            # Allow limited test requests
            return self._test_requests.get(provider_name, 0) < self.HALF_OPEN_REQUESTS
        
        return True
    
    def get_state(self, provider_name: str) -> str:
        """Get current circuit state for debugging."""
        return self._states.get(provider_name, "CLOSED")


@dataclass
class RPCProvider:
    """Represents a single RPC endpoint with rate limiting."""
    url: str
    name: str
    priority: int = 1
    max_rps: float = 10.0
    is_ws: bool = False
    
    # Rate limiting state
    _tokens: float = field(default=10.0, init=False)
    _last_refill: float = field(default=time.monotonic(), init=False)
    
    # Stats
    requests_made: int = 0
    requests_failed: int = 0
    last_error: Optional[str] = None
    is_healthy: bool = True
    
    def refill_tokens(self) -> None:
        """Refill rate limit tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.max_rps, self._tokens + elapsed * self.max_rps)
        self._last_refill = now
    
    def consume_token(self) -> bool:
        """Try to consume a token for a request."""
        self.refill_tokens()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class MultiRPCManager:
    """
    Manages multiple RPC endpoints with:
    - Round-robin or least-loaded selection
    - Automatic failover on errors
    - Rate limit distribution
    - Request queuing when all providers busy
    - Circuit breaker pattern for failing providers
    """
    
    def __init__(self, providers: list[RPCProvider]):
        self.providers = providers
        self._current_index = 0
        self._request_queue: asyncio.Queue = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(50)  # Max concurrent requests
        self._lock = asyncio.Lock()
        
        # MEV OPTIMIZATION: Circuit breaker for RPC resilience
        self._circuit_breaker = CircuitBreaker()
        
        # Stats
        self.total_requests = 0
        self.total_failures = 0
    
    @classmethod
    def from_config(cls, config_list: list) -> "MultiRPCManager":
        """Create manager from config list."""
        providers = [
            RPCProvider(
                url=c.url,
                name=c.name,
                priority=c.priority,
                max_rps=c.max_rps,
                is_ws=c.is_ws,
            )
            for c in config_list
        ]
        return cls(providers)
    
    def get_web3(self, ws: bool = False, provider: Optional[RPCProvider] = None) -> Web3:
        """Get a Web3 instance connected to a specific provider or the best available."""
        if provider:
            p = provider
            if ws:
                ws_provider = WebsocketProvider(p.url, **_WS_KWARGS)
            else:
                ws_provider = HTTPProvider(p.url, **_HTTP_KWARGS)
        else:
            providers_sub = [p for p in self.providers if p.is_ws == ws]
            if not providers_sub:
                p = self.providers[0]
                if ws:
                    ws_provider = WebsocketProvider(p.url, **_WS_KWARGS)
                else:
                    ws_provider = HTTPProvider(p.url, **_HTTP_KWARGS)
            else:
                available = sorted(
                    [p for p in providers_sub if p.is_healthy],
                    key=lambda x: x.priority
                )
                p = available[0] if available else providers_sub[0]
                if ws:
                    ws_provider = WebsocketProvider(p.url, **_WS_KWARGS)
                else:
                    ws_provider = HTTPProvider(p.url, **_HTTP_KWARGS)
        
        w3 = Web3(ws_provider)
        # Apply middleware for PoA chains
        if w3.is_connected():
            try:
                # Cache chain_id or detect once
                chain_id = w3.eth.chain_id
                if chain_id in (56, 137, 8453, 42161, 324):
                    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except:
                pass
        return w3
    
    def get_failover_web3(self, ws: bool = False) -> Web3:
        """
        Get a Web3 instance with automatic failover across all RPC providers.
        
        Unlike get_web3() which returns a static connection to one provider,
        this returns a Web3 instance that automatically retries failed requests
        on different providers with circuit breaker and rate limiting.
        
        Args:
            ws: If True, prefer WebSocket connections. If False, prefer HTTP.
        
        Usage:
            w3 = rpc_manager.get_failover_web3(ws=False)  # HTTP preferred
            # All calls automatically failover if a provider fails:
            result = w3.eth.call(tx)
            logs = w3.eth.get_logs(filter_params)
        """
        failover_provider = FailoverProvider(self, prefer_ws=ws)
        w3 = Web3(failover_provider)
        
        # Apply middleware for PoA chains
        if w3.is_connected():
            try:
                chain_id = w3.eth.chain_id
                if chain_id in (56, 137, 8453, 42161, 324):
                    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except:
                pass
        
        return w3
    
    def _select_provider(self, prefer_ws: bool = False) -> Optional[RPCProvider]:
        """Select the best available provider using round-robin with rate limit and circuit breaker.
        
        Args:
            prefer_ws: If True, prefer WebSocket providers. If False, prefer HTTP providers.
        """
        # Filter by connection type preference
        def is_preferred_type(p: RPCProvider) -> bool:
            is_ws_url = p.url.startswith("wss://") or p.url.startswith("ws://")
            return is_ws_url if prefer_ws else not is_ws_url
        
        # Try preferred type first, then fallback to any
        for prefer_type in [True, False]:
            candidates = sorted(
                [p for p in self.providers 
                 if p.is_healthy 
                 and self._circuit_breaker.can_request(p.name)
                 and (not prefer_type or is_preferred_type(p))],
                key=lambda x: x.priority
            )
            
            if prefer_type and not candidates:
                continue  # No preferred type available, try any
            
            for p in candidates:
                if p.consume_token():
                    return p
            
            if not prefer_type:
                break  # Already tried any type
        
        # All at rate limit or circuit open, check if any circuit is half-open
        for p in self.providers:
            if p.is_healthy and self._circuit_breaker.get_state(p.name) == "HALF_OPEN":
                return p
        
        # All at rate limit, return any healthy provider (will wait)
        healthy = [p for p in self.providers if p.is_healthy]
        return healthy[0] if healthy else None
    
    async def eth_call(
        self,
        call_params: dict,
        block: str = "latest"
    ) -> Any:
        """Make an eth_call with automatic failover."""
        last_error = None
        
        for attempt in range(len(self.providers)):
            provider = self._select_provider()
            if not provider:
                await asyncio.sleep(0.1)
                continue
            
            try:
                w3 = self.get_web3(provider=provider)
                result = w3.eth.call(call_params, block)
                provider.requests_made += 1
                self.total_requests += 1
                self._circuit_breaker.record_success(provider.name)
                return result
            except Exception as e:
                provider.requests_failed += 1
                provider.last_error = str(e)
                last_error = e
                self._circuit_breaker.record_failure(provider.name)
                
                if "429" in str(e) or "rate" in str(e).lower():
                    # Rate limited, mark as temporarily unhealthy
                    provider.is_healthy = False
                    asyncio.create_task(self._restore_health(provider))
                
                logger.debug(f"RPC {provider.name} failed: {e}")
        
        self.total_failures += 1
        raise last_error or Exception("All RPC providers failed")
    
    async def eth_get_logs(self, filter_params: dict) -> list:
        """Fetch logs with failover."""
        last_error = None
        
        for attempt in range(len(self.providers)):
            provider = self._select_provider()
            if not provider:
                await asyncio.sleep(0.1)
                continue
            
            try:
                w3 = self.get_web3(provider=provider)
                result = w3.eth.get_logs(filter_params)
                provider.requests_made += 1
                self.total_requests += 1
                return result
            except Exception as e:
                provider.requests_failed += 1
                provider.last_error = str(e)
                last_error = e
                
                if "429" in str(e) or "rate" in str(e).lower():
                    provider.is_healthy = False
                    asyncio.create_task(self._restore_health(provider))
                
                logger.debug(f"RPC {provider.name} get_logs failed: {e}")
        
        self.total_failures += 1
        raise last_error or Exception("All RPC providers failed for get_logs")
    
    async def eth_get_block(self, block: Any, full: bool = False) -> dict:
        """Fetch block with failover."""
        last_error = None
        
        for attempt in range(len(self.providers)):
            provider = self._select_provider()
            if not provider:
                await asyncio.sleep(0.1)
                continue
            
            try:
                w3 = self.get_web3(provider=provider)
                result = w3.eth.get_block(block, full_transactions=full)
                provider.requests_made += 1
                self.total_requests += 1
                return result
            except Exception as e:
                provider.requests_failed += 1
                provider.last_error = str(e)
                last_error = e
                
                if "429" in str(e):
                    provider.is_healthy = False
                    asyncio.create_task(self._restore_health(provider))
        
        raise last_error or Exception("All RPC providers failed")
    
    async def eth_get_transaction_by_hash(self, tx_hash: str) -> dict:
        """Fetch transaction by hash with failover."""
        last_error = None
        
        for attempt in range(len(self.providers)):
            provider = self._select_provider()
            if not provider:
                await asyncio.sleep(0.1)
                continue
            
            try:
                w3 = self.get_web3(provider=provider)
                result = w3.eth.get_transaction(tx_hash)
                provider.requests_made += 1
                self.total_requests += 1
                return result
            except Exception as e:
                # "Not found" is common for dropped txs, not a provider error
                if "not found" not in str(e).lower():
                    provider.requests_failed += 1
                    provider.last_error = str(e)
                last_error = e
                
                if "429" in str(e):
                    provider.is_healthy = False
                    asyncio.create_task(self._restore_health(provider))
        
        raise last_error or Exception("All RPC providers failed")
    
    async def eth_get_balance(self, address: str, block: str = "latest") -> int:
        """Fetch balance with failover."""
        last_error = None
        
        for attempt in range(len(self.providers)):
            provider = self._select_provider()
            if not provider:
                await asyncio.sleep(0.1)
                continue
            
            try:
                w3 = self.get_web3(provider=provider)
                result = w3.eth.get_balance(address, block)
                provider.requests_made += 1
                self.total_requests += 1
                return result
            except Exception as e:
                provider.requests_failed += 1
                provider.last_error = str(e)
                last_error = e
        
        raise last_error or Exception("All RPC providers failed")
    
    async def eth_contract_call(self, contract_fn, *args, block: str = "latest") -> Any:
        """Make a contract call with failover."""
        last_error = None
        
        for attempt in range(len(self.providers)):
            provider = self._select_provider()
            if not provider:
                await asyncio.sleep(0.1)
                continue
            
            try:
                result = contract_fn(*args).call(block_identifier=block)
                provider.requests_made += 1
                self.total_requests += 1
                return result
            except Exception as e:
                provider.requests_failed += 1
                provider.last_error = str(e)
                last_error = e
                
                if "429" in str(e):
                    provider.is_healthy = False
                    asyncio.create_task(self._restore_health(provider))
        
        raise last_error or Exception("All RPC providers failed")
    
    async def _restore_health(self, provider: RPCProvider) -> None:
        """Restore provider health after a cooldown period."""
        await asyncio.sleep(5)  # Wait 5 seconds
        provider.is_healthy = True
        logger.info(f"RPC {provider.name} restored to healthy state")
    
    def get_stats(self) -> dict:
        """Get aggregated stats across all providers."""
        return {
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "success_rate": (
                (self.total_requests - self.total_failures) / self.total_requests * 100
                if self.total_requests > 0 else 0
            ),
            "providers": [
                {
                    "name": p.name,
                    "requests_made": p.requests_made,
                    "requests_failed": p.requests_failed,
                    "is_healthy": p.is_healthy,
                    "last_error": p.last_error,
                }
                for p in self.providers
            ]
        }
    
    def get_least_loaded_provider(self) -> Optional[RPCProvider]:
        """Get the provider with the lowest current load."""
        healthy = [p for p in self.providers if p.is_healthy]
        if not healthy:
            return None
        return min(healthy, key=lambda p: p._tokens)


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK POLLING MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class BlockPoller:
    """
    Manages block polling with multiple RPC providers.
    Tracks block confirmations and detects reorgs.
    """
    
    def __init__(self, rpc_manager: MultiRPCManager, confirmations: int = 1):
        self.rpc = rpc_manager
        self.confirmations = confirmations
        self.current_block = 0
        self.block_timestamps: deque = deque(maxlen=100)
        self.last_block_time = 0
        self.avg_block_time = 12.0  # Default for Ethereum
    
    async def start(self) -> int:
        """Initialize and get current block."""
        block = await self.rpc.eth_get_block("latest")
        self.current_block = block["number"]
        self.last_block_time = block["timestamp"]
        return self.current_block
    
    async def wait_for_new_block(self, timeout: float = 30.0) -> Optional[int]:
        """Wait for a new block, return block number."""
        start_time = time.time()
        last_checked = self.current_block
        
        while time.time() - start_time < timeout:
            try:
                block = await self.rpc.eth_get_block("latest")
                block_num = block["number"]
                
                if block_num > self.current_block:
                    # New block!
                    self._update_block_stats(block)
                    return block_num
                
                # Update average block time
                if block_num > last_checked:
                    block_time = block["timestamp"] - self.last_block_time
                    self.avg_block_time = 0.7 * self.avg_block_time + 0.3 * block_time
                    last_checked = block_num
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.warning(f"Block polling error: {e}")
                await asyncio.sleep(1)
        
        return None
    
    def _update_block_stats(self, block: dict) -> None:
        """Update block statistics."""
        self.current_block = block["number"]
        block_time = block["timestamp"]
        
        if self.last_block_time > 0:
            self.block_timestamps.append(block_time - self.last_block_time)
            
            if len(self.block_timestamps) > 1:
                self.avg_block_time = sum(self.block_timestamps) / len(self.block_timestamps)
        
        self.last_block_time = block_time
    
    def get_safe_block(self) -> int:
        """Get a block number that is confirmed (safe from reorg)."""
        return max(0, self.current_block - self.confirmations)
    
    def get_stats(self) -> dict:
        """Get poller stats."""
        return {
            "current_block": self.current_block,
            "avg_block_time": self.avg_block_time,
            "confirmations": self.confirmations,
            "safe_block": self.get_safe_block(),
        }


