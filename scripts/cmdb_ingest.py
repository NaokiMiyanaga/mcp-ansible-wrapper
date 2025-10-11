#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cmdb_ingest.py
-------------------
取得済みJSONデータ（BGP/OSPFなど）を受け取り、CMDB（rag.dbなど）にupsert・DIFF集計・サマリ生成を行う専用スクリプト。
"""
import os
import json
import sqlite3
import argparse
from typing import Any, Dict, List, Tuple


def upsert_to_cmdb(db_path: str, json_path: str):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # ここではBGP/OSPFデータのupsert例のみ（詳細は既存mcp_ingest_state.pyを参照）
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    bgp_rows = data.get("bgp_rows", [])
    ospf_rows = data.get("ospf_rows", [])
    # 例: routing_bgp_peer/routing_ospf_neighborテーブルへのupsert
    for r in bgp_rows:
        cur.execute("INSERT OR REPLACE INTO routing_bgp_peer VALUES (?,?,?,?,?,?,?,?)", tuple(r))
    for r in ospf_rows:
        cur.execute("INSERT OR REPLACE INTO routing_ospf_neighbor VALUES (?,?,?,?,?,?,?)", tuple(r))
    conn.commit()
    # DIFFサマリ集計例
    res = cur.execute("SELECT change, COUNT(*) FROM summary_diff GROUP BY change").fetchall()
    diff_summary = {row[0]: row[1] for row in res}
    print(json.dumps({"status": "ok", "diff_summary": diff_summary}, ensure_ascii=False, indent=2))
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", required=True)
    args = ap.parse_args()
    upsert_to_cmdb(args.db, args.json)


if __name__ == "__main__":
    main()
