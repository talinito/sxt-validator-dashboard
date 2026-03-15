#!/usr/bin/env python3
"""
SXT Validator Exporter
Polls the Substrate JSON-RPC of an SXT Chain node and exposes
derived metrics in Prometheus format.

Covers data NOT available from the native :9615 Prometheus endpoint:
  - System health (peers, syncing)
  - Sync state (current vs highest block, finalized lag)
  - Pending extrinsics count
  - BABE epoch authorship (blocks produced by this validator)
  - GRANDPA round state (round number, completeness)
  - SXT attestation data (custom pallet)
  - Runtime version tracking
"""

import json
import logging
import os
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock
from typing import Any, Optional
import struct
import economics

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RPC_URL = os.getenv("SXT_RPC_URL", "http://172.17.0.1:9944")
POLL_INTERVAL = int(os.getenv("SXT_EXPORTER_POLL_INTERVAL", "12"))
LISTEN_PORT = int(os.getenv("SXT_EXPORTER_PORT", "9101"))
LOG_LEVEL = os.getenv("SXT_EXPORTER_LOG_LEVEL", "INFO").upper()
VALIDATOR_NAME = os.getenv("SXT_VALIDATOR_NAME", "unknown")
# ---------------------------------------------------------------------------
# Validator name resolution (from SXT staking dashboard API)
# ---------------------------------------------------------------------------
VALIDATOR_NAMES_URL = os.getenv("SXT_VALIDATOR_NAMES_URL",
                                "https://staking.spaceandtime.io/api/validator")
_validator_names: dict[str, str] = {}
_names_last_fetch = 0.0
NAMES_REFRESH_INTERVAL = 3600  # refresh once per hour


