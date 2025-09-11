#!/usr/bin/env bash
set -euo pipefail
BASE_URL="${1:-http://localhost:9000}"
TOKEN="${MCP_TOKEN:-}"
echo "[INFO] BASE_URL=$BASE_URL"
echo "[INFO] TOKEN_SET=$([ -n "$TOKEN" ] && echo yes || echo no)"

echo -e "\n== GET /health (no auth required) =="
curl -sS "$BASE_URL/health" | jq . || curl -sS "$BASE_URL/health"

echo -e "\n== POST /run without Authorization header =="
curl -sS -X POST "$BASE_URL/run" -H "Content-Type: application/json"   -d '{"text":"show bgp summary on r1","decision":"run","score":0.9}' | jq . || true

if [ -n "$TOKEN" ]; then
  echo -e "\n== POST /run with Authorization: Bearer ***** =="
  curl -sS -X POST "$BASE_URL/run"     -H "Content-Type: application/json"     -H "Authorization: Bearer $TOKEN"     -d '{"text":"show bgp summary on r1","decision":"run","score":0.9}' | jq . || true
else
  echo -e "\n(SKIP) No MCP_TOKEN set; export MCP_TOKEN=... and re-run to test authorized call."
fi
