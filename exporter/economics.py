#!/usr/bin/env python3
"""
SXT Economic Data Collector
- Token price from CoinGecko (free API, no key required)
- ClickHouse writer for historical era rewards, price, delegation snapshots
- Reads validator data from the shared MetricStore (no changes to main collectors)
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("sxt_exporter.economics")

VALIDATOR_NAME = os.getenv("SXT_VALIDATOR_NAME", "unknown")
RPC_URL = os.getenv("SXT_RPC_URL", "http://172.17.0.1:9944")
VALIDATOR_NAMES_URL = os.getenv("SXT_VALIDATOR_NAMES_URL",
                                "https://staking.spaceandtime.io/api/validator")

# ---------------------------------------------------------------------------
# Validator address resolution
# ---------------------------------------------------------------------------
_own_address: str = ""
_address_last_resolve = 0.0
ADDRESS_RESOLVE_INTERVAL = 3600  # 1 hour


def _resolve_own_address() -> str:
    """Resolve VALIDATOR_NAME from .env to on-chain stash address."""
    global _own_address, _address_last_resolve
    now = time.time()
    if _own_address and now - _address_last_resolve < ADDRESS_RESOLVE_INTERVAL:
        return _own_address
    try:
        resp = requests.get(VALIDATOR_NAMES_URL, timeout=10)
        resp.raise_for_status()
        entries = resp.json().get("data", [])
        for entry in entries:
            if entry.get("name", "") == VALIDATOR_NAME:
                _own_address = entry.get("id", "")
                _address_last_resolve = now
                log.info("Resolved '%s' -> %s", VALIDATOR_NAME, _own_address[:16])
                return _own_address
        log.warning("Validator name '%s' not found in staking API (%d validators checked)",
                    VALIDATOR_NAME, len(entries))
    except Exception:
        log.warning("Failed to resolve validator address from staking API")
    return _own_address


# ---------------------------------------------------------------------------
# Era timestamp calculator
# ---------------------------------------------------------------------------
_era_start_cache: dict[str, int] = {}


def _get_era_timestamp(sub, era: int) -> str:
    """Calculate the real timestamp for a given era using ActiveEra start time."""
    cache_key = "active_era_start"
    if cache_key not in _era_start_cache:
        try:
            ae = sub.query("Staking", "ActiveEra")
            if ae and ae.value:
                _era_start_cache["current_era"] = ae.value["index"]
                _era_start_cache[cache_key] = ae.value.get("start", 0)
                if isinstance(_era_start_cache[cache_key], int) and _era_start_cache[cache_key] > 1e12:
                    _era_start_cache[cache_key] = _era_start_cache[cache_key] // 1000
        except Exception:
            pass

    current_era = _era_start_cache.get("current_era", 0)
    era_start = _era_start_cache.get(cache_key, 0)
    if era_start > 0 and current_era > 0:
        era_ts = era_start - ((current_era - era) * 86400)
        return datetime.fromtimestamp(era_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Pending rewards collector
# ---------------------------------------------------------------------------
_substrate_econ = None


def _get_substrate():
    global _substrate_econ
    if _substrate_econ is None:
        try:
            from substrateinterface import SubstrateInterface
            ws_url = RPC_URL.replace("http://", "ws://").replace("https://", "wss://")
            _substrate_econ = SubstrateInterface(url=ws_url, auto_reconnect=True)
            log.info("economics substrate-interface connected to %s", ws_url)
        except Exception:
            log.exception("Failed to init economics substrate-interface")
    return _substrate_econ


def collect_pending_rewards(store) -> None:
    """Query Staking.Ledger for own validator, compute pending reward eras."""
    address = _resolve_own_address()

    # Emit name match metric
    if address:
        store.set("sxt_validator_name_resolved", 1,
                  "Whether VALIDATOR_NAME from .env matches an on-chain validator (1=yes, 0=no)")
    else:
        store.set("sxt_validator_name_resolved", 0,
                  "Whether VALIDATOR_NAME from .env matches an on-chain validator (1=yes, 0=no)")
        return

    sub = _get_substrate()
    if sub is None:
        return

    try:
        active_era = int(store.get_value("sxt_staking_current_era", 0))
        if active_era <= 0:
            return

        # Get history depth (how many eras of rewards are available)
        history_depth_raw = None
        try:
            history_depth_raw = sub.query("Staking", "HistoryDepth")
        except Exception:
            pass  # Not a storage item in newer Substrate
        history_depth = history_depth_raw.value if history_depth_raw else 84

        # Query ledger for claimed rewards
        ledger = sub.query("Staking", "Ledger", [address])
        if not ledger or not ledger.value:
            log.debug("No ledger found for %s", address[:16])
            return

        ledger_data = ledger.value
        claimed = set()

        # Try legacy_claimed_rewards first (older Substrate)
        legacy = ledger_data.get("legacy_claimed_rewards", [])
        if legacy:
            claimed.update(int(e) for e in legacy)

        # Also check ClaimedRewards storage (newer Substrate, per-era per-validator)
        oldest_available = max(0, active_era - history_depth)
        for era in range(oldest_available, active_era):
            if era in claimed:
                continue
            try:
                cr = sub.query("Staking", "ClaimedRewards", [era, address])
                if cr and cr.value:
                    claimed.add(era)
            except Exception:
                pass  # storage item might not exist on this chain

        # Compute pending
        available_eras = set(range(oldest_available, active_era))
        pending_eras = available_eras - claimed
        claimed_count = len(claimed & available_eras)
        pending_count = len(pending_eras)

        store.set("sxt_validator_rewards_claimed_eras", claimed_count,
                  "Number of eras with claimed rewards (within history depth)")
        store.set("sxt_validator_rewards_pending_eras", pending_count,
                  "Number of eras with unclaimed rewards")
        store.set("sxt_validator_rewards_history_depth", history_depth,
                  "Staking history depth (eras)")

        if pending_count > 0:
            log.info("Pending rewards: %d eras unclaimed (claimed: %d, range: %d-%d)",
                     pending_count, claimed_count, oldest_available, active_era - 1)

        # Estimate pending SXT
        era_reward = store.get_value("sxt_staking_last_era_reward", 0)
        era_total_points = store.get_value("sxt_staking_era_total_reward_points", 0)
        own_points = 0
        for labels, pts in store.get_labeled_entries("sxt_validator_era_points"):
            if VALIDATOR_NAME in labels.get("address", ""):
                own_points = pts
                break

        if era_total_points > 0 and own_points > 0 and era_reward > 0:
            reward_per_era = era_reward * (own_points / era_total_points)
            pending_sxt = reward_per_era * pending_count
            store.set("sxt_validator_rewards_pending_sxt", round(pending_sxt, 4),
                      "Estimated unclaimed rewards (SXT)")
            price = get_current_price_usd()
            if price > 0:
                store.set("sxt_validator_rewards_pending_usd", round(pending_sxt * price, 2),
                          "Estimated unclaimed rewards (USD)")

    except Exception:
        log.exception("Failed to collect pending rewards")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_ID = "space-and-time"
PRICE_POLL_INTERVAL = int(os.getenv("SXT_PRICE_POLL_INTERVAL", "300"))

CLICKHOUSE_HOST = os.getenv("SXT_CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.getenv("SXT_CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DB = os.getenv("SXT_CLICKHOUSE_DB", "sxt")
CLICKHOUSE_ENABLED = os.getenv("SXT_CLICKHOUSE_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_price_last_fetch = 0.0
_ch_last_era_written = -1
_prev_stakes: dict[str, float] = {}

_current_price = {
    "usd": 0.0,
    "eur": 0.0,
    "market_cap_usd": 0.0,
    "volume_24h_usd": 0.0,
    "change_24h_pct": 0.0,
}


def get_current_price_usd() -> float:
    return _current_price["usd"]


# ---------------------------------------------------------------------------
# CoinGecko price collector
# ---------------------------------------------------------------------------
def collect_token_price(store) -> None:
    """Fetch SXT price from CoinGecko, emit Prometheus metrics, write to CH."""
    global _price_last_fetch, _current_price
    now = time.time()
    if now - _price_last_fetch < PRICE_POLL_INTERVAL:
        return
    _price_last_fetch = now

    try:
        resp = requests.get(
            COINGECKO_URL,
            params={
                "ids": COINGECKO_ID,
                "vs_currencies": "usd,eur",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get(COINGECKO_ID, {})
        if not data:
            log.warning("CoinGecko returned empty data for %s", COINGECKO_ID)
            return

        _current_price["usd"] = data.get("usd", 0.0)
        _current_price["eur"] = data.get("eur", 0.0)
        _current_price["market_cap_usd"] = data.get("usd_market_cap", 0.0)
        _current_price["volume_24h_usd"] = data.get("usd_24h_vol", 0.0)
        _current_price["change_24h_pct"] = data.get("usd_24h_change", 0.0)

        store.set("sxt_token_price_usd", _current_price["usd"],
                  "SXT token price in USD (CoinGecko)")
        store.set("sxt_token_price_eur", _current_price["eur"],
                  "SXT token price in EUR (CoinGecko)")
        store.set("sxt_token_market_cap_usd", _current_price["market_cap_usd"],
                  "SXT token market cap in USD")
        store.set("sxt_token_volume_24h_usd", _current_price["volume_24h_usd"],
                  "SXT 24h trading volume in USD")
        store.set("sxt_token_price_change_24h_pct", _current_price["change_24h_pct"],
                  "SXT price change in last 24h (%)")

        log.info("Token price: $%.6f (24h: %.2f%%)",
                 _current_price["usd"], _current_price["change_24h_pct"])

        if CLICKHOUSE_ENABLED:
            _ch_insert_price()

    except requests.RequestException as exc:
        log.warning("CoinGecko price fetch failed: %s", exc)
    except Exception:
        log.exception("Unexpected error in token price collector")


# ---------------------------------------------------------------------------
# ClickHouse HTTP client
# ---------------------------------------------------------------------------
def _ch_query(query: str, data: str = "") -> Optional[str]:
    if not CLICKHOUSE_ENABLED:
        return None
    try:
        url = f"http://{CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}/"
        params = {"database": CLICKHOUSE_DB, "query": query}
        if data:
            resp = requests.post(url, params=params, data=data, timeout=10)
        else:
            resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.text.strip()
    except requests.RequestException as exc:
        log.warning("ClickHouse query failed: %s — %s", exc, query[:100])
        return None
    except Exception:
        log.exception("Unexpected ClickHouse error")
        return None


def ch_health_check() -> bool:
    if not CLICKHOUSE_ENABLED:
        return False
    try:
        resp = requests.get(
            f"http://{CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}/ping", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def _ch_insert_price() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    row = (
        f"{now}\t{_current_price['usd']}\t{_current_price['eur']}\t"
        f"{_current_price['market_cap_usd']}\t{_current_price['volume_24h_usd']}\t"
        f"{_current_price['change_24h_pct']}"
    )
    _ch_query("INSERT INTO price_history FORMAT TabSeparated", row + "\n")


# ---------------------------------------------------------------------------
# Commission & yield calculator (runs once per era change)
# ---------------------------------------------------------------------------
_earnings_last_era = -1
ERAS_PER_MONTH = 30  # 1 era = 24h on SXT Chain


def collect_earnings(store) -> None:
    """Calculate commission earned and own-stake yield across available eras."""
    global _earnings_last_era

    era = int(store.get_value("sxt_staking_current_era", 0))
    if era <= 0 or era == _earnings_last_era:
        return
    _earnings_last_era = era

    address = _resolve_own_address()
    if not address:
        return

    sub = _get_substrate()
    if sub is None:
        return

    t0 = time.time()
    total_commission = 0.0
    total_own_yield = 0.0
    eras_counted = 0
    last_30_commission = 0.0
    last_30_own_yield = 0.0

    try:
        for check_era in range(max(0, era - 84), era):
            try:
                era_reward_raw = sub.query("Staking", "ErasValidatorReward", [check_era])
                era_points_raw = sub.query("Staking", "ErasRewardPoints", [check_era])
                overview = sub.query("Staking", "ErasStakersOverview", [check_era, address])
                prefs = sub.query("Staking", "ErasValidatorPrefs", [check_era, address])
                if not all([era_reward_raw, era_points_raw, overview]):
                    continue
                if not era_reward_raw.value or not era_points_raw.value or not overview.value:
                    continue

                era_reward = era_reward_raw.value / 1e18
                total_points = era_points_raw.value.get("total", 0)
                individual = era_points_raw.value.get("individual", [])
                my_points = 0
                if isinstance(individual, list):
                    for entry in individual:
                        if isinstance(entry, (list, tuple)) and len(entry) == 2 and str(entry[0]) == address:
                            my_points = int(entry[1])
                elif isinstance(individual, dict):
                    my_points = int(individual.get(address, 0))
                if total_points == 0 or my_points == 0:
                    continue

                val_reward = era_reward * (my_points / total_points)
                total_stake = overview.value.get("total", 0) / 1e18
                own_stake = overview.value.get("own", 0) / 1e18
                delegated = total_stake - own_stake
                commission_rate = prefs.value.get("commission", 0) / 1e9 if prefs and prefs.value else 0.10

                if total_stake > 0:
                    comm = val_reward * (delegated / total_stake) * commission_rate
                    own_y = val_reward * (own_stake / total_stake)
                    total_commission += comm
                    total_own_yield += own_y
                    eras_counted += 1

                    # Last 30 eras = last month
                    if check_era >= era - 30:
                        last_30_commission += comm
                        last_30_own_yield += own_y

                    # Write per-era breakdown to ClickHouse
                    if CLICKHOUSE_ENABLED:
                        now = _get_era_timestamp(sub, check_era)
                        _ch_query(
                            "INSERT INTO validator_earnings FORMAT TabSeparated",
                            f"{check_era}\t{comm}\t{own_y}\t{comm + own_y}\t"
                            f"{val_reward}\t{commission_rate}\t{own_stake}\t"
                            f"{delegated}\t{total_stake}\t{get_current_price_usd()}\t{now}\n"
                        )
            except Exception:
                continue

        if eras_counted > 0:
            price = get_current_price_usd()

            store.set("sxt_validator_commission_earned_84", round(total_commission, 4),
                      "Commission earned over last 84 eras (SXT)")
            store.set("sxt_validator_own_yield_84", round(total_own_yield, 4),
                      "Own-stake yield over last 84 eras (SXT)")
            store.set("sxt_validator_total_earned_84", round(total_commission + total_own_yield, 4),
                      "Total validator operator earnings over last 84 eras (SXT)")
            store.set("sxt_validator_monthly_commission", round(last_30_commission, 4),
                      "Commission earned in last 30 eras / 1 month (SXT)")
            store.set("sxt_validator_monthly_own_yield", round(last_30_own_yield, 4),
                      "Own-stake yield in last 30 eras / 1 month (SXT)")
            store.set("sxt_validator_monthly_total", round(last_30_commission + last_30_own_yield, 4),
                      "Total operator earnings in last 30 eras / 1 month (SXT)")

            if price > 0:
                store.set("sxt_validator_monthly_commission_usd", round(last_30_commission * price, 2),
                          "Commission earned in last month (USD)")
                store.set("sxt_validator_monthly_total_usd", round((last_30_commission + last_30_own_yield) * price, 2),
                          "Total operator earnings in last month (USD)")

            elapsed = time.time() - t0
            log.info("Earnings calc: %d eras, commission=%.2f own=%.2f total=%.2f SXT (%.1fs)",
                     eras_counted, total_commission, total_own_yield,
                     total_commission + total_own_yield, elapsed)

    except Exception:
        log.exception("Failed earnings calculation")


# ---------------------------------------------------------------------------
# Post-staking hook: read MetricStore -> write to ClickHouse + USD metrics
# ---------------------------------------------------------------------------
def post_staking_hook(store) -> None:
    """Called after collect_staking_deep. Reads metrics from store,
    computes USD values, writes historical data to ClickHouse."""
    global _ch_last_era_written, _prev_stakes

    price = get_current_price_usd()

    # --- Extract current era from store ---
    era = int(store.get_value("sxt_staking_current_era", 0))
    if era <= 0:
        return

    # --- USD metrics (always, even without CH) ---
    era_total_stake = store.get_value("sxt_staking_era_total_stake", 0)
    era_reward = store.get_value("sxt_staking_last_era_reward", 0)

    if price > 0:
        store.set("sxt_staking_era_total_stake_usd", era_total_stake * price,
                  "Total era stake in USD")
        store.set("sxt_staking_last_era_reward_usd", era_reward * price,
                  "Last completed era reward in USD")

        # Per-validator USD metrics
        validators = store.get_labeled_entries("sxt_validator_total_stake")
        for labels, value in validators:
            addr = labels.get("address", "")
            store.set_labeled("sxt_validator_total_stake_usd",
                              {"address": addr}, value * price,
                              "Validator total stake in USD", "gauge")

        # Estimated APR per validator
        era_total_points = store.get_value("sxt_staking_era_total_reward_points", 0)
        points_entries = store.get_labeled_entries("sxt_validator_era_points")
        for labels, pts in points_entries:
            addr = labels.get("address", "")
            validator_stake = _find_labeled_value(store, "sxt_validator_total_stake", addr)
            if validator_stake > 0 and era_total_points > 0 and pts > 0:
                validator_era_reward = era_reward * (pts / era_total_points)
                apr = (validator_era_reward / validator_stake) * 365 * 100
                store.set_labeled("sxt_validator_estimated_apr",
                                  {"address": addr}, round(apr, 2),
                                  "Estimated annual return (%)", "gauge")
                store.set_labeled("sxt_validator_estimated_era_reward",
                                  {"address": addr}, round(validator_era_reward, 4),
                                  "Estimated reward for last era (SXT)", "gauge")

    # --- Pending rewards for own validator ---
    collect_pending_rewards(store)

    # --- Commission & yield breakdown ---
    collect_earnings(store)

    # --- ClickHouse: only write once per era change ---
    if not CLICKHOUSE_ENABLED or era == _ch_last_era_written:
        return
    _ch_last_era_written = era

    sub = _get_substrate()
    now = _get_era_timestamp(sub, era) if sub else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    era_total_points = int(store.get_value("sxt_staking_era_total_reward_points", 0))

    # era_snapshots
    active_vals = int(store.get_value("sxt_network_active_validators", 0))
    total_noms = int(store.get_value("sxt_staking_total_nominators", 0))
    snap_row = (
        f"{era}\t{era_total_stake}\t{active_vals}\t{total_noms}\t"
        f"{era_reward}\t{price}\t{now}"
    )
    _ch_query("INSERT INTO era_snapshots FORMAT TabSeparated", snap_row + "\n")

    # era_rewards + delegation_snapshots (per validator)
    validators = store.get_labeled_entries("sxt_validator_total_stake")
    era_rows = []
    delegation_rows = []

    for labels, total_stake in validators:
        addr_label = labels.get("address", "")
        safe_addr = addr_label.replace("\t", " ")

        own_stake = _find_labeled_value(store, "sxt_validator_own_stake", addr_label)
        nom_count = int(_find_labeled_value(store, "sxt_validator_nominator_count", addr_label))
        commission = _find_labeled_value(store, "sxt_validator_commission", addr_label)
        v_points = int(_find_labeled_value(store, "sxt_validator_era_points", addr_label))
        is_active = int(_find_labeled_value(store, "sxt_validator_active", addr_label))

        if era_total_points > 0 and v_points > 0:
            v_reward = era_reward * (v_points / era_total_points)
        else:
            v_reward = 0.0

        era_rows.append(
            f"{era}\t{safe_addr}\t{safe_addr}\t{total_stake}\t{own_stake}\t"
            f"{nom_count}\t{commission}\t{v_points}\t{era_total_points}\t"
            f"{era_reward}\t{v_reward}\t{is_active}\t{now}"
        )

        # Delegation change tracking
        delegated = total_stake - own_stake
        prev = _prev_stakes.get(addr_label, total_stake)
        change = total_stake - prev
        _prev_stakes[addr_label] = total_stake

        delegation_rows.append(
            f"{now}\t{era}\t{safe_addr}\t{safe_addr}\t{total_stake}\t"
            f"{own_stake}\t{delegated}\t{nom_count}\t{change}"
        )

    if era_rows:
        _ch_query("INSERT INTO era_rewards FORMAT TabSeparated",
                  "\n".join(era_rows) + "\n")
        log.info("Wrote %d era_rewards rows for era %d to ClickHouse",
                 len(era_rows), era)

    if delegation_rows:
        _ch_query("INSERT INTO delegation_snapshots FORMAT TabSeparated",
                  "\n".join(delegation_rows) + "\n")


def _find_labeled_value(store, metric_name: str, address: str) -> float:
    entries = store.get_labeled_entries(metric_name)
    for labels, value in entries:
        if labels.get("address") == address:
            return value
    return 0.0
