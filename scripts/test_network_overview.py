#!/usr/bin/env python3
"""Test helper for playbooks/network_overview.yml (no ingest).

Purpose:
  - Execute the Ansible playbook `playbooks/network_overview.yml` with JSON stdout callback
  - Extract per-host `network_overview_entry` debug messages
  - Summarize simple metrics (host count, interface counts, BGP presence)
  - Output a concise JSON summary to stdout

Assumptions:
  - `ansible-playbook` is available in PATH
  - Inventory path is provided (defaults to ./inventory or ./inventory.ini)
  - Playbook uses a debug task named: "Emit network overview (JSON)"

Usage:
  python scripts/test_network_overview.py \
      --inventory mcp-ansible-wrapper/inventory \
      --playbook mcp-ansible-wrapper/playbooks/network_overview.yml

Exit codes:
  0 success
  2 ansible-playbook failed
  3 parse error or structure missing

NOTE: This script intentionally avoids any CMDB ingest logic per user request.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, shutil, tempfile, pathlib, re
from typing import Any, Dict, List

PLAYBOOK_TASK_NAME = "Emit network overview (JSON)"

# Container path hints
CONTAINER_ROOT_MARKER = "/app/ansible-mcp"


def _resolve_path(p: str) -> str:
    """Best-effort path normalization so the same CLI defaults work:

    Scenarios:
      - Host repo root: paths may be prefixed with mcp-ansible-wrapper/
      - Inside container WORKDIR=/app/ansible-mcp: prefix should be stripped
      - Inventory may be a directory (inventory/) or a file (inventory.ini)
    """
    if not p:
        return p
    # If path exists as-is, keep
    if os.path.exists(p):
        return p
    prefix = "mcp-ansible-wrapper/"
    if p.startswith(prefix):
        alt = p[len(prefix):]
        if os.path.exists(alt):
            return alt
    # Fallback to inventory.ini if inventory dir missing
    if "inventory" in p and not os.path.exists(p):
        for candidate in ["inventory.ini", "inventory", "./inventory.ini", "./inventory"]:
            if os.path.exists(candidate):
                return candidate
    return p


def _attempt_substring_json(raw: str) -> tuple[Any, str | None]:
    """Best-effort salvage: locate the *largest* JSON object in stdout.

    Strategy:
      - Find first '{' and last '}' – naive wide net.
      - Also specifically look for a block starting with '{\n    "plays":' to reduce risk of grabbing unrelated braces.
      - Try longest candidate first; on failure, progressively trim leading noise until json loads or exhaustion.
    Returns (obj, note) where note describes salvage method used.
    """
    if not raw:
        return None, None
    candidates: list[tuple[str, str]] = []
    first = raw.find('{')
    last = raw.rfind('}')
    if first != -1 and last != -1 and last > first:
        candidates.append((raw[first:last+1], 'first_to_last_brace'))
    # Targeted pattern for Ansible JSON callback
    m = re.search(r'(\{\s*\n\s*"plays"\s*:\s*\[.*)', raw, re.DOTALL)
    if m:
        segment = m.group(1)
        # ensure closing brace
        closing = segment.rfind('}')
        if closing != -1:
            candidates.insert(0, (segment[:closing+1], 'plays_block'))
    # Deduplicate by text
    seen = set()
    uniq: list[tuple[str, str]] = []
    for txt, label in candidates:
        if txt in seen:
            continue
        seen.add(txt)
        uniq.append((txt, label))
    for txt, label in uniq:
        try:
            return json.loads(txt), label
        except Exception:
            continue
    # Progressive trim: strip leading non-{ lines
    lines = raw.splitlines()
    while lines:
        if lines[0].lstrip().startswith('{'):
            attempt = '\n'.join(lines)
            try:
                return json.loads(attempt), 'progressive_trim'
            except Exception:
                pass
        lines.pop(0)
    return None, None


def run_playbook(playbook: str, inventory: str, limit: str | None = None, timeout: int = 300, ansible_verbose: bool = False) -> Dict[str, Any]:
    if shutil.which("ansible-playbook") is None:
        print(json.dumps({"ok": False, "error": "ansible-playbook not found in PATH"}))
        sys.exit(2)
    env = os.environ.copy()
    # Force json callback for structured parsing
    env["ANSIBLE_STDOUT_CALLBACK"] = "json"
    cmd = ["ansible-playbook", playbook, "-i", inventory]
    if ansible_verbose:
        cmd.append("-vvv")
    if limit:
        cmd += ["-l", limit]
    try:
        cp = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "_exec_error": True,
            "ok": False,
            "error": f"timeout after {timeout}s",
            "rc": -1,
            "cmd": cmd,
        }
    result: Dict[str, Any]
    parse_error = None
    data_obj = None
    full_stdout = cp.stdout
    salvage_note = None
    if full_stdout:
        try:
            data_obj = json.loads(full_stdout)
        except Exception as e:  # noqa: BLE001
            parse_error = str(e)
            # Try substring salvage
            data_obj, salvage_note = _attempt_substring_json(full_stdout)
            if data_obj is not None:
                parse_error = None  # Treat as recovered
    result = {
        "raw_stdout": full_stdout[-800:],
        "raw_stderr": cp.stderr[-800:],
        "rc": cp.returncode,
        "cmd": cmd,
        "parsed": data_obj,
        "parse_error": parse_error,
        "parse_salvage": salvage_note,
    }
    return result


def extract_entries(ansible_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    plays = ansible_json.get("plays") or []
    entries: List[Dict[str, Any]] = []
    for p in plays:
        for task in p.get("tasks", []):
            tinfo = task.get("task") or {}
            tname = tinfo.get("name")
            if tname != PLAYBOOK_TASK_NAME:
                continue
            hosts = task.get("hosts") or {}
            for h, hv in hosts.items():
                msg = hv.get("msg")
                if isinstance(msg, dict):
                    # Already parsed dict (rare) – standard callback gives string
                    entry = msg
                else:
                    try:
                        entry = json.loads(msg) if isinstance(msg, str) else None
                    except Exception:
                        entry = None
                if entry:
                    entry["_host"] = h
                    entries.append(entry)
    return entries


def detect_failures(ansible_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect failed task info per host from ansible JSON callback structure.

    Returns list of {host, task, msg, stderr, rc}.
    """
    failures: List[Dict[str, Any]] = []
    for play in ansible_json.get("plays", []):
        for task in play.get("tasks", []):
            tinfo = task.get("task") or {}
            tname = tinfo.get("name")
            for host, hv in (task.get("hosts") or {}).items():
                if hv.get("failed"):
                    failures.append({
                        "host": host,
                        "task": tname,
                        "msg": hv.get("msg") or hv.get("stderr") or hv.get("stdout"),
                        "rc": hv.get("rc"),
                        "stderr": hv.get("stderr"),
                    })
    return failures


def summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    host_count = len(entries)
    iface_total = 0
    bgp_present = 0
    platforms = {}
    for e in entries:
        ifaces = e.get("interfaces") or []
        if isinstance(ifaces, list):
            iface_total += len(ifaces)
        bgp = e.get("bgp") or {}
        if isinstance(bgp, dict) and bgp.get("peers_total"):
            bgp_present += 1
        plat = e.get("platform") or "unknown"
        platforms[plat] = platforms.get(plat, 0) + 1
    return {
        "host_count": host_count,
        "interface_total": iface_total,
        "bgp_hosts": bgp_present,
        "platform_breakdown": platforms,
    }


def main():
    ap = argparse.ArgumentParser(description="Test runner for network_overview playbook (no ingest)")
    default_playbook = "mcp-ansible-wrapper/playbooks/network_overview.yml"
    default_inventory = "mcp-ansible-wrapper/inventory"
    # Inside container we typically want the stripped path
    if os.getcwd().startswith(CONTAINER_ROOT_MARKER):
        default_playbook = "playbooks/network_overview.yml"
        # Prefer inventory.ini if present
        if os.path.exists("inventory.ini"):
            default_inventory = "inventory.ini"
        else:
            default_inventory = "inventory"
    ap.add_argument("--playbook", default=default_playbook)
    ap.add_argument("--inventory", default=default_inventory, help="Inventory path (dir or file)")
    ap.add_argument("--limit", help="Ansible --limit pattern (optional)")
    ap.add_argument("--json", action="store_true", help="Print full entry list as JSON as well")
    ap.add_argument("--ansible-verbose", action="store_true", help="Add -vvv to ansible-playbook for troubleshooting")
    ap.add_argument("--allow-fail", action="store_true", help="Do not exit on ansible failure; attempt partial parse")
    ap.add_argument("--offline-callback-file", help="Parse an existing Ansible JSON callback output file (skip running ansible)")
    args = ap.parse_args()

    playbook_path = _resolve_path(args.playbook)
    inventory_path = _resolve_path(args.inventory)
    if not os.path.exists(playbook_path):
        print(json.dumps({"ok": False, "error": f"playbook not found: {playbook_path}"}))
        sys.exit(2)
    if not os.path.exists(inventory_path):
        print(json.dumps({"ok": False, "error": f"inventory not found: {inventory_path}"}))
        sys.exit(2)

    if args.offline_callback_file:
        try:
            with open(args.offline_callback_file, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            rc = 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"offline parse failed: {e}", "file": args.offline_callback_file}, ensure_ascii=False))
            sys.exit(3)
    else:
        exec_result = run_playbook(playbook_path, inventory_path, args.limit, ansible_verbose=args.ansible_verbose)
        rc = exec_result.get("rc", 1)
        parsed = exec_result.get("parsed")
        if rc != 0 and not args.allow_fail:
            print(json.dumps({
                "ok": False,
                "error": "ansible-playbook failed",
                "rc": rc,
                "cmd": exec_result.get("cmd"),
                "stderr_tail": exec_result.get("raw_stderr"),
                "stdout_tail": exec_result.get("raw_stdout"),
                "parse_error": exec_result.get("parse_error"),
            }, ensure_ascii=False, separators=(",", ":")))
            sys.exit(2)
        if not parsed:
            salvage = {}
            import re, tempfile  # local import retained (regex already imported globally)
            raw_stdout = exec_result.get("raw_stdout", "")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ansible-raw.json", mode="w", encoding="utf-8")
            tmp.write(raw_stdout)
            tmp.close()
            fatal_re = re.compile(r"fatal: \[(?P<host>[^\]]+)\]: FAILED! => (?P<rest>.*)")
            failures = []
            for line in raw_stdout.splitlines():
                m = fatal_re.search(line)
                if m:
                    failures.append({"host": m.group("host"), "raw": m.group("rest")[:400]})
            salvage["failures"] = failures
            salvage["stdout_file"] = tmp.name
            print(json.dumps({
                "ok": False,
                "error": "no parsable JSON output" if not exec_result.get("parse_error") else "parse error",
                "rc": rc,
                "parse_error": exec_result.get("parse_error"),
                "parse_salvage": exec_result.get("parse_salvage"),
                "stdout_tail": exec_result.get("raw_stdout"),
                "salvage": salvage,
            }, ensure_ascii=False, separators=(",", ":")))
            sys.exit(3)

    entries = extract_entries(parsed)
    failures = detect_failures(parsed)
    summary = summarize(entries)
    out = {
        "ok": rc == 0,
        "partial": rc != 0,
        "rc": rc,
        "summary": summary,
        "entries_count": len(entries),
    }
    if failures:
        out["failures"] = failures
    if args.json:
        out["entries"] = entries
    if rc != 0:
        out["stderr_tail"] = exec_result.get("raw_stderr")
        out["stdout_tail"] = exec_result.get("raw_stdout")
        out["cmd"] = exec_result.get("cmd")
    # Include salvage note if any
    if 'exec_result' in locals() and exec_result.get('parse_salvage'):
        out['parse_salvage'] = exec_result.get('parse_salvage')
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))

if __name__ == "__main__":
    main()
