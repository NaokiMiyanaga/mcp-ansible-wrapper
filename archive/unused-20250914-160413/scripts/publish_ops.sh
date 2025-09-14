#!/usr/bin/env bash
set -euo pipefail

# Publish IETF ops: generate latest + snapshot, convert to JSONL, and call external ETL
# Usage: bash scripts/publish_ops.sh [--schema-dir /path/to/ietf-network-schema] [--db rag.db] [--reset]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT/compose.yaml"

SCHEMA_DIR="${IETF_SCHEMA_DIR:-}"
DB_NAME="rag.db"
RESET_FLAG="--reset"

DEBUG=0
DEBUG_LIMIT=10
VALIDATE=0
SYNTH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --schema-dir)
      SCHEMA_DIR="$2"; shift 2 ;;
    --db)
      DB_NAME="$2"; shift 2 ;;
    --no-reset)
      RESET_FLAG=""; shift ;;
    --debug)
      DEBUG=1; shift ;;
    --debug-limit)
      DEBUG_LIMIT="$2"; shift 2 ;;
    --synthesize)
      SYNTH=1; shift ;;
    --validate)
      VALIDATE=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$SCHEMA_DIR" ]]; then
  read -r -p "Path to ietf-network-schema repo (SCHEMA_DIR): " SCHEMA_DIR
fi

if [[ ! -d "$SCHEMA_DIR" ]]; then
  echo "Schema dir not found: $SCHEMA_DIR" >&2
  exit 1
fi

echo "==> Export IETF latest"
docker compose -f "$COMPOSE_FILE" run --rm ansible \
  python scripts/mcp.py export-ops --format ietf --output /work/output/ops_ietf.json

echo "==> Export IETF snapshot"
docker compose -f "$COMPOSE_FILE" run --rm ansible \
  python scripts/mcp.py export-ops --format ietf --snapshot --output /work/output/

echo "==> Export JSONL directly (objects.jsonl)"
docker compose -f "$COMPOSE_FILE" run --rm ansible \
  python scripts/mcp.py export-ops --format jsonl --output /work/output/objects.jsonl

echo "==> Copy to external repo"
cp "$ROOT/output/objects.jsonl" "$SCHEMA_DIR/outputs/objects.jsonl"

if [[ "$SYNTH" -eq 1 ]]; then
  echo "==> Synthesize defaults (placeholders) into objects.jsonl"
  python "$ROOT/scripts/synthesize_jsonl.py" \
    --inventory "$ROOT/inventory/hosts.ini" \
    --append "$SCHEMA_DIR/outputs/objects.jsonl"
fi

echo "==> Run external ETL"
(
  cd "$SCHEMA_DIR"
  python scripts/loadJSONL.py --db "$DB_NAME" --jsonl outputs/objects.jsonl ${RESET_FLAG}
)

echo "Publish completed. DB: $SCHEMA_DIR/$DB_NAME"

if [[ "$DEBUG" -eq 1 ]]; then
  echo "==> Inspect DB summary"
  python "$ROOT/scripts/inspect_db.py" --db "$SCHEMA_DIR/$DB_NAME" --limit "$DEBUG_LIMIT"
  echo "==> Report (BGP and IF mismatches)"
  python "$ROOT/scripts/inspect_db.py" --db "$SCHEMA_DIR/$DB_NAME" --report
fi

if [[ "$VALIDATE" -eq 1 ]]; then
  echo "==> Validate schemas (IETF & JSONL)"
  python "$ROOT/scripts/validate_schema.py" --ietf "$ROOT/output/ops_ietf.json" --jsonl "$ROOT/output/objects.jsonl"
fi
