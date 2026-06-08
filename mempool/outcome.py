"""
mempool/outcome.py — Post-mine outcome resolver.

On each confirmed block:
  1. Checks which pending tx hashes were included (mined)
  2. Checks remaining pending hashes — if too old, mark as dropped
  3. For mined txs: fetches actual post-swap reserves and computes
     simulation accuracy vs our pre-mine prediction
  4. Updates PendingOpportunity with outcome fields
  5. Logs completed records to pending_opportunities.jsonl

Simulation accuracy metric
──────────────────────────
After a tx mines, we re-fetch the affected pool's reserves.
Accuracy = 1 - |simulated_reserve_change - actual_reserve_change| / actual_reserve_change

100% = our shadow math was exact
<80%  = significant slippage, partial fill, or fee-on-transfer token
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from web3 import Web3
    from mempool.simulator import PendingOpportunity
    from graph import TokenGraph

from config import LOG_DIR, UNISWAP_V2_PAIR_ABI, UNISWAP_V3_POOL_ABI, MAX_PENDING_BLOCKS

from log import get_logger
log = get_logger("outcome")


class OutcomeResolver:
    """
    Maintains a registry of in-flight PendingOpportunity objects.
    Call on_block() each confirmed block to resolve outcomes.
    """

    def __init__(self, w3: "Web3", graph: "TokenGraph", graph_client: Optional["GraphClient"] = None):
        self._w3     = w3
        self._graph  = graph
        # self._graph_client = graph_client # Dead dependency
        self._log_path = Path(LOG_DIR) / "pending_opportunities.jsonl"
        self._competitor_path = Path(LOG_DIR) / "competitors.jsonl"
        self._shadow_path = Path(LOG_DIR) / "shadow_trades.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        # tx_hash → (PendingOpportunity, seen_block)
        self._pending: dict[str, tuple["PendingOpportunity", int]] = {}

        # Accuracy stats
        self._mined_count      = 0
        self._reverted_count   = 0
        self._dropped_count    = 0
        self._accuracy_sum     = 0.0
        self._accuracy_samples = 0
        self._resolved_count   = 0 # Total resolved (mined + reverted + dropped)
        self._loop = None  # Background loop for scheduling tasks from sync threads
        log.info(f"OutcomeResolver ready | log={self._log_path}")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop to use for background tasks."""
        self._loop = loop

    def register(self, pending_opp: "PendingOpportunity", current_block: int) -> None:
        """Register a PendingOpportunity for outcome tracking."""
        # Normalise to standard lowercase hex with 0x prefix
        raw = pending_opp.trigger_tx_hash
        if hasattr(raw, "hex"):       # HexBytes
            h = raw.hex().lower()
        elif isinstance(raw, str):
            h = raw.lower()
            if not h.startswith("0x"):
                h = "0x" + h
        else:
            h = str(raw).lower()
            if not h.startswith("0x"): h = "0x" + h

        # Ensure correct length for 0x + 64 hex chars
        if len(h) < 66 and h.startswith("0x"):
            h = "0x" + h[2:].zfill(64)

        self._pending[h] = (pending_opp, current_block)
        log.debug(f"Registered pending {pending_opp.opp_type} | hash={h[:10]} | block={current_block}")
        
        # Immediately log the pending state to the JSONL file
        pending_opp.tx_outcome = "pending"
        self._log(pending_opp)

    def on_block(self, block_number: int, tracker=None, is_public_fn=None) -> None:
        """
        Called on each confirmed block.
        Resolves pending txs and optionally notifies the OpportunityTracker
        with mempool_to_open_ms data.
        """
        if block_number % 20 == 0:
            log.debug(f"OUTCOME | Solver heartbeat block {block_number} | Pending arbs: {len(self._pending)}")

        if not self._pending:
            return

        log.research(f"OUTCOME | Block {block_number} | Checking {len(self._pending)} pending arbs")


        # Fetch the confirmed block with full transactions for shadow audit & resolution
        try:
            block = self._w3.eth.get_block(block_number, full_transactions=True)
            if not block:
                log.warning(f"Block {block_number} fetch returned None")
                return

            base_fee = block.get("baseFeePerGas", 0)

            
            transactions = block.get("transactions", [])
            mined_hashes = set()
            for tx in transactions:
                # Normalise to 0x + 64 hex chars
                if hasattr(tx["hash"], "hex"):
                    raw_h = tx["hash"].hex().lower()
                else:
                    raw_h = str(tx["hash"]).lower()
                
                if not raw_h.startswith("0x"): raw_h = "0x" + raw_h
                if len(raw_h) < 66: raw_h = "0x" + raw_h[2:].zfill(64)
                
                mined_hashes.add(raw_h)
            
            log.debug(f"Block {block_number}: Checking {len(self._pending)} pending vs {len(mined_hashes)} mined hashes")

                
        except Exception as e:
            log.warning(f"Could not fetch block {block_number}: {e}")
            return

        to_remove = []
        mined_count = dropped_count = 0

        for tx_hash, (opp, seen_block) in list(self._pending.items()):
            # Already normalized in register(), but double-check
            h = tx_hash.lower()
            if not h.startswith("0x"): h = "0x" + h
            if len(h) < 66: h = "0x" + h[2:].zfill(64)
            
            if h in mined_hashes:
                log.info(f"OUTCOME mined   | {opp.opp_type} | tx={h[:10]} | block={block_number}")
                self._resolve_mined(opp, block_number, tracker, base_fee)
                to_remove.append(tx_hash)
                mined_count += 1

            elif block_number - seen_block > MAX_PENDING_BLOCKS:
                log.info(f"OUTCOME dropped | {opp.opp_type} | tx={h[:10]} | aged {block_number - seen_block} blocks")
                opp.tx_outcome = "dropped"
                self._log(opp)
                self._dropped_count += 1
                self._resolved_count += 1
                to_remove.append(tx_hash)
                dropped_count += 1
            else:
                log.debug(f"OUTCOME pending | {opp.opp_type} | tx={h[:10]} | age={block_number - seen_block}")

        for h in to_remove:
            self._pending.pop(h, None)

        if mined_count or dropped_count:
            log.info(f"Block {block_number}: resolved {mined_count} mined, {dropped_count} dropped | {len(self._pending)} still pending")

        # Shadow Audit (Audit ALL txs in the block)
        if is_public_fn:
            self._audit_shadow_trades(block, is_public_fn)

    @property
    def stats(self) -> dict:
        avg_acc = (
            self._accuracy_sum / self._accuracy_samples
            if self._accuracy_samples > 0 else None
        )
        return {
            "in_flight":        len(self._pending),
            "resolved":         self._resolved_count,
            "mined":            self._mined_count,
            "reverted":         self._reverted_count,
            "dropped":          self._dropped_count,
            "avg_sim_accuracy": round(avg_acc, 2) if avg_acc is not None else None,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _resolve_mined(
        self,
        opp: "PendingOpportunity",
        block_number: int,
        tracker,
        base_fee: int = 0,
    ) -> None:
        now = time.time()
        opp.mined_block = block_number
        opp.mined_at    = now

        # Fetch actual post-mine reserves
        actual_r_in, actual_r_out = self._fetch_reserves(opp.affected_pool, opp.token_in)

        if actual_r_in is not None and actual_r_out is not None:
            opp.actual_reserve_in_after  = actual_r_in
            opp.actual_reserve_out_after = actual_r_out

            # Did the tx revert? (reserves unchanged)
            if (actual_r_in == opp.reserve_in_before and
                    actual_r_out == opp.reserve_out_before):
                opp.tx_outcome = "reverted"
                self._reverted_count += 1
            else:
                opp.tx_outcome = "mined"
                self._mined_count += 1

                # Simulation accuracy
                sim_delta   = abs(opp.reserve_in_after  - opp.reserve_in_before)
                actual_delta = abs(actual_r_in - opp.reserve_in_before)
                if actual_delta > 0:
                    accuracy = 1.0 - abs(sim_delta - actual_delta) / actual_delta
                    accuracy = max(0.0, min(1.0, accuracy))
                    opp.simulation_accuracy_pct = round(accuracy * 100, 2)
                    self._accuracy_sum    += opp.simulation_accuracy_pct
                    self._accuracy_samples += 1
        else:
            opp.tx_outcome = "mined"
            self._mined_count += 1

        # GOLD UPGRADE: Competitor Forensics
        if opp.tx_outcome == "mined":
            self._fetch_forensics(opp, base_fee)
            
            # Subgraph Reconciliation (Background)
            # if self._graph_client: # Removed: verification runs strictly native RPC now
            coro = self._verify_actual_swap(opp, opp.trigger_tx_hash)
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(lambda: self._loop.create_task(coro))
            else:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(coro)
                except RuntimeError:
                    # Skip if no loop available (common in isolated tests)
                    pass

        # If tracker is provided, record the mempool→open timing
        if tracker and opp.tx_outcome == "mined":
            self._record_mempool_timing(opp, block_number, tracker)

        if opp.tx_outcome == "mined" and actual_r_in and actual_r_out:
            sim_price = opp.reserve_out_after / opp.reserve_in_after
            actual_price = actual_r_out / actual_r_in
            log.research(f"REALITY CHECK | expected={sim_price:.10g} | actual={actual_price:.10g} | acc={opp.simulation_accuracy_pct}%")

        self._log(opp)
        self._resolved_count += 1
        log.info(
            f"{opp.trigger_tx_hash[:10]} → {opp.tx_outcome} "
            f"| acc={opp.simulation_accuracy_pct}%"
        )

    def _fetch_reserves(
        self, pool_addr: str, token_in: str
    ) -> tuple[Optional[int], Optional[int]]:
        """Fetch current reserves for a pool. Returns (r_in, r_out) or (None, None)."""
        edges = self._graph._pool_index.get(pool_addr.lower())
        if not edges:
            log.debug(f"Fetch reserves: pool {pool_addr[:10]} not in graph index")
            return None, None

        version = edges[0].version
        try:
            if version == 2:
                contract = self._w3.eth.contract(
                    address=self._w3.to_checksum_address(pool_addr),
                    abi=UNISWAP_V2_PAIR_ABI,
                )
                r0, r1, _ = contract.functions.getReserves().call()
                # Determine which reserve is token_in
                t0 = edges[0].token_in if edges[0].token_in < edges[0].token_out else edges[0].token_out
                if token_in.lower() == t0.lower():
                    return r0, r1
                return r1, r0
            else:
                contract = self._w3.eth.contract(
                    address=self._w3.to_checksum_address(pool_addr),
                    abi=UNISWAP_V3_POOL_ABI,
                )
                slot0 = contract.functions.slot0().call()
                liq   = contract.functions.liquidity().call()
                sqrt  = slot0[0]
                # Approximate virtual reserves from sqrtPrice and liquidity
                if sqrt == 0 or liq == 0:
                    return None, None
                r0 = liq * (2**96) // sqrt
                r1 = liq * sqrt // (2**96)
                t0 = edges[0].token_in if edges[0].token_in < edges[0].token_out else edges[0].token_out
                if token_in.lower() == t0.lower():
                    return r0, r1
                return r1, r0
        except Exception as e:
            log.debug(f"Reserve fetch failed for {pool_addr[:10]}: {e}")
            return None, None

    def _record_mempool_timing(
        self, opp: "PendingOpportunity", mined_block: int, tracker
    ) -> None:
        """
        If the tracker has an active opportunity that was created around
        the time this tx mined, record the mempool→open latency.
        """
        try:
            from tracker import _fingerprint as fp_fn
            from logger import Opportunity, OppType

            # Build a synthetic opportunity to generate the fingerprint
            try:
                opp_type_enum = OppType(opp.opp_type)
            except ValueError:
                return

            synthetic = Opportunity(
                opp_type=opp_type_enum,
                chain_id=0,
                block_number=mined_block,
                tokens=opp.arb_tokens,
                pools=opp.arb_pools,
                dexes=opp.arb_dexes,
            )
            fp = fp_fn(synthetic)

            active = tracker._active.get(fp)
            if active:
                # Time from mempool hash seen to opportunity being active (block mined)
                opp.opp_open_block  = active.open_block
                opp.mempool_to_open_ms = (active.open_time - opp.pending_seen_at) * 1000
        except Exception:
            pass

    def _fetch_forensics(self, opp: "PendingOpportunity", base_fee: int = 0) -> None:
        """Fetch winner data: EOA, Contract, Tip, and Gas usage."""
        try:
            tx = self._w3.eth.get_transaction(opp.trigger_tx_hash)
            receipt = self._w3.eth.get_transaction_receipt(opp.trigger_tx_hash)
            
            opp.winner_eoa = (tx.get("from") or "").lower()
            opp.winner_contract = (tx.get("to") or "").lower()
            opp.gas_used = receipt.get("gasUsed", 0)
            
            # Tip logic (EIP-1559 vs Legacy)
            effective_gas_price = receipt.get("effectiveGasPrice", 0)
            # base_fee is now passed in to save an RPC call

            # Priority Fee (Tip) calculation
            if "maxPriorityFeePerGas" in tx:
                opp.tip_gwei = min(tx["maxPriorityFeePerGas"], 
                                 tx["maxFeePerGas"] - base_fee) / 1e9
            else:
                opp.tip_gwei = (effective_gas_price - base_fee) / 1e9

            opp.total_fee_eth = (opp.gas_used * effective_gas_price) / 1e18
            
            self._log_competitor(opp)
        except Exception as e:
            log.debug(f"Forensics failed for {opp.trigger_tx_hash[:10]}: {e}")

    def _log_competitor(self, opp: "PendingOpportunity") -> None:
        """Log competitor Alpha data to a separate leaderboard file."""
        data = {
            "timestamp": time.time(),
            "block": opp.mined_block,
            "winner_eoa": opp.winner_eoa,
            "winner_contract": opp.winner_contract,
            "tip_gwei": round(opp.tip_gwei or 0, 8),
            "gas_used": opp.gas_used,
            "total_fee_eth": round(opp.total_fee_eth or 0, 8),
            "opp_type": opp.opp_type,
            "tx_hash": opp.trigger_tx_hash,
            "accuracy": opp.simulation_accuracy_pct
        }
        with open(self._competitor_path, "a") as f:
            f.write(json.dumps(data) + "\n")
            f.flush()
        
        # RESEARCH SHOUT-OUT
        log.research(f"WINNER | {opp.winner_eoa[:10]}... | "
              f"Tip: {data['tip_gwei']} Gwei | Gas: {data['gas_used']} | "
              f"Contract: {opp.winner_contract[:10]}...")

    def _log(self, opp: "PendingOpportunity") -> None:
        with open(self._log_path, "a") as f:
            f.write(json.dumps(opp.to_dict()) + "\n")
            f.flush()

    def _audit_shadow_trades(self, block: dict, is_public_fn) -> None:
        """Find DEX trades that were never seen in the public mempool."""
        try:
            block_number = block.get("number", 0)
            from mempool.decoder import get_router_addresses
            from config import CHAIN_ID, get_chain
            
            chain = get_chain()
            actual_chain_id = chain.chain_id if chain else CHAIN_ID
            routers = {addr.lower() for addr in get_router_addresses(actual_chain_id)}
            
            if not routers:
                log.debug(f"Shadow audit: No routers configured for chain {actual_chain_id}")
                return
            
            log.debug(f"Shadow audit: block {block_number} | {len(routers)} routers")

            
            builder = self._extract_builder(block.get("extraData", b"").hex())
            
            shadow_count = 0
            for tx in block.get("transactions", []):
                h = tx["hash"].hex().lower()
                if not h.startswith("0x"): h = "0x" + h
                
                # If it's a DEX trade AND we didn't see it in public mempool
                if not is_public_fn(h):
                    to_addr = (tx.get("to") or "").lower()
                    if to_addr in routers:
                        shadow_count += 1
                        self._log_shadow(h, to_addr, builder, block_number)
            
            if shadow_count > 0:
                log.research(f"SHADOW | {shadow_count} private DEX trades in block {block_number} | Builder: {builder}")
                
        except Exception as e:
            log.debug(f"Shadow audit failed: {e}")

    def _extract_builder(self, extra_data_hex: str) -> str:
        """Extract builder name from block extraData."""
        if not extra_data_hex: return "Unknown"
        try:
            # Common builder tags
            tags = {
                "6265617665726275696c64": "beaverbuild",
                "666c617368626f7473": "flashbots",
                "746974616e": "titan",
                "6275696c64657230783639": "builder0x69",
                "7273796e632d6275696c646572": "rsync-builder",
            }
            extra_lower = extra_data_hex.lower()
            for tag, name in tags.items():
                if tag in extra_lower: return name
                
            # Try to decode as ASCII
            decoded = bytes.fromhex(extra_data_hex.replace("0x", "")).decode('ascii', errors='ignore')
            return decoded.strip() or "Unknown"
        except:
            return "Unknown"

    def _log_shadow(self, tx_hash: str, router: str, builder: str, block: int) -> None:
        """Log private Alpha activity."""
        data = {
            "timestamp": time.time(),
            "block": block,
            "tx_hash": tx_hash,
            "router": router,
            "builder": builder,
            "type": "SHADOW_TRADE"
        }
        with open(self._shadow_path, "a") as f:
            f.write(json.dumps(data) + "\n")
            f.flush()

    async def _verify_actual_swap(self, opp: "PendingOpportunity", tx_hash: str) -> None:
        """
        Instantly verify our simulation by decoding the Swap event natively from the RPC transaction receipt.
        Submits NO graph queries and requires 0 wait time. 
        """
        from eth_abi import decode
        try:
            # Let the RPC index the receipt for safety
            await asyncio.sleep(1)
            
            receipt = await asyncio.get_event_loop().run_in_executor(
                None, self._w3.eth.get_transaction_receipt, tx_hash
            )
            
            if not receipt or "logs" not in receipt:
                return

            V2_SWAP_TOPIC = self._w3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
            V3_SWAP_TOPIC = self._w3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
            
            affected_low = opp.affected_pool.lower()
            actual_in = 0.0
            actual_out = 0.0
            
            for log_entry in receipt["logs"]:
                if log_entry["address"].lower() == affected_low:
                    topics = log_entry["topics"]
                    if not topics: continue
                    topic0 = topics[0].hex()
                    
                    if topic0 == V2_SWAP_TOPIC:
                        # V2: amount0In, amount1In, amount0Out, amount1Out (4 uint256s)
                        a0_in, a1_in, a0_out, a1_out = decode(
                            ["uint256", "uint256", "uint256", "uint256"],
                            bytes(log_entry["data"])
                        )
                        # data_hex = log_entry["data"].hex()
                        # if data_hex.startswith("0x"): data_hex = data_hex[2:]
                        # if len(data_hex) >= 256:
                        #     a0_in = int(data_hex[0:64], 16)
                        #     a1_in = int(data_hex[64:128], 16)
                        #     a0_out = int(data_hex[128:192], 16)
                        #     a1_out = int(data_hex[192:256], 16)
                        actual_in = float(a0_in + a1_in)
                        actual_out = float(a0_out + a1_out)
                        break
                            
                    elif topic0 == V3_SWAP_TOPIC:
                        # V3: amount0, amount1 (2 int256s) - standard signed hex
                        data = bytes(log_entry["data"])
                        a0_val, a1_val, _, _, _ = decode(
                            ["int256", "int256", "uint160", "uint128", "int24"],
                            data
                        )
                        # data_hex = log_entry["data"].hex()
                        # if data_hex.startswith("0x"): data_hex = data_hex[2:]
                        # if len(data_hex) >= 128:
                        #     a0_val = int.from_bytes(bytes.fromhex(data_hex[0:64]), byteorder='big', signed=True)
                        #     a1_val = int.from_bytes(bytes.fromhex(data_hex[64:128]), byteorder='big', signed=True)
                            
                        if a0_val > 0:
                            actual_in = float(a0_val)
                            actual_out = -float(a1_val)
                        else:
                            actual_in = float(a1_val)
                            actual_out = -float(a0_val)
                        break

            if actual_in > 0:
                realized_price = actual_out / actual_in
                sim_price = (opp.reserve_out_after / opp.reserve_in_after) if opp.reserve_in_after > 0 else 0
                
                recon_data = {
                    "timestamp": time.time(),
                    "tx_hash": tx_hash,
                    "sim_price": sim_price,
                    "actual_price": realized_price,
                    "price_diff_pct": abs(sim_price - realized_price) / realized_price * 100 if realized_price > 0 else 0,
                    "type": "RECONCILED_OUTCOME",
                    "opp_type": opp.opp_type
                }
                
                with open(self._log_path, "a") as f:
                    f.write(json.dumps(recon_data) + "\n")
                
                log.research(f"RECONCILED NATIVE | {tx_hash[:10]} | Sim: {sim_price:.6g} | Actual: {realized_price:.6g} | Diff: {recon_data['price_diff_pct']:.2f}%")
        except Exception as e:
            # log.debug(f"Native Reconciliation error for {tx_hash[:10]}: {e}")
            pass

