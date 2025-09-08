# mcp-ansible-wrapper/start.sh
#!/usr/bin/env bash
set -euo pipefail

# --------------- settings ---------------
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"   # 明示してcompose.yamlを避ける
SERVICE="${SERVICE:-mcp}"                             # docker-compose.yml 側のサービス名
PORT="${PORT:-9000}"                                  # 公開ポート
# ----------------------------------------

# .env があれば読み込む（任意）
if [[ -f .env ]]; then set -a; source ./.env; set +a; fi

# デフォルト（上書き可）
export MCP_TOKEN="${MCP_TOKEN:-secret123}"
export MCP_WORKDIR="${MCP_WORKDIR:-/app}"

# サービス名存在チェック
if ! docker compose -f "$COMPOSE_FILE" config --services | grep -qx "$SERVICE"; then
  echo "[ERR] service '$SERVICE' が $COMPOSE_FILE に見つかりません。利用可能: "
  docker compose -f "$COMPOSE_FILE" config --services || true
  exit 1
fi

echo "[+] Building & starting $SERVICE ..."
docker compose -f "$COMPOSE_FILE" up -d --build "$SERVICE"
docker compose -f "$COMPOSE_FILE" ps

# ポート待ち（/health未実装でもOKな簡易チェック）
echo "[*] wait for :$PORT ..."
for i in {1..30}; do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1 || \
     curl -fsS "http://localhost:${PORT}/" >/dev/null 2>&1; then
    echo "[ok] HTTP endpoint is up"
    break
  fi
  sleep 1
  [[ $i -eq 30 ]] && { echo "[WARN] ヘルス応答なし（でも進みます）"; }
done

echo "===> MCP base: http://localhost:${PORT}"
echo "===> TOKEN    : MCP_TOKEN=${MCP_TOKEN}"
