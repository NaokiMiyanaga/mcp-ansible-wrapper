#!/usr/bin/env python3
import argparse
import json
import sys


def emit(line_obj, out):
    out.write(json.dumps(line_obj, ensure_ascii=False) + "\n")


def convert(doc, out):
    root = doc.get("ietf-network:networks", {})
    # networks
    for net in root.get("network", []) or []:
        nid = net.get("network-id")
        if not nid:
            continue
        emit({"type": "network", "network_id": nid}, out)
        for n in net.get("node", []) or []:
            node_id = n.get("node-id")
            if not node_id:
                continue
            emit({"type": "node", "network_id": nid, "node_id": node_id}, out)
            for tp in n.get("ietf-network-topology:termination-point", []) or []:
                rec = {
                    "type": "termination-point",
                    "network_id": nid,
                    "node_id": node_id,
                    "tp_id": tp.get("tp-id"),
                }
                if "operational:ipv4" in tp:
                    rec["ipv4"] = tp.get("operational:ipv4")
                if "operational:vlan" in tp:
                    rec["vlan"] = tp.get("operational:vlan")
                if "operational:role" in tp:
                    rec["role"] = tp.get("operational:role")
                emit(rec, out)

    # operational extras
    op = root.get("operational", {})
    for fr in op.get("frr", []) or []:
        emit({
            "type": "frr_status",
            "node_id": fr.get("node"),
            "version": fr.get("version"),
            "bgp_summary": fr.get("bgp_summary"),
        }, out)
    for br in op.get("bridge", []) or []:
        emit({
            "type": "bridge_status",
            "node_id": br.get("node"),
            "bridge_link": br.get("bridge_link"),
            "bridge_detail": br.get("bridge_detail"),
        }, out)


def main():
    ap = argparse.ArgumentParser(description="Convert IETF networks JSON to JSONL objects")
    ap.add_argument("--input", required=True, help="Path to ops_ietf.json")
    ap.add_argument("--output", required=True, help="Path to write objects.jsonl")
    args = ap.parse_args()

    with open(args.input, "r") as f:
        doc = json.load(f)
    with open(args.output, "w") as out:
        convert(doc, out)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    sys.exit(main())

