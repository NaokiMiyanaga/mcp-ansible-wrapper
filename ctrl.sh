#!/usr/bin/env bash
set -euo pipefail

# mcp-ansible-wrapper controller (single entrypoint) v2
# Usage: ./mcpctl.sh [start|stop|restart|status|logs [svc]|health|run [json]|rebuild]

COMPOSE_FILE="docker-compose.yml"
SERVICE="ansible-mcp"
PORT="9000"

# auto-load .env if present
if [[ -f .env ]]; then
  set -a
  source ./.env
  set +a
fi

export MCP_TOKEN="${MCP_TOKEN:-secret123}"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

ensure_service() {
  if ! compose config --services | grep -qx "$SERVICE"; then
    echo "[ERR] service '$SERVICE' not found in $COMPOSE_FILE"
    echo "      services: $(compose config --services | tr '\n' ' ')"
    exit 1
  fi
}

start() {
  ensure_service
  # ensure external network for cross-repo connectivity
  docker network inspect mgmtnet >/dev/null 2>&1 || docker network create mgmtnet >/dev/null 2>&1 || true
  echo "[+] Build & start: $SERVICE"
  compose up -d --build "$SERVICE"
  status
  wait_http
  echo "Base URL : http://localhost:${PORT}"
  echo "Auth     : Bearer \$MCP_TOKEN"
}

stop() {
  ensure_service
  echo "[+] Stop & remove: $SERVICE"
  compose stop "$SERVICE" || true
  compose rm -f "$SERVICE" || true
  status || true
}

restart() { stop; start; }

status() {
  echo "[ps]"; compose ps || true
  echo; echo "[ports]"; compose ps --format 'table {{.Name}}\t{{.Publishers}}' || true
}

logs() {
  ensure_service
  local svc="${1:-$SERVICE}"
  compose logs -f --tail=200 "$svc"
}

wait_http() {
  echo "[*] waiting for http://localhost:${PORT} ..."
  for i in {1..60}; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/health" || true)
    [[ "$code" =~ ^(200|404)$ ]] && echo "[ok] HTTP up (code $code)" && return 0
    code2=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/" || true)
    [[ "$code2" =~ ^(200|404)$ ]] && echo "[ok] HTTP up (code $code2)" && return 0
    sleep 1
  done
  echo "[WARN] no HTTP response yet"
  return 1
}

health() {
  echo "[health] GET /health"
  curl -fsS "http://localhost:${PORT}/health" || { echo "(fallback GET /)"; curl -fsS "http://localhost:${PORT}/" || true; }
  echo
}

run() {
  # default payload matches Option 2A (bind-mount): playbooks/site.yml
  local payload="${1:-}"
  if [[ -z "$payload" ]]; then
    payload='{"playbook":"playbooks/site.yml","limit":"r1","extra_vars":{"bridge_name":"br0"}}'
  fi
  echo "[*] POST /mcp/run  (token: ${MCP_TOKEN})"
  curl -sS -H "Authorization: Bearer ${MCP_TOKEN}" \
       -H "Content-Type: application/json" \
       -d "$payload" \
       "http://localhost:${PORT}/mcp/run" | jq .
}

rebuild() {
  read -r -p "This will DESTROY and rebuild the MCP service. Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
  compose down -v --remove-orphans || true
  compose rm -f -v || true
  echo "[+] Rebuild --no-cache"
  compose build --no-cache "$SERVICE"
  start
}

usage() {
cat <<EOF
mcp-ansible-wrapper controller (v2)

Usage:
  $0 start            Build & start service ($SERVICE)
  $0 stop             Stop & remove service
  $0 restart          Stop then start
  $0 status           Show compose ps and port mappings
  $0 logs [service]   Tail logs (default: $SERVICE)
  $0 health           GET /health (fallback GET /)
  $0 run [json]       POST /mcp/run (default: playbooks/site.yml)
  $0 rebuild          Destroy, build --no-cache, and start
EOF
}

cmd="${1:-}"; shift || true
case "$cmd" in
  start)   start "$@";;
  stop)    stop "$@";;
  restart) restart "$@";;
  status)  status "$@";;
  logs)    logs "${1:-}";;
  health)  health;;
  run)     run "${1:-}";;
  rebuild) rebuild;;
  ""|-h|--help) usage;;
  *) echo "[ERR] unknown command: $cmd"; usage; exit 1;;
esac
