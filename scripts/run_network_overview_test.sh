#!/usr/bin/env bash
# Wrapper to run test_network_overview.py inside the ansible-mcp container.
# Usage (host):
#   ./scripts/run_network_overview_test.sh [--limit pattern] [--json]
# Requires: docker compose service name 'ansible-mcp' running or buildable.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
SERVICE=ansible-mcp
LIMIT=""
EXTRA=""
for a in "$@"; do
  case "$a" in
    --limit*) LIMIT="$a" ;;
    --json) EXTRA+=" --json" ;;
    *) EXTRA+=" $a" ;;
  esac
  shift || true
  # we accumulate; not shifting to keep simplicity for now
  # (args are small; ignoring shift side-effect)
  :
done

# Ensure container is up (non-fatal if already)
if ! docker ps --format '{{.Names}}' | grep -q "^${SERVICE}$"; then
  echo "[info] Starting docker compose service ${SERVICE}..." >&2
  docker compose up -d ${SERVICE}
  # small wait so ansible-playbook available
  sleep 2
fi

ENV_EXPORTS='export ANSIBLE_STDOUT_CALLBACK=json; export ANSIBLE_FORCE_COLOR=0; export ANSIBLE_DEPRECATION_WARNINGS=False; export ANSIBLE_DISPLAY_SKIPPED_HOSTS=False'
CMD="python scripts/test_network_overview.py ${LIMIT} ${EXTRA}";
echo "[info] Executing inside container (noise-suppressed): $CMD" >&2
docker compose exec -T ${SERVICE} bash -lc "$ENV_EXPORTS; $CMD" | jq .
