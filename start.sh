#!/usr/bin/env bash
#===============================================================================
# SXT Validator Dashboard - Start script
# Renders Prometheus config from .env values, then launches Docker Compose
#
# Usage: ./start.sh [up|down|restart|logs|status]
#===============================================================================

set -euo pipefail
cd "$(dirname "$0")"

# -----------------------------------------------
# Load .env
# -----------------------------------------------
if [ ! -f .env ]; then
    echo "No .env found. Creating from .env.example..."
    cp .env.example .env
    echo "Please edit .env with your settings, then re-run this script."
    exit 1
fi

set -a
source .env
set +a

# Defaults
SXT_RPC_URL="${SXT_RPC_URL:-http://172.17.0.1:9944}"
SXT_PROMETHEUS_TARGET="${SXT_PROMETHEUS_TARGET:-172.17.0.1:9615}"
NODE_EXPORTER_TARGET="${NODE_EXPORTER_TARGET:-172.17.0.1:9100}"
SXT_VALIDATOR_NAME="${SXT_VALIDATOR_NAME:-unknown}"
SXT_EXPORTER_PORT="${SXT_EXPORTER_PORT:-9101}"
PROMETHEUS_SCRAPE_INTERVAL="${PROMETHEUS_SCRAPE_INTERVAL:-15s}"

# -----------------------------------------------
# Render Prometheus config
# -----------------------------------------------
echo "Rendering prometheus/prometheus.yml from .env..."
sed \
    -e "s|__SXT_PROMETHEUS_TARGET__|${SXT_PROMETHEUS_TARGET}|g" \
    -e "s|__SXT_EXPORTER_PORT__|${SXT_EXPORTER_PORT}|g" \
    -e "s|__SXT_VALIDATOR_NAME__|${SXT_VALIDATOR_NAME}|g" \
    -e "s|__NODE_EXPORTER_TARGET__|${NODE_EXPORTER_TARGET}|g" \
    -e "s|__PROMETHEUS_SCRAPE_INTERVAL__|${PROMETHEUS_SCRAPE_INTERVAL}|g" \
    prometheus/prometheus.yml > prometheus/prometheus.rendered.yml

echo "  SXT node metrics:  ${SXT_PROMETHEUS_TARGET}"
echo "  SXT exporter:      sxt-exporter:${SXT_EXPORTER_PORT}"
echo "  Node exporter:     ${NODE_EXPORTER_TARGET}"
echo "  Validator name:    ${SXT_VALIDATOR_NAME}"

# -----------------------------------------------
# Action
# -----------------------------------------------
ACTION="${1:-up}"

case "$ACTION" in
    up)
        echo ""
        echo "Starting SXT Validator Dashboard..."
        docker compose up -d --build
        echo ""
        echo "Dashboard ready at http://localhost:${GRAFANA_PORT:-3000}"
        echo "  User: ${GRAFANA_ADMIN_USER:-admin}"
        echo "  Pass: (set in .env)"
        ;;
    down)
        echo "Stopping SXT Validator Dashboard..."
        docker compose down
        ;;
    restart)
        echo "Restarting SXT Validator Dashboard..."
        docker compose down
        docker compose up -d --build
        ;;
    logs)
        docker compose logs -f "${2:-}"
        ;;
    status)
        echo "=== Container status ==="
        docker compose ps
        echo ""
        echo "=== Prometheus targets ==="
        curl -sf http://localhost:${PROMETHEUS_PORT:-9090}/api/v1/targets 2>/dev/null | \
            python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data.get('data',{}).get('activeTargets',[]):
    print(f\"  {t['labels'].get('job','?'):20s} {t['health']:6s} {t['scrapeUrl']}\")
" 2>/dev/null || echo "  (Prometheus not reachable)"
        echo ""
        echo "=== Exporter health ==="
        curl -sf http://localhost:${SXT_EXPORTER_PORT:-9101}/health 2>/dev/null && echo "  Exporter OK" || echo "  Exporter not reachable"
        ;;
    *)
        echo "Usage: $0 [up|down|restart|logs|status]"
        exit 1
        ;;
esac
