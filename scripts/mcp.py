#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import Dict, List, Any

try:
    import yaml  # PyYAML
except Exception as e:
    print("PyYAML not available inside container. Rebuild the image.", file=sys.stderr)
    raise

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
WORK = "/work"  # container workdir mount
DEFAULT_POLICY = "/work/policy/master.ietf.yaml"


def run(cmd: List[str], dry_run: bool = False) -> int:
    print("$", " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        return 0
    return subprocess.run(cmd).returncode


# ---------------------- Policy helpers ----------------------

def _ip_of_tp(node: Dict[str, Any]) -> str:
    tp = node.get("ietf-network-topology:termination-point", [])
    if not tp:
        return ""
    ip = tp[0].get("operational:ipv4", "")
    return ip.split("/")[0] if ip else ""


def load_policy(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    root = data.get("ietf-network:networks", data)
    networks = root.get("network", [])
    operational = root.get("operational", {})

    # Build per-plane maps
    mgmt_ip: Dict[str, str] = {}
    svc_ip: Dict[str, str] = {}
    for net in networks:
        nid = net.get("network-id")
        if nid == "management-plane":
            for n in net.get("node", []):
                mgmt_ip[n["node-id"]] = _ip_of_tp(n)
        elif nid == "service-plane":
            for n in net.get("node", []):
                svc_ip[n["node-id"]] = _ip_of_tp(n)

    # FRR/BGP intent
    bgp = operational.get("bgp", {})
    asn_map: Dict[str, int] = bgp.get("asn", {})
    router_id_map: Dict[str, str] = bgp.get("router_id", {})
    peering: List[Dict[str, Any]] = bgp.get("peering", [])

    frr_apply: Dict[str, Any] = {}
    for node in asn_map.keys():
        rid = router_id_map.get(node) or mgmt_ip.get(node)
        if not rid:
            raise ValueError(f"router_id for {node} missing and mgmt IP not found")
        nbrs: List[Dict[str, Any]] = []
        for p in peering:
            if p.get("a") == node:
                other = p.get("z")
            elif p.get("z") == node:
                other = p.get("a")
            else:
                continue
            peer_ip = svc_ip.get(other)
            if not peer_ip:
                raise ValueError(f"service-plane IP for peer {other} not found")
            remote_as = asn_map.get(other)
            if not remote_as:
                raise ValueError(f"ASN for peer {other} not found")
            nbrs.append({"ip": peer_ip, "remote_as": remote_as})
        frr_apply[node] = {"asn": asn_map[node], "router_id": rid, "neighbors": nbrs}

    # Bridge intent (summary for now)
    l2sw = operational.get("l2sw", {})
    l2_nodes = {n.get("id"): n for n in l2sw.get("nodes", [])}
    # derive VLANs per L2 node from topology networks
    per_l2_vlans: Dict[str, List[int]] = {k: [] for k in l2_nodes.keys()}
    for net in networks:
        if not str(net.get("network-id", "")).startswith("vlan"):
            continue
        vlan_id = None
        # try to parse from nodes' termination-point
        for n in net.get("node", []):
            if n.get("node-id") in per_l2_vlans:
                tps = n.get("ietf-network-topology:termination-point", [])
                if tps:
                    v = tps[0].get("operational:vlan")
                    if isinstance(v, int):
                        vlan_id = v
        if vlan_id is None:
            # fallback: from operational.vlans[] list
            for v in operational.get("vlans", []):
                if str(v.get("vlan-id")) in str(net.get("network-id")):
                    vlan_id = v.get("vlan-id")
                    break
        if vlan_id is None:
            continue
        for n in net.get("node", []):
            nid = n.get("node-id")
            if nid in per_l2_vlans and vlan_id not in per_l2_vlans[nid]:
                per_l2_vlans[nid].append(vlan_id)

    bridge_apply = {k: {"vlans": sorted(v)} for k, v in per_l2_vlans.items()}

    # Addressing
    mgmt_subnet = operational.get("addressing", {}).get("management", {}).get("ipv4_subnet", "")
    svc_subnet = operational.get("addressing", {}).get("service", {}).get("ipv4_subnet", "")

    # VLAN metadata (id -> ipv4_subnet, etc.)
    vlans_meta: Dict[int, Dict[str, Any]] = {}
    for v in operational.get("vlans", []):
        vid = v.get("vlan-id")
        if isinstance(vid, int):
            vlans_meta[vid] = {k: v.get(k) for k in ("ipv4_subnet", "svi")}

    # Compose network name mapping (if provided)
    compose_networks = operational.get("compose-mapping", {}).get("networks", {})

    return {
        "mgmt_ip": mgmt_ip,
        "svc_ip": svc_ip,
        "frr_apply": frr_apply,
        "bridge_apply": bridge_apply,
        "mgmt_subnet": mgmt_subnet,
        "svc_subnet": svc_subnet,
        "vlans_meta": vlans_meta,
        "compose_networks": compose_networks,
    }


def write_extra_vars(obj: Dict[str, Any]) -> str:
    path = "/tmp/mcp_extra_vars.json"
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


# ---------------------- Policy overlay merge ----------------------

def _deep_merge(dst: Any, src: Any) -> Any:
    if isinstance(dst, dict) and isinstance(src, dict):
        for k, v in src.items():
            if k in dst:
                dst[k] = _deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst
    # lists and scalars: replace by src
    return src


def _merge_vlans(base_oper: Dict[str, Any], overlay_oper: Dict[str, Any]) -> None:
    """Merge operational.vlans by vlan-id if overlay provides a list.
    Updates existing entries by vlan-id, appends new ones if missing.
    """
    if not isinstance(base_oper, dict) or not isinstance(overlay_oper, dict):
        return
    o_vlans = overlay_oper.get("vlans")
    if not isinstance(o_vlans, list):
        return
    b_vlans = base_oper.get("vlans")
    if not isinstance(b_vlans, list):
        b_vlans = []
        base_oper["vlans"] = b_vlans
    # index base by vlan-id
    idx = {}
    for i, v in enumerate(b_vlans):
        vid = v.get("vlan-id")
        if isinstance(vid, int):
            idx[vid] = i
    for ov in o_vlans:
        vid = ov.get("vlan-id")
        if isinstance(vid, int) and vid in idx:
            # deep-merge existing vlan dict
            b_vlans[idx[vid]] = _deep_merge(b_vlans[idx[vid]], ov)
        else:
            b_vlans.append(ov)


def render_policy_with_overlays(base_path: str, overlays: List[str]) -> Dict[str, Any]:
    with open(base_path, "r") as f:
        merged = yaml.safe_load(f)
    for path in overlays:
        with open(path, "r") as f:
            ov = yaml.safe_load(f)
        # Prefer operational subtree merge to avoid list merge pitfalls under network[]
        base_root = merged.get("ietf-network:networks", merged)
        ov_root = ov.get("ietf-network:networks", ov)
        # Special-case operational.vlans merge by vlan-id
        if "operational" in ov_root:
            base_oper = base_root.setdefault("operational", {})
            ov_oper = ov_root.get("operational", {})
            _merge_vlans(base_oper, ov_oper)
            # Deep-merge the rest of operational
            base_root["operational"] = _deep_merge(base_oper, {k: v for k, v in ov_oper.items() if k != "vlans"})
        else:
            # Fallback: deep merge whole root (use carefully)
            merged = _deep_merge(merged, ov)
    return merged


# ---------------------- Commands ----------------------

def cmd_ping(args) -> int:
    cmd = [
        "ansible", "-i", "inventory/hosts.ini",
        *( ["-l", args.limit] if args.limit else [] ),
        "all", "-m", "ping"
    ]
    return run(cmd, args.dry_run)


def cmd_frr_check(args) -> int:
    cmd = [
        "ansible-playbook", "-i", "inventory/hosts.ini",
        "-l", args.limit or "frr",
        "playbooks/frr_check.yml",
    ]
    return run(cmd, args.dry_run)


def cmd_bridge_check(args) -> int:
    cmd = [
        "ansible-playbook", "-i", "inventory/hosts.ini",
        "-l", args.limit or "linux_bridge",
        "playbooks/bridge_check.yml",
    ]
    return run(cmd, args.dry_run)


def cmd_install_collections(args) -> int:
    cmd = [
        "ansible-galaxy", "collection", "install",
        "-r", "collections/requirements.yml",
        "-p", "collections",
    ]
    return run(cmd, args.dry_run)


def cmd_export_ops(args) -> int:
    # Collect operational data; optionally render IETF-style JSON
    fmt = getattr(args, "format", "raw")
    extra: Dict[str, Any] = {"format": fmt}
    # When IETF format is requested, pass addressing and vlan metadata for topology mapping
    if fmt in ("ietf", "jsonl"):
        intent = load_policy(args.policy or DEFAULT_POLICY)
        extra.update({
            "mgmt_subnet": intent.get("mgmt_subnet", ""),
            "svc_subnet": intent.get("svc_subnet", ""),
            "vlans_meta": intent.get("vlans_meta", {}),
        })
    # Resolve destination path
    dest = getattr(args, "output", None)
    if not dest:
        # defaults inside container mount (persisted on host)
        if fmt == "ietf":
            dest = "/work/output/ops_ietf.json"
        elif fmt == "jsonl":
            dest = "/work/output/objects.jsonl"
        else:
            dest = "/work/output/ops.json"
    # snapshot handling (for ietf only typically)
    if getattr(args, "snapshot", False):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # if dest looks like a directory, write file into it; else append suffix before .json
        if dest.endswith("/"):
            os.makedirs(dest, exist_ok=True)
            base = os.path.join(dest, f"ops_ietf-{ts}.json")
            dest = base
        else:
            root, ext = os.path.splitext(dest)
            dest = f"{root}-{ts}{ext or '.json'}"
    extra["dest_path"] = dest
    extra_file = write_extra_vars(extra)
    cmd = [
        "ansible-playbook", "-i", "inventory/hosts.ini",
        "-e", f"@{extra_file}",
        "playbooks/ops_export.yml",
    ]
    return run(cmd, args.dry_run)


def cmd_apply(args) -> int:
    policy_path = args.policy or DEFAULT_POLICY
    intent = load_policy(policy_path)
    extra = {
        "frr_apply": intent["frr_apply"],
        "bridge_apply": intent["bridge_apply"],
        "mgmt_subnet": intent.get("mgmt_subnet", ""),
        "vlans_meta": intent.get("vlans_meta", {}),
    }
    extra_file = write_extra_vars(extra)

    ret = 0
    if args.component in ("frr", "all"):
        ret = ret or run([
            "ansible-playbook", "-i", "inventory/hosts.ini",
            "-l", args.limit or "frr",
            "-e", f"@{extra_file}",
            "playbooks/frr_apply.yml",
        ], args.dry_run)
    if args.component in ("bridge", "all"):
        ret = ret or run([
            "ansible-playbook", "-i", "inventory/hosts.ini",
            "-l", args.limit or "linux_bridge",
            "-e", f"@{extra_file}",
            "playbooks/bridge_apply.yml",
        ], args.dry_run)
    return ret


def cmd_infra_build(args) -> int:
    policy_path = args.policy or DEFAULT_POLICY
    intent = load_policy(policy_path)
    extra = {
        "compose_networks": intent.get("compose_networks", {}),
        "mgmt_subnet": intent.get("mgmt_subnet", ""),
        "svc_subnet": intent.get("svc_subnet", ""),
        "vlans_meta": intent.get("vlans_meta", {}),
        "mgmt_ip": intent.get("mgmt_ip", {}),
        "svc_ip": intent.get("svc_ip", {}),
        # Container creation is optional (default: false)
        "create_containers": getattr(args, "create_containers", False),
        # Images can be overridden via extra vars as needed
        "frr_image": getattr(args, "frr_image", None) or "frrouting/frr:8.4.4",
        "l2_image": getattr(args, "l2_image", None) or "debian:bookworm-slim",
        "host_image": getattr(args, "host_image", None) or "alpine:3.19",
    }
    extra_file = write_extra_vars(extra)
    cmd = [
        "ansible-playbook", "-i", "inventory/hosts.ini",
        "-l", "localhost",
        "-e", f"@{extra_file}",
        "playbooks/infra_build.yml",
    ]
    return run(cmd, args.dry_run)


def cmd_policy_render(args) -> int:
    base = args.policy or DEFAULT_POLICY
    overlays = args.overlay or []
    if not overlays:
        print("No overlays provided; copying base policy.")
    merged = render_policy_with_overlays(base, overlays)
    out_path = args.out or "/work/output/policies/effective.yaml"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote merged policy: {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="mcp", description="Minimal MCP wrapper for Ansible ops")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("-l", "--limit", help="inventory limit (e.g., r1.example)")
        p.add_argument("--dry-run", action="store_true", help="print commands only")

    p_ping = sub.add_parser("ping", help="Ansible ping for all hosts")
    add_common(p_ping)
    p_ping.set_defaults(func=cmd_ping)

    p_frr = sub.add_parser("frr.check", help="FRR status check (vtysh)")
    add_common(p_frr)
    p_frr.set_defaults(func=cmd_frr_check)

    p_br = sub.add_parser("bridge.check", help="Linux bridge status check")
    add_common(p_br)
    p_br.set_defaults(func=cmd_bridge_check)

    p_inst = sub.add_parser("install-collections", help="Install Ansible collections")
    p_inst.add_argument("--dry-run", action="store_true", help="print commands only")
    p_inst.set_defaults(func=cmd_install_collections)

    p_apply = sub.add_parser("apply", help="Apply intent from policy to targets")
    add_common(p_apply)
    p_apply.add_argument("--policy", default=DEFAULT_POLICY, help="path to policy YAML (default: %(default)s)")
    p_apply.add_argument("--component", choices=["all", "frr", "bridge"], default="all")
    p_apply.set_defaults(func=cmd_apply)

    p_export = sub.add_parser("export-ops", help="Collect and export operational data to out/ops.json")
    p_export.add_argument("--format", choices=["raw", "ietf", "jsonl"], default="raw", help="export format (default: raw)")
    p_export.add_argument("--policy", default=DEFAULT_POLICY, help="policy file for context when format=ietf")
    p_export.add_argument("--output", help="destination path inside container (default: /work/out/ops.json or ops_ietf.json)")
    p_export.add_argument("--snapshot", action="store_true", help="append UTC timestamp to filename (or write into directory if --output ends with /)")
    p_export.add_argument("--dry-run", action="store_true", help="print commands only")
    p_export.set_defaults(func=cmd_export_ops)

    p_infra = sub.add_parser("infra.build", help="Create Docker networks/infra from policy")
    p_infra.add_argument("--policy", default=DEFAULT_POLICY, help="path to policy YAML (default: %(default)s)")
    p_infra.add_argument("--create-containers", action="store_true", dest="create_containers", help="also create containers (r1/r2/l2a/l2b/h10/h20)")
    p_infra.add_argument("--frr-image", dest="frr_image", help="FRR router image (default: frrouting/frr:8.4.4)")
    p_infra.add_argument("--l2-image", dest="l2_image", help="L2 switch image (default: debian:bookworm-slim)")
    p_infra.add_argument("--host-image", dest="host_image", help="Host image (default: alpine:3.19)")
    p_infra.add_argument("--dry-run", action="store_true", help="print commands only")
    p_infra.set_defaults(func=cmd_infra_build)

    # Policy render (merge overlays)
    p_pr = sub.add_parser("policy.render", help="Merge overlays into base policy and write effective YAML")
    p_pr.add_argument("--policy", default=DEFAULT_POLICY, help="base policy path (default: %(default)s)")
    p_pr.add_argument("--overlay", action="append", help="overlay YAML (can be specified multiple times)")
    p_pr.add_argument("--out", help="output path (default: /work/output/policies/effective.yaml)")
    p_pr.add_argument("--dry-run", action="store_true", help="no-op (reserved)")
    p_pr.set_defaults(func=cmd_policy_render)

    # if.fix-unknown: bring up interfaces with unknown state
    def _cmd_if_fix_unknown(args) -> int:
        return run([
            "ansible-playbook", "-i", "inventory/hosts.ini",
            "-l", args.limit or "frr",
            "playbooks/if_fix_unknown.yml",
        ], args.dry_run)

    p_if_fix = sub.add_parser("if.fix-unknown", help="Bring up interfaces with unknown state (exclude lo)")
    add_common(p_if_fix)
    p_if_fix.set_defaults(func=_cmd_if_fix_unknown)

    # Interface toggle (by plane)
    def add_common_if(p):
        p.add_argument("-l", "--limit", help="inventory limit (e.g., r1.example)")
        p.add_argument("--dry-run", action="store_true", help="print commands only")
        p.add_argument("--policy", default=DEFAULT_POLICY, help="policy file for subnet context")
        p.add_argument("--plane", choices=["service", "management"], default="service")
        p.add_argument("--state", choices=["up", "down"], required=True)
        p.add_argument("--if-name", dest="if_name", help="interface name (override plane auto-detect)")

    def _cmd_if(args) -> int:
        intent = load_policy(args.policy or DEFAULT_POLICY)
        extra = {
            "mgmt_subnet": intent.get("mgmt_subnet", ""),
            "svc_subnet": intent.get("svc_subnet", ""),
            "plane": args.plane,
            "state": args.state,
        }
        if getattr(args, "if_name", None):
            extra["if_name"] = args.if_name
        extra_file = write_extra_vars(extra)
        return run([
            "ansible-playbook", "-i", "inventory/hosts.ini",
            *( ["-l", args.limit] if args.limit else [] ),
            "-e", f"@{extra_file}",
            "playbooks/if_toggle.yml",
        ], args.dry_run)

    p_if = sub.add_parser("if.toggle", help="Toggle interface up/down by plane (service/management)")
    add_common_if(p_if)
    p_if.set_defaults(func=_cmd_if)

    # Interface IPv4 management
    def _cmd_if_addr(args) -> int:
        extra = {
            "if_name": args.if_name,
            "addr": args.addr or "",
            "action": args.action,
        }
        extra_file = write_extra_vars(extra)
        return run([
            "ansible-playbook", "-i", "inventory/hosts.ini",
            *( ["-l", args.limit] if args.limit else [] ),
            "-e", f"@{extra_file}",
            "playbooks/if_addr.yml",
        ], args.dry_run)

    p_ifaddr = sub.add_parser("if.addr", help="Manage IPv4 on an interface (add/del/replace/flush/show)")
    add_common(p_ifaddr)
    p_ifaddr.add_argument("--if-name", required=True)
    p_ifaddr.add_argument("--addr", help="CIDR address, e.g., 10.0.0.10/24")
    p_ifaddr.add_argument("--action", choices=["add","del","replace","flush","show"], default="show")
    p_ifaddr.set_defaults(func=_cmd_if_addr)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
