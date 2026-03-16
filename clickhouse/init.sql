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

-- Views for Grafana

CREATE OR REPLACE VIEW sxt.v_era_rewards AS
SELECT era, total_stake as network_stake, era_reward as network_reward,
       active_validators, total_nominators, price_usd_at_era as price_usd
FROM sxt.era_snapshots FINAL ORDER BY era;

CREATE OR REPLACE VIEW sxt.v_delegation_changes AS
SELECT era, sum(stake_change) as net_change,
       sum(if(stake_change > 0, stake_change, 0)) as inflows,
       sum(if(stake_change < 0, stake_change, 0)) as outflows
FROM sxt.delegation_snapshots GROUP BY era ORDER BY era;

-- 5. Per-era operator earnings (derived from era_rewards)
--    Substrate economics: commission taken first, remainder split by stake proportion
CREATE OR REPLACE VIEW sxt.v_validator_earnings AS
SELECT
    era,
    validator_name,
    validator_reward * (commission_pct / 100) as commission_sxt,
    if(total_stake > 0,
       (validator_reward - validator_reward * (commission_pct / 100)) * (own_stake / total_stake),
       0) as own_yield_sxt,
    validator_reward * (commission_pct / 100)
      + if(total_stake > 0,
           (validator_reward - validator_reward * (commission_pct / 100)) * (own_stake / total_stake),
           0) as total_earned_sxt,
    validator_reward,
    commission_pct as commission_rate,
    own_stake,
    total_stake - own_stake as delegated_stake,
    total_stake
FROM sxt.era_rewards FINAL
WHERE validator_reward > 0
ORDER BY era;

-- 6. Monthly aggregation of operator earnings
CREATE OR REPLACE VIEW sxt.v_validator_monthly AS
SELECT
    formatDateTime(toStartOfMonth(toDateTime(timestamp)), '%Y-%m') as month,
    validator_name,
    sum(validator_reward * (commission_pct / 100)) as comm_sxt,
    sum(if(total_stake > 0,
           (validator_reward - validator_reward * (commission_pct / 100)) * (own_stake / total_stake),
           0)) as yield_sxt,
    sum(validator_reward * (commission_pct / 100)
      + if(total_stake > 0,
           (validator_reward - validator_reward * (commission_pct / 100)) * (own_stake / total_stake),
           0)) as total_sxt
FROM sxt.era_rewards FINAL
WHERE validator_reward > 0
GROUP BY month, validator_name
ORDER BY month;
