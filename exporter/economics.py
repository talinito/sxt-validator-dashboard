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
_era_start_cache: dict[str, int] = {}

_current_price = {
    "usd": 0.0,
    "eur": 0.0,
    "market_cap_usd": 0.0,
    "volume_24h_usd": 0.0,
    "change_24h_pct": 0.0,
}


# ---------------------------------------------------------------------------
# Validator address resolution
# ---------------------------------------------------------------------------


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
