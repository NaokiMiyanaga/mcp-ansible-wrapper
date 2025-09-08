#!/usr/bin/env python3
import argparse
import os
import sqlite3
from typing import Optional


def q1(conn, sql, args=()):
    cur = conn.execute(sql, args)
    return cur.fetchone()[0]


def qall(conn, sql, args=()):
    cur = conn.execute(sql, args)
    return cur.fetchall()


def inspect(db_path: str, limit: int, type_filter: Optional[str], node: Optional[str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        print(f"DB: {db_path}")
        total = q1(conn, "SELECT COUNT(*) FROM docs")
        print(f"- total rows (docs): {total}")

        print("- counts by type:")
        for t, c in qall(conn, "SELECT type, COUNT(*) FROM docs GROUP BY type ORDER BY 2 DESC"):
            print(f"  {t or 'NULL'}: {c}")

        print("- nodes by network-id (top):")
        for nid, c in qall(conn, "SELECT network_id, COUNT(*) FROM docs WHERE type='node' GROUP BY network_id ORDER BY 2 DESC"):
            print(f"  {nid or 'NULL'}: {c}")

        where = []
        args = []
        if type_filter:
            where.append("type = ?")
            args.append(type_filter)
        if node:
            where.append("node_id = ?")
            args.append(node)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        print(f"- sample rows (limit {limit})" + (f" where {where_sql[7:]}" if where else "") + ":")
        for row in qall(
            conn,
            f"SELECT rowid, type, network_id, node_id, tp_id, substr(text,1,120) FROM docs{where_sql} LIMIT ?",
            (*args, limit),
        ):
            print("  ", row)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Inspect contents of rag.db/docs for quick debugging")
    ap.add_argument("--db", required=True, help="Path to SQLite DB (e.g., ~/devNet/ietf-network-schema/rag.db)")
    ap.add_argument("--limit", type=int, default=10, help="Number of sample rows to show")
    ap.add_argument("--type", dest="type_filter", help="Filter by type (e.g., node, network, termination-point, frr_status)")
    ap.add_argument("--node", help="Filter by node-id (e.g., r1)")
    ap.add_argument("--report", action="store_true", help="Print BGP and interface summaries")
    args = ap.parse_args()

    db = os.path.expanduser(args.db)
    if not os.path.exists(db):
        raise SystemExit(f"DB not found: {db}")
    if args.report:
        conn = sqlite3.connect(db)
        try:
            c = conn.cursor()
            print("[BGP] Established per node:")
            for row in c.execute("""
                SELECT json_extract(json,'$.node-id') AS node,
                       SUM(CASE WHEN json_extract(json,'$.peers_established') IS NOT NULL THEN json_extract(json,'$.peers_established') ELSE 0 END) AS established,
                       SUM(CASE WHEN json_extract(json,'$.peers_total') IS NOT NULL THEN json_extract(json,'$.peers_total') ELSE 0 END) AS total
                FROM docs WHERE type='summary'
                GROUP BY node ORDER BY node
            """):
                print("  ", row)
            print("[IF] link up but proto down (per node):")
            for row in c.execute("""
                SELECT json_extract(json,'$.node-id') AS node,
                       SUM(CASE WHEN json_extract(json,'$.if_link_up_proto_down') IS NOT NULL THEN json_extract(json,'$.if_link_up_proto_down') ELSE 0 END) as mismatches
                FROM docs WHERE type='summary'
                GROUP BY node ORDER BY mismatches DESC
            """):
                print("  ", row)
        finally:
            conn.close()
    else:
        inspect(db, args.limit, args.type_filter, args.node)


if __name__ == "__main__":
    main()
