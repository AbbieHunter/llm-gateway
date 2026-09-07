#!/usr/bin/env bash
# Rolling recreate of gateway-a then gateway-b (HA compose).
# Always keep at least one instance healthy while the other is replaced.
#
# Usage (from repo root on the server):
#   ./scripts/rollout_ha.sh
#   COMPOSE_FILE=docker-compose.prod.ha.yml HEALTH_TIMEOUT=180 ./scripts/rollout_ha.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.ha.yml}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: missing $COMPOSE_FILE" >&2
  exit 1
fi

wait_healthy() {
  local service="$1"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  echo "→ waiting for $service healthy (timeout ${HEALTH_TIMEOUT}s)..."
  while (( SECONDS < deadline )); do
    # Prefer compose health status when available
    local status
    status="$("${COMPOSE[@]}" ps --format json "$service" 2>/dev/null | head -1 || true)"
    if [[ -n "$status" ]] && echo "$status" | grep -q '"Health":"healthy"'; then
      echo "✓ $service is healthy"
      return 0
    fi
    # Fallback: docker inspect by compose container name pattern
    local cid
    cid="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$cid" ]]; then
      local h
      h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
      if [[ "$h" == "healthy" ]]; then
        echo "✓ $service is healthy"
        return 0
      fi
      echo "  ... $service status=$h"
    else
      echo "  ... $service container not found yet"
    fi
    sleep 3
  done
  echo "ERROR: $service did not become healthy within ${HEALTH_TIMEOUT}s" >&2
  "${COMPOSE[@]}" ps
  "${COMPOSE[@]}" logs --tail 80 "$service" || true
  exit 1
}

echo "== build gateway-a gateway-b =="
"${COMPOSE[@]}" build gateway-a gateway-b

for svc in gateway-a gateway-b; do
  echo "== recreate $svc =="
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$svc"
  wait_healthy "$svc"
done

echo "== done =="
"${COMPOSE[@]}" ps
echo
echo "Tip: do NOT use 'up -d --build' against both gateways at once; use this script."
