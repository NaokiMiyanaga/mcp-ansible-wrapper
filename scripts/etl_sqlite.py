#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
from typing import Any, Dict


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS networks (
            network_id TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT,
            network_id TEXT,
            PRIMARY KEY (node_id, network_id),
            FOREIGN KEY (network_id) REFERENCES networks(network_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS termination_points (
            node_id TEXT,
            network_id TEXT,
            tp_id TEXT,
            ipv4 TEXT,
            vlan INTEGER,
            role TEXT,
            PRIMARY KEY (node_id, network_id, tp_id),
            FOREIGN KEY (node_id, network_id) REFERENCES nodes(node_id, network_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS frr_status (
            node_id TEXT PRIMARY KEY,
            version TEXT,
            bgp_summary TEXT
        );
        CREATE TABLE IF NOT EXISTS bridge_status (
            node_id TEXT PRIMARY KEY,
            bridge_link TEXT,
            bridge_detail TEXT
        );
        """
    )
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, data: Dict[str, Any], pk_cols):
    cols = list(data.keys())
    placeholders = ",".join([":" + c for c in cols])
    collist = ",".join(cols)
    update_cols = [c for c in cols if c not in pk_cols]
    set_clause = ",".join([f"{c}=excluded.{c}" for c in update_cols])
    sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) " \
          f"ON CONFLICT({','.join(pk_cols)}) DO UPDATE SET {set_clause}"
    conn.execute(sql, data)


def load_ietf_json(conn: sqlite3.Connection, doc: Dict[str, Any]) -> None:
    root = doc.get("ietf-network:networks", {})
    networks = root.get("network", [])
    for net in networks:
        nid = net.get("network-id")
        if not nid:
            continue
        upsert(conn, "networks", {"network_id": nid}, ["network_id"])
        for n in net.get("node", [])
            or []:
            node_id = n.get("node-id")
            if not node_id:
                continue
            upsert(conn, "nodes", {"node_id": node_id, "network_id": nid}, ["node_id", "network_id"])
            for tp in n.get("ietf-network-topology:termination-point", []) or []:
                tp_id = tp.get("tp-id")
                ipv4 = tp.get("operational:ipv4")
                vlan = tp.get("operational:vlan")
                role = tp.get("operational:role")
                if tp_id:
                    upsert(conn, "termination_points", {
                        "node_id": node_id,
                        "network_id": nid,
                        "tp_id": tp_id,
                        "ipv4": ipv4,
                        "vlan": vlan,
                        "role": role,
                    }, ["node_id", "network_id", "tp_id"])

    op = root.get("operational", {})
    for fr in op.get("frr", []) or []:
        upsert(conn, "frr_status", {
            "node_id": fr.get("node"),
            "version": fr.get("version"),
            "bgp_summary": fr.get("bgp_summary"),
        }, ["node_id"])
    for br in op.get("bridge", []) or []:
        upsert(conn, "bridge_status", {
            "node_id": br.get("node"),
            "bridge_link": br.get("bridge_link"),
            "bridge_detail": br.get("bridge_detail"),
        }, ["node_id"])
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="ETL IETF network JSON into SQLite")
    ap.add_argument("--input", required=True, help="Path to IETF JSON (e.g., out/ops_ietf.json)")
    ap.add_argument("--db", required=True, help="Path to SQLite DB (created if missing)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        ensure_schema(conn)
        with open(args.input, "r") as f:
            doc = json.load(f)
        load_ietf_json(conn, doc)
        print(f"Loaded {args.input} into {args.db}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

