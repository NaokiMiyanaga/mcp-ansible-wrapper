#!/usr/bin/env bash
set -euo pipefail

DB="${DB:-rag.db}"
MCP_BASE="${MCP_BASE:-http://127.0.0.1:9000}"
MCP_TOKEN="${MCP_TOKEN:-}"
PLAYBOOK_BGP="${PLAYBOOK_BGP:-show_bgp}"
PLAYBOOK_OSPF="${PLAYBOOK_OSPF:-show_ospf}"
VERBOSE="${VERBOSE:-1}"

# Basic banner so we always know which file ran
echo "[start] $(basename "$0") pid=$$ pwd=$(pwd)"
echo "[env] VERBOSE=${VERBOSE} DB=${DB} MCP_BASE=${MCP_BASE} PLAYBOOK_BGP=${PLAYBOOK_BGP} PLAYBOOK_OSPF=${PLAYBOOK_OSPF}"

if [[ "${VERBOSE}" =~ ^[1-9]$ ]]; then
  # VERBOSE>=1 shows lightweight traces for early debugging
  : # no-op placeholder
fi
if [[ "${VERBOSE}" =~ ^[2-9]$ ]]; then
  set -x
fi

if [[ -z "${MCP_TOKEN}" ]]; then
  echo "MCP_TOKEN is required" >&2
  exit 2
fi

# Optional: apply DB schema if provided (try sensible defaults if not set)
SCHEMA_SQL="${SCHEMA_SQL:-}"
if [[ -z "${SCHEMA_SQL}" ]]; then
  # try common locations relative to this repo and your dev tree
  for cand in \
    "./cmdb_schema.sql" \
    "../ietf-network-schema/cmdb_schema.sql" \
    "/Users/naoki/devNet/ietf-network-schema/cmdb_schema.sql"
  do
    if [[ -f "$cand" ]]; then
      SCHEMA_SQL="$cand"
      break
    fi
  done
fi

# Show how SCHEMA_SQL was resolved
if [[ -n "${SCHEMA_SQL}" ]]; then
  echo "[schema] selected: ${SCHEMA_SQL} -> ${DB}"
else
  echo "[schema] selected: <none> -> ${DB}"
fi

if [[ -n "${SCHEMA_SQL}" ]]; then
  if [[ -f "${SCHEMA_SQL}" ]]; then
    if [[ "${VERBOSE}" =~ ^[2-9]$ ]]; then
      ls -l "${SCHEMA_SQL}" || true
    fi
    echo "[schema] applying: ${SCHEMA_SQL} -> ${DB}"
    sqlite3 "${DB}" ".read ${SCHEMA_SQL}" || {
      echo "[schema] ERROR: failed to apply ${SCHEMA_SQL} to ${DB}" >&2
      exit 1
    }
  else
    echo "[schema] WARN: file not found: ${SCHEMA_SQL}" >&2
  fi
else
  echo "[schema] skipped (SCHEMA_SQL not set and no default found)"
fi

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON_BIN:-python3}"

"${PY}" "${THIS_DIR}/mcp_ingest_state.py" \
  --db "${DB}" \
  --mcp-base "${MCP_BASE}" \
  --token "${MCP_TOKEN}" \
  --playbook-bgp "${PLAYBOOK_BGP}" \
  --playbook-ospf "${PLAYBOOK_OSPF}" \
  $([[ "${VERBOSE}" == "1" ]] && echo --verbose)
