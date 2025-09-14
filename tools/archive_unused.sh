#!/usr/bin/env bash
set -euo pipefail

# Archive likely-unused directories to test if runtime breaks.
# Default targets: roles/ inventory/ docs/ scripts/
# Templates are kept (ops_export.yml depends on them).
#
# Usage:
#   ./tools/archive_unused.sh --dry-run       # show what would move + grep refs
#   ./tools/archive_unused.sh --apply         # actually move into archive/unused-TS/
#   ./tools/archive_unused.sh --restore       # move back from latest archive/unused-TS/
#   ./tools/archive_unused.sh --targets roles,inventory,docs,scripts  # custom

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
ARCHIVE_DIR="$ROOT_DIR/archive"
TS=$(date +%Y%m%d-%H%M%S)
DEST="$ARCHIVE_DIR/unused-$TS"

MODE="dry"
TARGETS="roles,inventory,docs,scripts"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE="dry"; shift ;;
    --apply)   MODE="apply"; shift ;;
    --restore) MODE="restore"; shift ;;
    --targets) TARGETS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--dry-run|--apply|--restore] [--targets roles,inventory,docs,scripts]"; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

IFS=',' read -r -a ARR <<< "$TARGETS"
declare -a PATHS=()
for t in "${ARR[@]}"; do
  case "$t" in
    roles|inventory|docs|scripts)
      PATHS+=("$ROOT_DIR/$t") ;;
    *) echo "[WARN] ignore unknown target: $t" ;;
  esac
done

if [[ "$MODE" == "restore" ]]; then
  last=$(ls -1dt "$ARCHIVE_DIR"/unused-* 2>/dev/null | head -n1 || true)
  if [[ -z "$last" ]]; then
    echo "[ERR] no archive found under $ARCHIVE_DIR" >&2; exit 1
  fi
  echo "[+] Restoring from: $last"
  shopt -s dotglob
  for p in "$last"/*; do
    base=$(basename "$p")
    dest="$ROOT_DIR/$base"
    echo "  - mv $p -> $dest"
    mv "$p" "$dest"
  done
  rmdir "$last" 2>/dev/null || true
  echo "[OK] restore done"
  exit 0
fi

echo "[plan] targets: ${PATHS[*]}"

# Show references to these paths for awareness
echo "[scan] grep references (may include harmless mentions):"
for base in "${ARR[@]}"; do
  rg -n "\b${base}/" "$ROOT_DIR" || true
done

if [[ "$MODE" == "dry" ]]; then
  echo "[dry-run] Would move into: $DEST"
  for p in "${PATHS[@]}"; do
    [[ -e "$p" ]] || { echo "  - skip (missing): $p"; continue; }
    echo "  - mv $p -> $DEST/$(basename "$p")"
  done
  exit 0
fi

mkdir -p "$DEST"
for p in "${PATHS[@]}"; do
  [[ -e "$p" ]] || { echo "  - skip (missing): $p"; continue; }
  echo "  - mv $p -> $DEST/$(basename "$p")"
  mv "$p" "$DEST/"
done
echo "[OK] archived at: $DEST"

