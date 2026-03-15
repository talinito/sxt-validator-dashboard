-- ============================================================
-- SXT Validator Dashboard — ClickHouse Schema
-- Economic & historical data tables
-- ============================================================

CREATE DATABASE IF NOT EXISTS sxt;

-- 1. Token price snapshots (from CoinGecko)
CREATE TABLE IF NOT EXISTS sxt.price_history (
    timestamp    DateTime64(3, 'UTC'),
    price_usd    Float64,
    price_eur    Float64,
    market_cap_usd Float64,
    volume_24h_usd Float64,
    change_24h_pct Float64
) ENGINE = MergeTree()
ORDER BY timestamp
TTL toDateTime(timestamp) + INTERVAL 2 YEAR;

-- 2. Per-era, per-validator reward & stake data
CREATE TABLE IF NOT EXISTS sxt.era_rewards (
    era              UInt32,
    validator_address String,
    validator_name    String,
    total_stake       Float64,
    own_stake         Float64,
    nominator_count   UInt32,
    commission_pct    Float64,
    era_points        UInt32,
    era_total_points  UInt32,
    era_total_reward  Float64,
    validator_reward  Float64,
    is_active         UInt8,
    timestamp         DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(timestamp)
ORDER BY (era, validator_address)
TTL toDateTime(timestamp) + INTERVAL 2 YEAR;

-- 3. Network-level era snapshots
CREATE TABLE IF NOT EXISTS sxt.era_snapshots (
    era               UInt32,
    total_stake       Float64,
    active_validators UInt32,
    total_nominators  UInt32,
    era_reward        Float64,
    price_usd_at_era  Float64,
    timestamp         DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(timestamp)
ORDER BY era
TTL toDateTime(timestamp) + INTERVAL 2 YEAR;

-- 4. Delegation change events (snapshots diffed by exporter)
CREATE TABLE IF NOT EXISTS sxt.delegation_snapshots (
    timestamp          DateTime64(3, 'UTC'),
    era                UInt32,
    validator_address  String,
    validator_name     String,
    total_stake        Float64,
    own_stake          Float64,
    delegated_stake    Float64,
    nominator_count    UInt32,
    stake_change       Float64
) ENGINE = MergeTree()
ORDER BY (timestamp, validator_address)
TTL toDateTime(timestamp) + INTERVAL 2 YEAR;

-- 5. Per-era operator earnings breakdown
CREATE TABLE IF NOT EXISTS sxt.validator_earnings (
    era              UInt32,
    commission_sxt   Float64,
    own_yield_sxt    Float64,
    total_earned_sxt Float64,
    validator_reward  Float64,
    commission_rate   Float64,
    own_stake         Float64,
    delegated_stake   Float64,
    total_stake       Float64,
    price_usd         Float64,
    timestamp         DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(timestamp)
ORDER BY era
TTL toDateTime(timestamp) + INTERVAL 2 YEAR;

-- Views for Grafana
CREATE OR REPLACE VIEW sxt.v_monthly_earnings AS
SELECT
    formatDateTime(toStartOfMonth(toDateTime(timestamp)), '%Y-%m') as month,
    sum(commission_sxt) as comm_sxt,
    sum(own_yield_sxt) as yield_sxt,
    sum(commission_sxt * price_usd) as comm_usd,
    sum(own_yield_sxt * price_usd) as yield_usd
FROM sxt.validator_earnings FINAL
WHERE price_usd > 0
GROUP BY month;

CREATE OR REPLACE VIEW sxt.v_era_rewards AS
SELECT era, total_stake as network_stake, era_reward as network_reward,
       active_validators, total_nominators, price_usd_at_era as price_usd
FROM sxt.era_snapshots FINAL ORDER BY era;

CREATE OR REPLACE VIEW sxt.v_delegation_changes AS
SELECT era, sum(stake_change) as net_change,
       sum(if(stake_change > 0, stake_change, 0)) as inflows,
       sum(if(stake_change < 0, stake_change, 0)) as outflows
FROM sxt.delegation_snapshots GROUP BY era ORDER BY era;
