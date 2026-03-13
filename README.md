# SXT Validator Dashboard

Monitoring stack for **Space and Time (SXT Chain)** validator nodes. Built with Prometheus, Grafana, and a custom Python exporter that queries the Substrate RPC for deep validator metrics not available from the native Prometheus endpoint.

Designed for validator operators who want full visibility into their node, the network, and the active validator set — all from a single dashboard.

![SXT](https://img.shields.io/badge/SXT_Chain-Substrate-5000BF) ![License](https://img.shields.io/badge/license-MIT-6F4D80)

---

## What it monitors

### Chain status
Block height (best and finalized), finality lag, sync state, era and epoch progress bars, GRANDPA round, runtime version, pending extrinsics.

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
┌─────────────────────────┐   │   │   ┌──────────────┐   ┌─────────┐
│   sxt_exporter (Python)  │◄──┘   ├──►│  Prometheus   │──►│ Grafana │
│   :9101                  │───────┘   │  :9090        │   │  :3000  │
└─────────────────────────┘           └──────────────┘   └─────────┘
                               ┌──────────┘
┌─────────────────────────┐   │
│   node_exporter          │───┘
│   :9100                  │
└─────────────────────────┘
```

The custom exporter connects to the Substrate RPC via `substrate-interface` and queries:
- `BabeApi_current_epoch` — epoch progress, authority set, slot production
- `GrandpaApi_grandpa_authorities` — finality authority count
- `Staking.*` — full validator set, stake, commission, nominators, era points
- `Session.Validators` — active validator list
- `system_peers` — peer roles and block heights
- Validator names from the [SXT Staking Dashboard API](https://staking.spaceandtime.io/api/validator)

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
nano .env   # set your validator name, password, and data mountpoint

# 2. Launch
chmod +x start.sh
./start.sh up

# 3. Open
# http://localhost:3000
```

Default login: `admin` / (password from `.env`)

---

## Configuration

All settings are in `.env` — nothing is hardcoded.

| Variable | Default | Description |
|---|---|---|
| `SXT_RPC_URL` | `http://172.17.0.1:9944` | RPC endpoint of your SXT node |
| `SXT_PROMETHEUS_TARGET` | `172.17.0.1:9615` | Native Substrate metrics endpoint |
| `NODE_EXPORTER_TARGET` | `172.17.0.1:9100` | Host hardware metrics |
| `SXT_VALIDATOR_NAME` | `YourValidator` | Your validator name (as in `--name` flag). Marked with ★ in all panels |
| `SXT_EXPORTER_POLL_INTERVAL` | `12` | Fast metrics poll interval (seconds) |
| `SXT_STAKING_POLL_INTERVAL` | `120` | Deep staking data poll interval (seconds) |
| `SXT_DATA_MOUNTPOINT` | `/your-data-mount` | Mountpoint for disk usage gauge |
| `GRAFANA_PORT` | `3000` | Grafana web UI port |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana admin username |
| `GRAFANA_ADMIN_PASSWORD` | `changeme` | Grafana admin password |
| `PROMETHEUS_RETENTION` | `30d` | How long Prometheus keeps data |

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

The dashboard has 4 collapsible rows, all using SXT brand colors (`#5000BF` Electric Purple, `#CC0AAC` Neon Magenta, `#6F4D80` Nebula Silver).

| Row | Panels | Key data |
|---|---|---|
| **⬡ Chain status** | 12 panels | Blocks, finality lag, sync, era/epoch progress bars, runtime |
| **⬡ Network staking** | 10 panels | Validator table with names, stake distribution, era points, nominators |
| **⬡ Node performance** | 12 panels | BABE production, peers, GRANDPA, bandwidth, proposal time |
| **⬡ Host resources** | 16 panels | CPU/RAM/disk gauges, I/O, cache, uptime |

Your validator is automatically marked with **★** across all panels and tables.

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

**No data in staking panels**: Wait up to 120 seconds for the first deep staking scrape. Check with:
```bash
curl -s http://localhost:9101/metrics | grep sxt_validator_total_stake | head -3
```

**Exporter errors**: Check logs:
```bash
docker compose logs sxt-exporter --tail 30
```

**Prometheus targets down**:
```bash
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool
```

**Disk gauge shows wrong disk**: Edit `SXT_DATA_MOUNTPOINT` in `.env` and run `./start.sh restart`. Find your mountpoint with `df -h`.

**Grafana not loading dashboard**: Delete the Grafana volume and restart:
```bash
docker compose down
docker volume rm sxt-validator-dashboard_grafana-data
./start.sh up
```

---

## How validator names work

The exporter fetches names from the [SXT Staking Dashboard API](https://staking.spaceandtime.io/api/validator) once per hour. Names are matched by on-chain address and used as labels in all Prometheus metrics. No hardcoded mapping needed — names update automatically as validators register.

---

## License

MIT

---

Built by [Ethernodes](https://ethernodes.io) for the SXT validator community.
