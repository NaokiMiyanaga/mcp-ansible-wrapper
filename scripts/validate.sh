#!/usr/bin/env bash
set -euo pipefail

# Minimal validation runner for MCP + lab
# Usage: bash scripts/validate.sh [--skip-bridge]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="${ROOT}/compose.yaml"

SKIP_BRIDGE=0
if [[ "${1:-}" == "--skip-bridge" ]]; then
  SKIP_BRIDGE=1
fi

tmpdir="$(mktemp -d)"
cleanup() { rm -rf "$tmpdir" || true; }
trap cleanup EXIT

run() {
  local name="$1"; shift
  echo "==> ${name}"
  set +e
  "$@" | tee "${tmpdir}/${name}.log"
  local rc=${PIPESTATUS[0]}
  set -e
  if [[ $rc -ne 0 ]]; then
    echo "[FAIL] ${name} (rc=${rc})" >&2
    exit $rc
  fi
}

assert_idempotent() {
  local name="$1"; local log_file="${tmpdir}/${name}.log"
  # If any host shows changed>=1, fail idempotency
  if grep -E "changed=[1-9]" "$log_file" >/dev/null 2>&1; then
    echo "[FAIL] ${name}: not idempotent (found changes)" >&2
    exit 1
  fi
  echo "[OK] ${name}: idempotent"
}

mcp() {
  docker compose -f "$COMPOSE" run --rm ansible python scripts/mcp.py "$@"
}

echo "Using compose file: $COMPOSE"

# 0) Sanity
run "ansible-version" docker compose -f "$COMPOSE" run --rm ansible ansible --version

# 1) Ping
run "ping" mcp ping

# 2) FRR check
run "frr.check" mcp frr.check

# 3) FRR apply (once)
run "apply.frr.1" mcp apply --component frr

# 4) FRR apply (idempotency)
run "apply.frr.2" mcp apply --component frr
assert_idempotent "apply.frr.2"

if [[ "$SKIP_BRIDGE" -eq 0 ]]; then
  # 5) Bridge apply (once)
  run "apply.bridge.1" mcp apply --component bridge

  # 6) Bridge apply (idempotency)
  run "apply.bridge.2" mcp apply --component bridge
  assert_idempotent "apply.bridge.2"

  # 7) Bridge check
  run "bridge.check" mcp bridge.check
else
  echo "[SKIP] bridge apply/check (requested)"
fi

# 8) Export ops
run "export-ops" mcp export-ops

# 9) Basic output check
if [[ -f "$ROOT/out/ops.json" ]]; then
  echo "[OK] out/ops.json generated"
else
  echo "[WARN] out/ops.json not found" >&2
fi

echo "All validations completed."

