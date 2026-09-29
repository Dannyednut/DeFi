from types import SimpleNamespace

from block_information import BlockInformationCollector, ProtocolEventRegistry


class FakeW3:
    class _K:
        @staticmethod
        def keccak(text):
            import hashlib
            return bytes.fromhex(hashlib.sha256(text.encode()).hexdigest())
    keccak = staticmethod(_K.keccak)

    def __init__(self, logs):
        self._logs = logs
        self.eth = SimpleNamespace(
            get_block=lambda n: {
                "hash": bytes.fromhex("11" * 32),
                "parentHash": bytes.fromhex("22" * 32),
                "timestamp": 1,
                "gasLimit": 2,
                "gasUsed": 1,
                "baseFeePerGas": 3,
                "miner": "0x" + "33" * 20,
            },
            get_logs=lambda f: self._logs,
        )


def test_event_registry_contains_supported_protocols():
    r = ProtocolEventRegistry(FakeW3([]))
    protocols = {x.protocol for specs in r.by_topic.values() for x in specs}
    assert {"uniswap_v2", "uniswap_v3", "curve", "balancer_v2", "syncswap", "aerodrome_v2"} <= protocols


def test_collector_gates_generic_events_by_known_pool_protocol():
    w3 = FakeW3([])
    reg = ProtocolEventRegistry(w3)
    sync_topic = next(x.topic for specs in reg.by_topic.values() for x in specs if x.protocol == "uniswap_v2" and x.event == "Sync")
    log = {"address": "0x" + "aa" * 20, "topics": [sync_topic], "transactionHash": bytes.fromhex("44" * 32), "logIndex": 0}
    w3._logs = [log]
    graph = SimpleNamespace(_pool_index={
        log["address"]: [SimpleNamespace(protocol="uniswap_v2")]
    })
    ctx = BlockInformationCollector(w3, 1, graph).collect(10)
    assert len(ctx.state_changes) == 1
    assert ctx.state_changes[0].protocol == "uniswap_v2"


def test_unknown_pool_event_is_ignored():
    w3 = FakeW3([])
    reg = ProtocolEventRegistry(w3)
    topic = reg.topics[0]
    w3._logs = [{"address": "0x" + "bb" * 20, "topics": [topic]}]
    graph = SimpleNamespace(_pool_index={})
    ctx = BlockInformationCollector(w3, 1, graph).collect(10)
    assert ctx.state_changes == []