def _fetch_validator_names():
    """Fetch validator name mapping from SXT staking dashboard API."""
    global _validator_names, _names_last_fetch
    now = time.time()
    if now - _names_last_fetch < NAMES_REFRESH_INTERVAL and _validator_names:
        return
    try:
        resp = requests.get(VALIDATOR_NAMES_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("data", [])
        if isinstance(entries, list):
            new_names = {}
            for entry in entries:
                addr = entry.get("id", "")
                name = entry.get("name", "")
                if addr and name:
                    new_names[addr] = name
            if new_names:
                _validator_names = new_names
                _names_last_fetch = now
                log.info("Fetched %d validator names from staking API", len(new_names))
    except Exception:
        log.warning("Failed to fetch validator names from %s", VALIDATOR_NAMES_URL)


def _get_validator_name(full_address: str) -> str:
    """Get validator name by full address, or return short address.
    Prefixes own validator with ★ for easy identification."""
    name = _validator_names.get(full_address, full_address[:8] + ".." + full_address[-6:])
    if name == VALIDATOR_NAME:
        return "★ " + name
    return name



logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sxt_exporter")

# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
_rpc_id = 0
_rpc_lock = Lock()


def rpc_call(method: str, params: list | None = None, timeout: int = 10) -> Optional[Any]:
    """Execute a JSON-RPC call and return the 'result' field, or None on error."""
    global _rpc_id
    with _rpc_lock:
        _rpc_id += 1
        request_id = _rpc_id
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or [],
    }
    try:
        resp = requests.post(RPC_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.warning("RPC error on %s: %s", method, data["error"])
            return None
        return data.get("result")
    except requests.RequestException as exc:
        log.error("RPC call %s failed: %s", method, exc)
        return None


def hex_to_int(hex_str: str) -> int:
    """Convert a hex string (0x...) to int."""
    if isinstance(hex_str, str) and hex_str.startswith("0x"):
        return int(hex_str, 16)
    return int(hex_str)


# ---------------------------------------------------------------------------
# Metric store
# ---------------------------------------------------------------------------
class MetricStore:
    """Thread-safe store for Prometheus metrics."""

    def __init__(self):
        self._lock = Lock()
        self._metrics: dict[str, tuple[str, str, float]] = {}  # name → (help, type, value)
        self._labeled: dict[str, tuple[str, str, list[tuple[dict, float]]]] = {}

    def set(self, name: str, value: float, help_text: str = "", metric_type: str = "gauge"):
        with self._lock:
            self._metrics[name] = (help_text, metric_type, value)

    def set_labeled(self, name: str, labels: dict, value: float,
                    help_text: str = "", metric_type: str = "gauge"):
        with self._lock:
            if name not in self._labeled:
                self._labeled[name] = (help_text, metric_type, [])
            # Replace existing label combo or append
            entries = self._labeled[name][2]
            for i, (existing_labels, _) in enumerate(entries):
                if existing_labels == labels:
                    entries[i] = (labels, value)
                    return
            entries.append((labels, value))

    def get_value(self, name: str, default: float = 0.0) -> float:
        """Read a scalar metric value."""
        with self._lock:
            if name in self._metrics:
                return self._metrics[name][2]
            return default

    def get_labeled_entries(self, name: str) -> list[tuple[dict, float]]:
        """Read all (labels, value) pairs for a labeled metric."""
        with self._lock:
            if name in self._labeled:
                return list(self._labeled[name][2])
            return []

    def clear_labeled(self, name: str):
        """Remove all entries for a labeled metric (stale data cleanup)."""
        with self._lock:
            if name in self._labeled:
                self._labeled[name] = (self._labeled[name][0], self._labeled[name][1], [])

    def render(self) -> str:
        lines = []
        with self._lock:
            for name, (help_text, metric_type, value) in sorted(self._metrics.items()):
                if help_text:
                    lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {metric_type}")
                lines.append(f"{name} {value}")

            for name, (help_text, metric_type, entries) in sorted(self._labeled.items()):
                if help_text:
                    lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {metric_type}")
                for labels, value in entries:
                    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
                    lines.append(f"{name}{{{label_str}}} {value}")

        lines.append("")
        return "\n".join(lines)


store = MetricStore()

# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def collect_system_health():
    """system_health → peers, syncing status."""
    result = rpc_call("system_health")
    if result is None:
        store.set("sxt_rpc_up", 0, "Whether the RPC endpoint is reachable")
        return
    store.set("sxt_rpc_up", 1, "Whether the RPC endpoint is reachable")
    store.set("sxt_peers_count", result.get("peers", 0),
              "Number of connected peers from RPC")
    store.set("sxt_is_syncing", 1 if result.get("isSyncing", False) else 0,
              "Whether the node is currently syncing")
    store.set("sxt_should_have_peers", 1 if result.get("shouldHavePeers", True) else 0,
              "Whether the node expects to have peers")


def collect_sync_state():
    """system_syncState → block heights and lag."""
    result = rpc_call("system_syncState")
    if result is None:
        return
    current = result.get("currentBlock", 0)
    highest = result.get("highestBlock", 0)
    starting = result.get("startingBlock", 0)
    store.set("sxt_sync_current_block", current,
              "Current block number from sync state")
    store.set("sxt_sync_highest_block", highest,
              "Highest known block number")
    store.set("sxt_sync_starting_block", starting,
              "Block number when sync started")
    store.set("sxt_sync_block_lag", highest - current,
              "Block lag (highest - current)")


def collect_chain_header():
    """chain_getHeader + chain_getFinalizedHead → best and finalized block."""
    # Best block
    header = rpc_call("chain_getHeader")
    if header and "number" in header:
        best = hex_to_int(header["number"])
        store.set("sxt_block_height_best", best,
                  "Best block height from chain header")

    # Finalized block
    finalized_hash = rpc_call("chain_getFinalizedHead")
    if finalized_hash:
        fin_header = rpc_call("chain_getHeader", [finalized_hash])
        if fin_header and "number" in fin_header:
            finalized = hex_to_int(fin_header["number"])
            store.set("sxt_block_height_finalized", finalized,
                      "Finalized block height")
            # Finality lag
            if header and "number" in header:
                best = hex_to_int(header["number"])
                store.set("sxt_finality_lag_blocks", best - finalized,
                          "Blocks behind finality (best - finalized)")


def collect_pending_extrinsics():
    """author_pendingExtrinsics → count of pending transactions."""
    result = rpc_call("author_pendingExtrinsics")
    if result is not None:
        store.set("sxt_pending_extrinsics", len(result),
                  "Number of pending extrinsics in the transaction pool")


def collect_babe_epoch_authorship():
    """babe_epochAuthorship → blocks authored by our validator in current epoch."""
    result = rpc_call("babe_epochAuthorship")
    if result is None:
        return
    total_primary = 0
    total_secondary = 0
    total_secondary_vrf = 0
    for _authority, data in result.items():
        primary = len(data.get("primary", []))
        secondary = len(data.get("secondary", []))
        secondary_vrf = len(data.get("secondary_vrf", []))
        total_primary += primary
        total_secondary += secondary
        total_secondary_vrf += secondary_vrf

    store.set("sxt_babe_epoch_primary_slots", total_primary,
              "Primary slots authored in current BABE epoch")
    store.set("sxt_babe_epoch_secondary_slots", total_secondary,
              "Secondary (fallback) slots in current BABE epoch")
    store.set("sxt_babe_epoch_secondary_vrf_slots", total_secondary_vrf,
              "Secondary VRF slots in current BABE epoch")
    store.set("sxt_babe_epoch_total_authored", total_primary + total_secondary + total_secondary_vrf,
              "Total blocks authored in current BABE epoch")


def _safe_get(obj, key, default=0):
    """Safely get from dict; if obj is not a dict, return it or default."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    if isinstance(obj, (int, float)):
        return obj
    return default


def collect_grandpa_state():
    """grandpa_roundState → current round info and vote completeness."""
    result = rpc_call("grandpa_roundState")
    if result is None:
        return
    try:
        best = result.get("best", result) if isinstance(result, dict) else result
        if not isinstance(best, dict):
            return

        store.set("sxt_grandpa_round", best.get("round", 0),
                  "Current GRANDPA round number")

        total_weight = best.get("totalWeight", 0)
        store.set("sxt_grandpa_total_weight_prevote", _safe_get(total_weight, "prevote"),
                  "Total weight of prevotes in current GRANDPA round")
        store.set("sxt_grandpa_total_weight_precommit", _safe_get(total_weight, "precommit"),
                  "Total weight of precommits in current GRANDPA round")

        threshold = best.get("thresholdWeight", 0)
        store.set("sxt_grandpa_threshold_prevote", _safe_get(threshold, "prevote"),
                  "Threshold weight needed for prevote supermajority")
        store.set("sxt_grandpa_threshold_precommit", _safe_get(threshold, "precommit"),
                  "Threshold weight needed for precommit supermajority")

        prevotes = best.get("prevotes", {})
        if isinstance(prevotes, dict):
            store.set("sxt_grandpa_prevotes_current", prevotes.get("currentWeight", 0),
                      "Current prevote weight in GRANDPA round")
            missing = prevotes.get("missing", 0)
            store.set("sxt_grandpa_prevotes_missing",
                      missing if isinstance(missing, (int, float)) else len(missing),
                      "Missing prevotes in GRANDPA round")

        precommits = best.get("precommits", {})
        if isinstance(precommits, dict):
            store.set("sxt_grandpa_precommits_current", precommits.get("currentWeight", 0),
                      "Current precommit weight in GRANDPA round")
            missing = precommits.get("missing", 0)
            store.set("sxt_grandpa_precommits_missing",
                      missing if isinstance(missing, (int, float)) else len(missing),
                      "Missing precommits in GRANDPA round")
    except Exception:
        log.exception("Failed to parse grandpa_roundState")


def collect_attestations():
    """attestation_v1_bestRecentAttestations → SXT-specific attestation metrics."""
    result = rpc_call("attestation_v1_bestRecentAttestations")
    if result is None:
        store.set("sxt_attestations_available", 0,
                  "Whether attestation data is available")
        return
    store.set("sxt_attestations_available", 1,
              "Whether attestation data is available")
    if isinstance(result, list):
        store.set("sxt_attestations_recent_count", len(result),
                  "Number of recent attestations")
        # Extract per-attestation details if structure allows
        for i, att in enumerate(result):
            if isinstance(att, dict):
                if "block_number" in att or "blockNumber" in att:
                    block = att.get("block_number", att.get("blockNumber", 0))
                    store.set_labeled("sxt_attestation_block", {"index": str(i)}, float(block),
                                     "Block number of recent attestation")
    elif isinstance(result, dict):
        # Single attestation or wrapped response
        store.set("sxt_attestations_recent_count", 1,
                  "Number of recent attestations")


def collect_runtime_version():
    """state_getRuntimeVersion → track spec version changes."""
    result = rpc_call("state_getRuntimeVersion")
    if result is None:
        return
    store.set("sxt_runtime_spec_version", result.get("specVersion", 0),
              "Runtime specification version")
    store.set("sxt_runtime_impl_version", result.get("implVersion", 0),
              "Runtime implementation version")
    store.set("sxt_runtime_transaction_version", result.get("transactionVersion", 0),
              "Runtime transaction version")


def collect_system_info():
    """One-time system info exposed as info metric."""
    version = rpc_call("system_version")
    chain = rpc_call("system_chain")
    peer_id = rpc_call("system_localPeerId")

    if version:
        store.set_labeled("sxt_node_info", {
            "version": str(version),
            "chain": str(chain or "unknown"),
            "peer_id": str(peer_id or "unknown")[:16],
            "validator_name": VALIDATOR_NAME,
        }, 1, "SXT node information", "gauge")


def collect_block_stats():
    """dev_getBlockStats → per-block stats if available."""
    # Get latest finalized hash for stats
    fin_hash = rpc_call("chain_getFinalizedHead")
    if fin_hash is None:
        return
    result = rpc_call("dev_getBlockStats", [fin_hash])
    if result is None:
        return
    if isinstance(result, dict):
        for key in ("witness_len", "witness_compact_len", "block_len",
                    "num_extrinsics"):
            if key in result:
                store.set(f"sxt_block_stats_{key}", float(result[key]),
                          f"Block stats: {key}")


# ---------------------------------------------------------------------------
# SCALE codec helpers
# ---------------------------------------------------------------------------
def decode_compact(data: bytes, offset: int) -> tuple[int, int]:
    """Decode SCALE compact integer, return (value, new_offset)."""
    b0 = data[offset]
    mode = b0 & 0x03
    if mode == 0:
        return b0 >> 2, offset + 1
    elif mode == 1:
        val = int.from_bytes(data[offset:offset + 2], "little") >> 2
        return val, offset + 2
    elif mode == 2:
        val = int.from_bytes(data[offset:offset + 4], "little") >> 2
        return val, offset + 4
    else:
        nb = (b0 >> 2) + 4
        val = int.from_bytes(data[offset + 1:offset + 1 + nb], "little")
        return val, offset + 1 + nb


def state_call(api_method: str, params_hex: str = "") -> Optional[bytes]:
    """Call a runtime API and return decoded bytes, or None."""
    result = rpc_call("state_call", [api_method, params_hex])
    if result and isinstance(result, str) and result.startswith("0x"):
        return bytes.fromhex(result[2:])
    return None


# ---------------------------------------------------------------------------
# Network-wide collectors
# ---------------------------------------------------------------------------

def collect_babe_epoch():
    """BabeApi_current_epoch → active validator count, epoch info, authority keys."""
    data = state_call("BabeApi_current_epoch")
    if data is None or len(data) < 24:
        return
    try:
        offset = 0
        epoch_index = struct.unpack_from("<Q", data, offset)[0]; offset += 8
        start_slot = struct.unpack_from("<Q", data, offset)[0]; offset += 8
        duration = struct.unpack_from("<Q", data, offset)[0]; offset += 8

        store.set("sxt_network_babe_epoch_index", epoch_index,
                  "Current BABE epoch index")
        store.set("sxt_network_babe_epoch_duration_slots", duration,
                  "Slots per BABE epoch")

        # Decode Vec<(AuthorityId[32], BabeWeight[u64])>
        n_auth, offset = decode_compact(data, offset)
        store.set("sxt_network_active_validators", n_auth,
                  "Number of validators in the active BABE authority set")

        # Emit per-authority metric with truncated key as label
        for i in range(n_auth):
            if offset + 40 > len(data):
                break
            pub_key = data[offset:offset + 32].hex()
            offset += 32
            weight = struct.unpack_from("<Q", data, offset)[0]; offset += 8
            short_key = pub_key[:12] + ".." + pub_key[-8:]
            store.set_labeled("sxt_network_babe_authority",
                              {"index": str(i), "pubkey": short_key},
                              float(weight),
                              "BABE authority weight", "gauge")

        log.debug("BABE epoch %d: %d authorities, %d slots/epoch", epoch_index, n_auth, duration)

        # Compute epoch progress from Babe.CurrentSlot storage
        try:
            import xxhash
            def _twox128(s):
                d = s.encode() if isinstance(s, str) else s
                h0 = xxhash.xxh64(d, seed=0).intdigest().to_bytes(8, 'little')
                h1 = xxhash.xxh64(d, seed=1).intdigest().to_bytes(8, 'little')
                return h0 + h1

            slot_key = "0x" + _twox128("Babe").hex() + _twox128("CurrentSlot").hex()
            slot_raw = rpc_call("state_getStorage", [slot_key])
            if slot_raw and len(slot_raw) > 4:
                current_slot = int.from_bytes(bytes.fromhex(slot_raw[2:]), "little")
                slot_in_epoch = current_slot - start_slot
                progress = min(1.0, max(0.0, slot_in_epoch / duration)) if duration > 0 else 0
                store.set("sxt_network_babe_epoch_progress", round(progress, 4),
                          "BABE epoch progress 0.0 to 1.0")
                store.set("sxt_network_babe_current_slot", current_slot,
                          "Current BABE slot number")
        except Exception:
            log.debug("Could not compute epoch progress")
    except Exception:
        log.exception("Failed to decode BABE epoch")


def collect_grandpa_authorities():
    """GrandpaApi_grandpa_authorities → GRANDPA authority count."""
    data = state_call("GrandpaApi_grandpa_authorities")
    if data is None:
        return
    try:
        n_auth, _ = decode_compact(data, 0)
        store.set("sxt_network_grandpa_authorities", n_auth,
                  "Number of GRANDPA authorities")
    except Exception:
        log.exception("Failed to decode GRANDPA authorities")


def collect_authority_discovery():
    """AuthorityDiscoveryApi_authorities → count of discoverable authorities."""
    data = state_call("AuthorityDiscoveryApi_authorities")
    if data is None:
        return
    try:
        n_auth, _ = decode_compact(data, 0)
        store.set("sxt_network_authority_discovery_count", n_auth,
                  "Number of authorities in discovery set")
    except Exception:
        log.exception("Failed to decode authority discovery")


def collect_peers_detail():
    """system_peers → per-peer role, best block, version; aggregate by role."""
    result = rpc_call("system_peers")
    if result is None:
        return

    authority_count = 0
    full_count = 0
    light_count = 0
    best_blocks = []

    for p in result:
        roles = p.get("roles", "UNKNOWN").upper()
        peer_id_short = str(p.get("peerId", ""))[:16]
        best_number = p.get("bestNumber", 0)
        best_blocks.append(best_number)

        if "AUTHORITY" in roles:
            authority_count += 1
        elif "FULL" in roles:
            full_count += 1
        else:
            light_count += 1

        store.set_labeled("sxt_peer_best_block",
                          {"peer_id": peer_id_short, "roles": roles},
                          float(best_number),
                          "Best block reported by each connected peer", "gauge")

    store.set("sxt_peers_total", len(result),
              "Total connected peers")
    store.set("sxt_peers_authority", authority_count,
              "Connected peers with AUTHORITY role")
    store.set("sxt_peers_full", full_count,
              "Connected peers with FULL node role")

    # Detect peer block lag relative to our best
    if best_blocks:
        our_best = max(best_blocks)
        lagging = sum(1 for b in best_blocks if our_best - b > 5)
        store.set("sxt_peers_lagging", lagging,
                  "Peers more than 5 blocks behind best")
        store.set("sxt_peers_best_block_max", our_best,
                  "Highest best block among all peers")
        store.set("sxt_peers_best_block_min", min(best_blocks),
                  "Lowest best block among all peers")


def collect_active_era():
    """Read Staking.ActiveEra from storage → current era index."""
    # Twox128("Staking") + Twox128("ActiveEra")
    key = "0x5f3e4907f716ac89b6347d15ececedca487df464e44a534ba6b0cbb32407b587"
    result = rpc_call("state_getStorage", [key])
    if result and isinstance(result, str) and len(result) > 10:
        try:
            data = bytes.fromhex(result[2:])
            era = int.from_bytes(data[:4], "little")
            store.set("sxt_network_active_era", era,
                      "Current active staking era")
        except Exception:
            log.exception("Failed to decode ActiveEra")


# ---------------------------------------------------------------------------
# Staking deep collector (substrate-interface, runs on slow interval)
# ---------------------------------------------------------------------------
_substrate = None
_staking_last_run = 0.0
STAKING_POLL_INTERVAL = int(os.getenv("SXT_STAKING_POLL_INTERVAL", "120"))


def _get_substrate():
    """Lazy-init substrate-interface connection."""
    global _substrate
    if _substrate is None:
        try:
            from substrateinterface import SubstrateInterface
            ws_url = RPC_URL.replace("http://", "ws://").replace("https://", "wss://")
            _substrate = SubstrateInterface(url=ws_url, auto_reconnect=True)
            log.info("substrate-interface connected to %s", ws_url)
        except Exception:
            log.exception("Failed to init substrate-interface")
    return _substrate


def collect_staking_deep():
    """Full staking data: validators, stake, commission, nominators, rewards.

    Runs every STAKING_POLL_INTERVAL seconds (default 120s) since era data
    changes slowly. Emits per-validator labeled metrics.
    """
    global _staking_last_run
    now = time.monotonic()
    if now - _staking_last_run < STAKING_POLL_INTERVAL:
        return
    _staking_last_run = now

    sub = _get_substrate()
    if sub is None:
        return

    t0 = time.monotonic()

    try:
        # ---- Network totals ----
        validator_count = sub.query("Staking", "ValidatorCount")
        counter_validators = sub.query("Staking", "CounterForValidators")
        counter_nominators = sub.query("Staking", "CounterForNominators")
        current_era_raw = sub.query("Staking", "CurrentEra")
        active_era_raw = sub.query("Staking", "ActiveEra")
        min_active_stake = sub.query("Staking", "MinimumActiveStake")

        target_count = validator_count.value if validator_count else 0
        total_registered = counter_validators.value if counter_validators else 0
        total_nominators = counter_nominators.value if counter_nominators else 0
        current_era = current_era_raw.value if current_era_raw else 0
        active_era = active_era_raw.value["index"] if active_era_raw and active_era_raw.value else 0

        store.set("sxt_staking_target_validator_count", target_count,
                  "Target number of active validators")
        store.set("sxt_staking_registered_validators", total_registered,
                  "Total registered validators (active + waiting)")
        store.set("sxt_staking_waiting_validators", max(0, total_registered - target_count),
                  "Validators in waiting queue")
        store.set("sxt_staking_total_nominators", total_nominators,
                  "Total number of nominators in the network")
        store.set("sxt_staking_current_era", current_era,
                  "Current staking era")
        if min_active_stake and min_active_stake.value:
            store.set("sxt_staking_min_active_stake", min_active_stake.value / 1e18,
                      "Minimum active stake in SXT")

        # ---- Era total stake ----
        try:
            era_total = sub.query("Staking", "ErasTotalStake", [active_era])
            if era_total and era_total.value:
                store.set("sxt_staking_era_total_stake", era_total.value / 1e18,
                          "Total stake in active era (SXT)")
        except Exception:
            pass

        # ---- Era progress ----
        try:
            planned_session = sub.query("Staking", "CurrentPlannedSession")
            era_start_session = sub.query("Staking", "ErasStartSessionIndex", [active_era])
            prev_era_start = sub.query("Staking", "ErasStartSessionIndex", [active_era - 1])
            if planned_session and era_start_session and prev_era_start:
                sessions_per_era = era_start_session.value - prev_era_start.value
                sessions_elapsed = planned_session.value - era_start_session.value
                if sessions_per_era > 0:
                    era_progress = min(1.0, max(0.0, sessions_elapsed / sessions_per_era))
                    store.set("sxt_staking_era_progress", round(era_progress, 4),
                              "Era progress 0.0 to 1.0")
                    store.set("sxt_staking_sessions_per_era", sessions_per_era,
                              "Number of sessions per era")
        except Exception:
            log.debug("Could not compute era progress")

        # ---- Era reward ----
        try:
            era_reward = sub.query("Staking", "ErasValidatorReward", [active_era - 1])
            if era_reward and era_reward.value:
                store.set("sxt_staking_last_era_reward", era_reward.value / 1e18,
                          "Total validator reward for last completed era (SXT)")
        except Exception:
            pass

        # ---- Active validators: Session.Validators ----
        session_vals = sub.query("Session", "Validators")
        active_addrs = []
        if session_vals and session_vals.value:
            active_addrs = [str(v) for v in session_vals.value]

        # ---- Per-validator metrics ----
        # 1. Enumerate ALL registered validators via Staking.Validators map
        all_validators = {}
        try:
            val_map = sub.query_map("Staking", "Validators", max_results=300)
            for key, val in val_map:
                addr = str(key)
                commission_perbill = val.value.get("commission", 0) if isinstance(val.value, dict) else 0
                commission_pct = commission_perbill / 1e7  # Perbill to percent
                blocked = val.value.get("blocked", False) if isinstance(val.value, dict) else False
                is_active = addr in active_addrs
                all_validators[addr] = {
                    "commission": commission_pct,
                    "blocked": blocked,
                    "active": is_active,
                }
        except Exception:
            log.exception("Failed to enumerate Staking.Validators map")

        # 2. Get stake per active validator from ErasStakersOverview
        # Clear stale per-validator metrics before repopulating
        for metric_name in ["sxt_validator_total_stake", "sxt_validator_own_stake",
                           "sxt_validator_nominator_count", "sxt_validator_commission",
                           "sxt_validator_active", "sxt_validator_era_points"]:
            store.clear_labeled(metric_name)

        _fetch_validator_names()  # Fetch once before iterating validators
        for addr in active_addrs:
            try:
                overview = sub.query("Staking", "ErasStakersOverview", [active_era, addr])
                if overview and overview.value:
                    total = overview.value.get("total", 0) / 1e18
                    own = overview.value.get("own", 0) / 1e18
                    nominator_count = overview.value.get("nominator_count", 0)
                    page_count = overview.value.get("page_count", 0)
                else:
                    # Fallback to ErasStakers
                    stakers = sub.query("Staking", "ErasStakers", [active_era, addr])
                    if stakers and stakers.value:
                        total = stakers.value.get("total", 0) / 1e18
                        own = stakers.value.get("own", 0) / 1e18
                        others = stakers.value.get("others", [])
                        nominator_count = len(others)
                    else:
                        total = own = 0
                        nominator_count = 0
                    page_count = 0

                vname = _get_validator_name(addr)
                commission = all_validators.get(addr, {}).get("commission", 0)

                store.set_labeled("sxt_validator_total_stake",
                                  {"address": vname},
                                  total,
                                  "Total stake backing this validator (SXT)", "gauge")
                store.set_labeled("sxt_validator_own_stake",
                                  {"address": vname},
                                  own,
                                  "Validator's own stake (SXT)", "gauge")
                store.set_labeled("sxt_validator_nominator_count",
                                  {"address": vname},
                                  float(nominator_count),
                                  "Number of nominators for this validator", "gauge")
                store.set_labeled("sxt_validator_commission",
                                  {"address": vname},
                                  commission,
                                  "Validator commission (%)", "gauge")
                store.set_labeled("sxt_validator_active",
                                  {"address": vname},
                                  1.0,
                                  "Whether validator is in the active set", "gauge")

            except Exception:
                log.debug("Failed staking query for %s", addr[:16])

        # 3. Waiting validators
        for addr, info in all_validators.items():
            if not info["active"]:
                vname = _get_validator_name(addr)
                store.set_labeled("sxt_validator_active",
                                  {"address": vname},
                                  0.0,
                                  "Whether validator is in the active set", "gauge")
                store.set_labeled("sxt_validator_commission",
                                  {"address": vname},
                                  info["commission"],
                                  "Validator commission (%)", "gauge")

        # 4. Era reward points for current era
        try:
            era_points = sub.query("Staking", "ErasRewardPoints", [active_era])
            if era_points and era_points.value:
                total_points = era_points.value.get("total", 0)
                store.set("sxt_staking_era_total_reward_points", total_points,
                          "Total reward points in current era")
                individual = era_points.value.get("individual", [])
                if isinstance(individual, list):
                    for entry in individual:
                        if isinstance(entry, (list, tuple)) and len(entry) == 2:
                            vaddr = str(entry[0])
                            pts = int(entry[1])
                            vname_pts = _get_validator_name(vaddr)
                            store.set_labeled("sxt_validator_era_points",
                                              {"address": vname_pts},
                                              float(pts),
                                              "Reward points earned in current era", "gauge")
                elif isinstance(individual, dict):
                    for vaddr, pts in individual.items():
                        vname_pts = _get_validator_name(str(vaddr))
                        store.set_labeled("sxt_validator_era_points",
                                          {"address": vname_pts},
                                          float(pts),
                                          "Reward points earned in current era", "gauge")
        except Exception:
            log.debug("Failed to get ErasRewardPoints")

        elapsed = time.monotonic() - t0
        store.set("sxt_staking_scrape_duration_seconds", round(elapsed, 4),
                  "Time taken for staking deep scrape")
        log.info("Staking deep collection: %d active, %d waiting, %d nominators in %.2fs",
                 len(active_addrs), max(0, total_registered - target_count),
                 total_nominators, elapsed)

    except Exception:
        log.exception("Failed staking deep collection")


def collect_all():
    """Run all collectors."""
    t0 = time.monotonic()
    # Node-level metrics
    collect_system_health()
    collect_sync_state()
    collect_chain_header()
    collect_pending_extrinsics()
    collect_babe_epoch_authorship()
    collect_grandpa_state()
    collect_attestations()
    collect_runtime_version()
    collect_block_stats()
    # Network-wide metrics (lightweight, every cycle)
    collect_babe_epoch()
    collect_grandpa_authorities()
    collect_authority_discovery()
    collect_peers_detail()
    collect_active_era()
    # Token price (CoinGecko)
    economics.collect_token_price(store)
    # Deep staking data (heavy, runs on own interval)
    collect_staking_deep()
    # Economic data: USD metrics + ClickHouse writes
    economics.post_staking_hook(store)
    elapsed = time.monotonic() - t0
    store.set("sxt_exporter_scrape_duration_seconds", round(elapsed, 4),
              "Time taken for the last full scrape cycle")
    store.set("sxt_exporter_scrape_timestamp", time.time(),
              "Unix timestamp of the last scrape")
    log.info("Collection complete in %.3fs", elapsed)


# Collect system info once at startup
def collect_static():
    collect_system_info()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = store.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default access logs
        pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
shutdown = False


def signal_handler(sig, frame):
    global shutdown
    log.info("Received signal %s, shutting down...", sig)
    shutdown = True


def polling_loop():
    collect_static()
    while not shutdown:
        try:
            collect_all()
        except Exception:
            log.exception("Unexpected error in collection cycle")
        for _ in range(POLL_INTERVAL * 10):
            if shutdown:
                break
            time.sleep(0.1)


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log.info("SXT Exporter starting")
    log.info("  RPC URL:        %s", RPC_URL)
    log.info("  Poll interval:  %ds", POLL_INTERVAL)
    log.info("  Listen port:    %d", LISTEN_PORT)
    log.info("  Validator name: %s", VALIDATOR_NAME)

    # Start polling thread
    poller = Thread(target=polling_loop, daemon=True)
    poller.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), MetricsHandler)
    server.timeout = 1
    log.info("Serving metrics on :%d/metrics", LISTEN_PORT)

    while not shutdown:
        server.handle_request()

    log.info("Exporter stopped")


if __name__ == "__main__":
    main()
