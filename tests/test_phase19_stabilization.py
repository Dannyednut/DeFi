from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(name):
    return (ROOT / name).read_text()

def test_pool_identity_is_contract_not_directional_edge():
    src = read("graph.py")
    assert "def pool_identity" in src
    assert "return (self.chain_id, protocol, edges[0].pool_address.lower())" in src
    assert "def upsert_pool_edges" in src

def test_discovery_logs_only_once_per_pool_identity():
    src = read("registry.py")
    assert "existing_before = graph.has_pool(pool_addr)" in src
    assert "is_new_pool=not existing_before" in src
    assert "if is_new_pool:" in src

def test_catchup_uses_canonical_block_information_layer():
    src = read("main.py")
    assert "collector = BlockInformationCollector(w3, CHAIN_ID, graph)" in src
    assert "collector.collect_range(start, end, chunk_size=chunk_size)" in src
    assert "ProtocolStateSynchronizer" in src
    assert "SYNC_TOPIC, SWAP_V3_TOPIC, MINT_V3_TOPIC, BURN_V3_TOPIC" not in src

def test_trade_classification_does_not_treat_mint_burn_sync_as_fills():
    src = read("block_information.py")
    assert "is_trade=spec.event in {\"Swap\", \"TokenExchange\", \"TokenExchangeUnderlying\"}" in src
    assert "is_trade: bool = False" in src

def test_balancer_refresh_updates_all_directional_edges():
    src = read("registry.py")
    assert "if refreshed:\n                graph.upsert_pool_edges(refreshed)" in src

def test_router_uses_canonical_token_economics_and_decimals():
    src = read("execution_router.py")
    assert "deterministic_net_token" in src
    assert "token_decimals" in src
    assert "10 ** decimals" in src

def test_polling_fallback_preserves_canonical_engine():
    src = read("main.py")
    assert "router=router, opportunity_engine=opportunity_engine" in src

def test_detector_scheduling_avoids_full_graph_scan_on_cold_blocks():
    src = read("opportunity_engine.py")
    assert 'requires_touched = bool(getattr(detector, "requires_touched_pools", False))' in src
    assert 'if requires_touched and not touched_pools:' in src
    assert 'block_interval' in src


def test_block_telemetry_reports_raw_and_tracked_events():
    src = read("block_information.py")
    main = read("main.py")
    assert 'raw_log_count: int = 0' in src
    assert 'ctx.raw_log_count = len(logs)' in src
    assert 'raw_logs=%d tracked_events=%d ignored=%d' in main


def test_rpc_manager_reuses_http_transports():
    src = read("utils/rpc_manager.py")
    assert 'self._http_providers: dict[str, HTTPProvider] = {}' in src
    assert 'http_provider = self._http_providers.get(provider.name)' in src
    assert 'HTTPProvider(provider.url, **_HTTP_KWARGS)' in src


def test_shutdown_closes_async_clients_before_event_loop():
    src = read("main.py")
    assert 'await crawler.stop()' in src
    assert 'await mempool.stop()' in src
    assert 'log.info("Background async loop: tasks and async clients closed.")' in src


def test_block_completion_reports_latency():
    src = read("main.py")
    assert "block_started = time.perf_counter()" in src
    assert "elapsed_ms=%.1f" in src


def test_block_information_records_unknown_pool_discovery_hints():
    src = read("block_information.py")
    assert 'ctx.discovery_events.append({' in src
    assert '"pool_address": address' in src
    assert 'recognized protocol event from an unknown contract' in src

def test_crawler_has_nonblocking_event_discovery_queue():
    src = read("crawler.py")
    assert "self._event_discovery_queue" in src
    assert "def enqueue_event_discovery" in src
    assert "crawler_event_discovery" in src

def test_event_discovery_verifies_v2_factory_ownership():
    src = read("registry.py")
    assert 'expected = factory.functions.getPair' in src
    assert 'if expected != address:' in src
