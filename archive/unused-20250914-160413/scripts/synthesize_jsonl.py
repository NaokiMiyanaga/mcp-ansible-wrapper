#!/usr/bin/env python3
import argparse
import re
from pathlib import Path


def parse_inventory(path: Path):
    groups = {}
    cur = None
    for line in path.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        m = re.match(r"\[(.+)\]", s)
        if m:
            cur = m.group(1)
            groups.setdefault(cur, [])
            continue
        if cur:
            host = s.split()[0]
            if host and not host.startswith('['):
                groups[cur].append(host)
    return groups


def short(name: str) -> str:
    return name.split('.')[0]


def main():
    ap = argparse.ArgumentParser(description="Append synthetic JSONL defaults for missing operational data")
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--append", required=True, help="objects.jsonl to append to")
    args = ap.parse_args()

    inv = parse_inventory(Path(args.inventory))
    out = Path(args.append)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = []

    # L2 switches: placeholder bridge_status and a synthetic interface up
    for h in inv.get('linux_bridge', []):
        nid = short(h)
        lines.append({
            "type": "bridge_status",
            "node-id": nid,
            "bridge_link": "",
            "bridge_detail": "",
            "snapshot_at": "synthetic",
            "generator": "nlctl-synth",
            "schema_version": "1"
        })
        lines.append({
            "type": "interface",
            "node-id": nid,
            "name": "synthetic0",
            "plane": "unknown",
            "ipv4": "",
            "link": "up",
            "proto": "unknown",
            "snapshot_at": "synthetic",
            "generator": "nlctl-synth",
            "schema_version": "1"
        })

    # Routers: placeholder interface up if nothing else exists
    for h in inv.get('frr', []):
        nid = short(h)
        lines.append({
            "type": "interface",
            "node-id": nid,
            "name": "synthetic0",
            "plane": "unknown",
            "ipv4": "",
            "link": "up",
            "proto": "unknown",
            "snapshot_at": "synthetic",
            "generator": "nlctl-synth",
            "schema_version": "1"
        })

    with out.open("a", encoding="utf-8") as f:
        for obj in lines:
            import json
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    print(f"Appended {len(lines)} synthetic rows into {out}")


if __name__ == "__main__":
    main()

