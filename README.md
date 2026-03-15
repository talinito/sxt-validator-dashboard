# SXT Validator Dashboard

Full monitoring and economics stack for **Space and Time (SXT Chain)** validator nodes. Built with Prometheus, ClickHouse, Grafana, and a custom Python exporter that queries the Substrate RPC for deep validator metrics not available from the native Prometheus endpoint.

Designed for validator operators who want full visibility into their node, the network, staking economics, and operator earnings — all from a single dashboard.

![SXT](https://img.shields.io/badge/SXT_Chain-Substrate-5000BF) ![License](https://img.shields.io/badge/license-MIT-6F4D80)

---

## What it monitors

### Chain status
Block height (best and finalized), finality lag, sync state, era and epoch progress bars, GRANDPA round, runtime version, pending extrinsics.

### Network economics
Token price (USD), 24h change, market cap, 24h trading volume, total network stake in USD, network era reward, price history chart, estimated APR per validator, stake distribution (donut chart), stake per validator over time, era rewards history, delegation inflows/outflows per era.

### Operator earnings
Select any validator from the dropdown to view: estimated APR, total reward generated, commission earned (84-day and monthly), own-stake yield, per-era earnings breakdown (SXT + USD), monthly earnings aggregation, total stake over time.

### Network staking
Active and waiting validators with names, total stake, era rewards, nominators count, commission, era points. Includes bar charts for stake distribution and a full sortable table of all validators.

### Node performance
Peer count and role breakdown, BABE block production vs expected average, GRANDPA finality rate, block proposal and import times, network bandwidth, gossip message rates.

### Host resources
CPU, memory, and disk usage gauges with configurable mountpoint, disk I/O, network I/O, DB and state cache sizes, system uptime, load average.

---

## Architecture
```
┌─────────────────────────┐
│     SXT Validator        │
│   :9615 (prometheus)     │───────┐
│   :9944 (rpc)            │───┐   │
│   :30333 (p2p)           │   │   │
└─────────────────────────┘   │   │
                               │   │
┌─────────────────────────┐   │   │   ┌──────────────┐   ┌─────────────┐
│   sxt_exporter (Python)  │◄──┘   ├──►│  Prometheus   │──►│   Grafana    │
│   :9101                  │───────┘   │  :9090        │   │   :3000     │
│   + economics module     │──────┐    └──────────────┘   │  + ClickHouse│
└─────────────────────────┘      │                        │    plugin    │
                               ┌──┘──────────┐            └─────────────┘
┌─────────────────────────┐   │              │                   │
│   node_exporter          │───┘  ┌──────────┴──┐               │
│   :9100                  │      │  ClickHouse  │◄──────────────┘
└─────────────────────────┘      │  :8123/:9000 │
    ┌─────────────────┐          └──────────────┘
    │  CoinGecko API   │               │
    │  (token price)   │───────────────┘
    └─────────────────┘
```

### Data sources

**Prometheus** stores real-time metrics: node health, sync state, peer data, staking snapshots, token price, estimated APR.

**ClickHouse** stores historical data: price history, per-era per-validator rewards, delegation changes, operator earnings breakdowns. Data is retained for 2 years and survives Prometheus retention limits (default 30 days).

**CoinGecko** provides token price, market cap, and 24h volume via the free public API (no API key required).

### What the exporter queries

- `BabeApi_current_epoch` — epoch progress, authority set, slot production
- `GrandpaApi_grandpa_authorities` — finality authority count
- `Staking.*` — full validator set, stake, commission, nominators, era points, ledger
- `Session.Validators` — active validator list
- `system_peers` — peer roles and block heights
- Validator names from the [SXT Staking Dashboard API](https://staking.spaceandtime.io/api/validator)
- Per-era commission and own-stake yield calculation for all validators

---

## Prerequisites

- Docker and Docker Compose
- SXT validator node running with:
  - `--prometheus-external --prometheus-port 9615`
  - `--rpc-port 9944`
  - `--validator`
- `node_exporter` on the host for hardware metrics (`apt install prometheus-node-exporter`)

---

## Quick start
```bash
git clone https://github.com/talinito/sxt-validator-dashboard.git
cd sxt-validator-dashboard

# 1. Configure
cp .env.example .env
nano .env   # set your password and data mountpoint

# 2. Launch
chmod +x start.sh
./start.sh up

# 3. Open Grafana
# http://localhost:3000
```

Default login: `admin` / (password you set in `.env`)

First data appears within ~2 minutes. Economic data (earnings, delegation history) populates on the first staking deep scrape. ClickHouse historical data accumulates over time — the longer the stack runs, the richer the charts.

---

## Configuration

All settings are in `.env` — nothing is hardcoded.

| Variable | Default | Description |
|---|---|---|
| `SXT_RPC_URL` | `http://172.17.0.1:9944` | RPC endpoint of your SXT node |
| `SXT_PROMETHEUS_TARGET` | `172.17.0.1:9615` | Native Substrate metrics endpoint |
| `NODE_EXPORTER_TARGET` | `172.17.0.1:9100` | Host hardware metrics |
| `SXT_EXPORTER_POLL_INTERVAL` | `12` | Fast metrics poll interval (seconds) |
| `SXT_STAKING_POLL_INTERVAL` | `120` | Deep staking data poll interval (seconds) |
| `SXT_PRICE_POLL_INTERVAL` | `300` | Token price poll interval (seconds) |
| `SXT_DATA_MOUNTPOINT` | — | Mountpoint for disk usage gauge (find with `df -h`) |
| `GRAFANA_PORT` | `3000` | Grafana web UI port |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana admin username |
| `GRAFANA_ADMIN_PASSWORD` | — | Grafana admin password |
| `PROMETHEUS_RETENTION` | `30d` | How long Prometheus keeps real-time data |

### Running on the validator machine (recommended)

Default `.env` values work out of the box — the exporter reaches the RPC via Docker bridge (`172.17.0.1`).

### Running on a remote machine
```bash
SXT_RPC_URL=http://YOUR_VALIDATOR_IP:9944
SXT_PROMETHEUS_TARGET=YOUR_VALIDATOR_IP:9615
NODE_EXPORTER_TARGET=YOUR_VALIDATOR_IP:9100
```

Ensure firewall allows access from the monitoring machine to those ports.

---

## Dashboard structure

The dashboard has 6 collapsible rows with a validator selector dropdown at the top. All panels use SXT brand colors.

| Row | Panels | Key data |
|---|---|---|
| **⬡ Chain status** | 12 | Blocks, finality lag, sync, era/epoch progress, runtime |
| **⬡ Network economics** | 12 | Token price, market cap, volume, APR, stake distribution, era rewards, delegation flows |
| **⬡ Operator earnings** | 11 | Commission, own yield, monthly/84-day totals, per-era and monthly barcharts, stake history |
| **⬡ Network staking** | 10 | Validator table with names, stake bars, era points, nominators |
| **⬡ Node performance** | 12 | BABE production, peers, GRANDPA, bandwidth, proposal time |
| **⬡ Host resources** | 16 | CPU/RAM/disk gauges, I/O, cache, uptime |

### Validator selector

The dropdown at the top of the dashboard lists all validators in the active set. Selecting a validator updates all Operator earnings panels to show that validator's commission, yield, and stake history. Network-level panels are not affected.

### How validator names work

The exporter fetches names from the [SXT Staking Dashboard API](https://staking.spaceandtime.io/api/validator) once per hour. Names are matched by on-chain address and used as labels in all metrics. No hardcoded mapping needed — names update automatically as validators register.

The validator dropdown in the dashboard lists all validators automatically — no configuration needed.

---

## Stack components

| Service | Image | Purpose | Ports |
|---|---|---|---|
| `sxt-exporter` | Built from `exporter/` | Custom metrics + economics module | `127.0.0.1:9101` |
| `sxt-prometheus` | `prom/prometheus:v2.53.0` | Time-series storage (real-time) | `127.0.0.1:9090` |
| `sxt-clickhouse` | `clickhouse/clickhouse-server:24.8-alpine` | Historical storage (2 year retention) | `127.0.0.1:8123`, `127.0.0.1:9000` |
| `sxt-grafana` | `grafana/grafana:12.3.2` | Visualization | `:3000` |

ClickHouse tables: `price_history`, `era_rewards`, `era_snapshots`, `delegation_snapshots`. Views: `v_validator_earnings`, `v_validator_monthly`, `v_era_rewards`, `v_delegation_changes`.

---

## Management commands
```bash
./start.sh up        # Start the stack
./start.sh down      # Stop the stack
./start.sh restart   # Rebuild and restart
./start.sh status    # Check container health and Prometheus targets
./start.sh logs      # Follow all logs (or: ./start.sh logs sxt-exporter)
```

---

## Updating
```bash
cd sxt-validator-dashboard
git pull
./start.sh restart
```

---

## Troubleshooting

**No data in staking panels**: Wait up to 120 seconds for the first deep staking scrape.
```bash
curl -s http://localhost:9101/metrics | grep sxt_validator_total_stake | head -3
```

**No economic data**: The earnings calculation runs once per era change. Check:
```bash
docker logs sxt-exporter 2>&1 | grep -E "Earnings calc|Token price"
```

**ClickHouse tables empty**: Verify ClickHouse is healthy:
```bash
docker exec sxt-clickhouse clickhouse-client --database sxt --query "SHOW TABLES"
docker exec sxt-clickhouse clickhouse-client --database sxt --query "SELECT count() FROM era_rewards"
```

**Exporter errors**:
```bash
docker compose logs sxt-exporter --tail 30
```

**Prometheus targets down**:
```bash
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool
```

**Disk gauge shows wrong disk**: Set `SXT_DATA_MOUNTPOINT` in `.env` and restart. Find your mountpoint with `df -h`.

**Grafana not loading dashboard**: Delete the Grafana volume and restart:
```bash
docker compose down
docker volume rm sxt-validator-dashboard_grafana-data
./start.sh up
```

**Validator not in dropdown**: The dropdown populates from on-chain data. If a validator is missing, wait for the next staking deep scrape (~120s) or check the [SXT Staking Dashboard](https://staking.spaceandtime.io/).

---

## License

MIT

---

Built by [Ethernodes](https://ethernodes.io) for the [Space and Time](https://www.spaceandtime.io/) validator community.
