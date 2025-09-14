#!/usr/bin/env python3
import argparse
import json
import sys
from typing import Any, Dict, Iterable


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise SystemExit(f"JSONL parse error at line {ln}: {e}")
            yield obj


def validate_ietf(doc: Dict[str, Any]) -> None:
    root = doc.get("ietf-network:networks")
    if not isinstance(root, dict):
        raise SystemExit("ietf-network:networks must be an object")
    nets = root.get("network")
    if not isinstance(nets, list):
        raise SystemExit("networks.network must be an array")
    for i, net in enumerate(nets):
        if not isinstance(net, dict):
            raise SystemExit(f"network[{i}] must be an object")
        if "network-id" not in net:
            raise SystemExit(f"network[{i}] missing network-id")
        nodes = net.get("node", [])
        if nodes is None:
            nodes = []
        if not isinstance(nodes, list):
            raise SystemExit(f"network[{i}].node must be an array if present")
        for j, nd in enumerate(nodes):
            if "node-id" not in nd:
                raise SystemExit(f"network[{i}].node[{j}] missing node-id")
            tps = nd.get("ietf-network-topology:termination-point", [])
            if tps is None:
                tps = []
            if not isinstance(tps, list):
                raise SystemExit(f"network[{i}].node[{j}].termination-point must be an array if present")
            for k, tp in enumerate(tps):
                if "tp-id" not in tp:
                    raise SystemExit(f"network[{i}].node[{j}].tp[{k}] missing tp-id")


REQUIRED_BY_TYPE = {
    "network": ["network-id"],
    "node": ["network-id", "node-id"],
    "termination-point": ["network-id", "node-id", "tp-id"],
    "frr_status": ["node-id", "version"],
    "bridge_status": ["node-id"],
    "bgp_neighbor": ["node-id", "peer", "remote-as"],
    "interface": ["node-id", "name", "plane", "ipv4", "link"],
    "summary": ["node-id", "peers_total", "peers_established", "peers_not_established"],
    "route": ["node-id", "prefix"],
}


def validate_jsonl(path: str) -> None:
    for idx, obj in enumerate(iter_jsonl(path), 1):
        typ = obj.get("type")
        if typ not in REQUIRED_BY_TYPE:
            raise SystemExit(f"JSONL[{idx}]: unknown type: {typ}")
        for key in REQUIRED_BY_TYPE[typ]:
            if key not in obj:
                raise SystemExit(f"JSONL[{idx}]: type={typ} missing key: {key}")


def main():
    ap = argparse.ArgumentParser(description="Validate IETF JSON and JSONL produced by MCP")
    ap.add_argument("--ietf", help="Path to ops_ietf.json to validate")
    ap.add_argument("--ietf-schema", default="docs/schema/ietf-networks.schema.json", help="Path to IETF JSON Schema (default: docs/schema/ietf-networks.schema.json)")
    ap.add_argument("--jsonl", help="Path to objects.jsonl to validate")
    ap.add_argument("--jsonl-schema", default="docs/schema/jsonl.schema.json", help="Path to JSONL line schema (default: docs/schema/jsonl.schema.json)")
    args = ap.parse_args()

    ok = True
    try:
        # Try formal jsonschema if available; fallback to lightweight checks
        try:
            import jsonschema
            use_jsonschema = True
        except Exception:
            use_jsonschema = False

        if args.ietf:
            if use_jsonschema:
                schema = load_json(args.ietf_schema)
                jsonschema.validate(instance=load_json(args.ietf), schema=schema)
            else:
                validate_ietf(load_json(args.ietf))
            print(f"[OK] IETF JSON valid: {args.ietf}")

        if args.jsonl:
            if use_jsonschema:
                schema = load_json(args.jsonl_schema)
                try:
                    import jsonschema
                    validator = jsonschema.Draft202012Validator(schema)
                except Exception:
                    validator = jsonschema.Draft7Validator(schema)
                for i, obj in enumerate(iter_jsonl(args.jsonl), 1):
                    errors = sorted(validator.iter_errors(obj), key=lambda e: e.path)
                    if errors:
                        msg = "; ".join([e.message for e in errors])
                        raise SystemExit(f"JSONL[{i}] invalid: {msg}")
            else:
                validate_jsonl(args.jsonl)
            print(f"[OK] JSONL valid: {args.jsonl}")
    except SystemExit as e:
        print(f"[ERROR] {e}")
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
