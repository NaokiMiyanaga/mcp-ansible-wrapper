#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cmdb_update.py - helper to perform '/mcp cmdb update' equivalent:
# 1) Apply schema_sql to the SQLite DB
# 2) Run scripts/mcp_ingest_state.py with snapshot+schema_meta
# 3) Run verify

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def apply_schema(db: str, schema_sql: str, verbose: bool) -> None:
    if not os.path.exists(schema_sql):
        raise FileNotFoundError(f"schema-sql not found: {schema_sql}")
    sql = Path(schema_sql).read_text(encoding="utf-8")
    conn = sqlite3.connect(db)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    if verbose:
        print(json.dumps({"ts": iso_now(), "event": "schema.apply.ok", "db": db, "schema_sql": schema_sql}))


def run_ingest(wrapper_dir: str, db: str, token: str | None, mcp_base: str | None,
               alias_file: str | None, json_log: bool, verbose: bool) -> int:
    script = str(Path(wrapper_dir) / "scripts" / "mcp_ingest_state.py")
    if not Path(script).exists():
        raise FileNotFoundError(f"mcp_ingest_state.py not found at {script}")
    cmd = [sys.executable, script, "--db", db, "--snapshot", "--schema-meta"]
    if alias_file:
        cmd += ["--alias-file", alias_file]
    if json_log:
        cmd += ["--json-log"]
    if verbose:
        cmd += ["--verbose"]
    # environment passthrough
    env = os.environ.copy()
    if token:
        env["MCP_TOKEN"] = token
    if mcp_base:
        env["MCP_BASE"] = mcp_base
    return subprocess.call(cmd, env=env)


def run_verify(wrapper_dir: str, db: str, json_log: bool, verbose: bool) -> int:
    script = str(Path(wrapper_dir) / "scripts" / "mcp_ingest_state.py")
    cmd = [sys.executable, script, "--db", db, "--verify"]
    if json_log:
        cmd += ["--json-log"]
    if verbose:
        cmd += ["--verbose"]
    return subprocess.call(cmd)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--schema-sql", required=True)
    ap.add_argument("--wrapper-dir", default="/Users/naoki/devNet/mcp-ansible-wrapper")
    ap.add_argument("--alias-file", default="/Users/naoki/devNet/mcp-ansible-wrapper/key_aliases.yml")
    ap.add_argument("--mcp-base", default=os.getenv("MCP_BASE"))
    ap.add_argument("--token", default=os.getenv("MCP_TOKEN"))
    ap.add_argument("--json-log", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    apply_schema(args.db, args.schema_sql, args.verbose)
    rc1 = run_ingest(args.wrapper_dir, args.db, args.token, args.mcp_base, args.alias_file, args.json_log, args.verbose)
    rc2 = run_verify(args.wrapper_dir, args.db, args.json_log, args.verbose)
    return 0 if rc1 == 0 and rc2 == 0 else (rc1 or rc2 or 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
