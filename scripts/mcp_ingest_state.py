#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mcp_ingest_state.py
-------------------
Fetch BGP/OSPF state via Ansible-MCP HTTP API and upsert into a SQLite CMDB.

- Reads from MCP (default http://127.0.0.1:9000) using tool "ansible.playbook"
- Extracts JSON from either result.msg or ansible.stdout (JSON per line / embedded)
- Writes to tables: routing_bgp_peer, routing_ospf_neighbor, routing_summary
- Also maintains ETL snapshots (raw_state / normalized_state), schema_meta, and summary_diff.

This file is a drop-in patched version that adds:
- --diff-host / --diff-kind / --set-unordered
- Structural diff (type-aware, normalized compare) into summary_diff
- --prune / --keep with safe deletion order and dry-run
- schema_meta_latest view helper

Usage (examples):
  python3 scripts/mcp_ingest_state.py \
    --db /path/to/rag.db \
    --token "$MCP_TOKEN" \
    --mcp-base http://127.0.0.1:9000 \
    --playbook-bgp show_bgp \
    --playbook-ospf show_ospf \
    --verbose

  # diff
  python3 scripts/mcp_ingest_state.py --db rag.db --diff \
    --base "2025-10-07T01:20:05.474512Z" --new "2025-10-07T01:53:04.682324Z" \
    --diff-host R1 --diff-kind bgp_peer --set-unordered --json-log

  # prune keep last 5
  python3 scripts/mcp_ingest_state.py --db rag.db --prune --keep 5 --json-log
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple, Iterable

# ---- Exit codes --------------------------------------------------------------
EXIT_OK = 0
EXIT_SCHEMA_MISSING = 2
EXIT_MCP_FAIL = 3
EXIT_NO_JSON = 4
EXIT_PREFLIGHT_FAIL = 5

# ---- Logger ------------------------------------------------------------------
class LogHelper:
    def __init__(self, json_mode: bool, correlation_id: str):
        self.json_mode = json_mode
        self.correlation_id = correlation_id

    def log_event(self, level: str, event: str, component: str, step: str, msg: str, **kwargs):
        payload = {
            "ts": _iso_now(),
            "level": level,
            "component": component,
            "step": step,
            "msg": msg,
            "event": event,
            "cid": self.correlation_id,
        }
        payload.update(kwargs)
        if self.json_mode:
            try:
                print(json.dumps(payload, ensure_ascii=False))
                return
            except Exception:
                pass
        kvs = " ".join(f"{k}={v}" for k, v in payload.items() if k not in ("ts","level","component","step","msg"))
        print(f"[{level}] {payload['ts']} {component} {step} {msg} {kvs}".rstrip())

# ---- Misc helpers -------------------------------------------------------------
def _iso_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")

def _sha1_of_file(path: str) -> str | None:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def _http_get(url: str, timeout: int = 5) -> Tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return True, body
    except Exception as e:
        return False, str(e)

def _http_post_json(url: str, payload: Dict[str, Any], token: str | None, timeout: int = 90) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": "invalid_json", "raw": body}

def _candidate_bases(port: int) -> List[str]:
    env_first = []
    for k in ("MCP_BASE", "AIOPS_MCP_URL", "AIOPS_MCP_BASE"):
        v = os.getenv(k)
        if v:
            env_first.append(v.rstrip("/"))
    cands = env_first + [
        f"http://127.0.0.1:{port}",
        f"http://host.docker.internal:{port}",
        f"http://ansible-mcp:{port}",
    ]
    seen = set(); out = []
    for b in cands:
        if b not in seen:
            seen.add(b); out.append(b)
    return out

def _call_playbook(playbook: str, token: str | None, port: int, verbose=False) -> Dict[str, Any] | None:
    payload = {"id": f"state-{playbook}", "name": "ansible.playbook", "arguments": {"playbook": playbook}}
    errs: List[str] = []
    for base in _candidate_bases(port):
        url = base + "/tools/call"
        try:
            js = _http_post_json(url, payload, token)
            if isinstance(js, dict) and js.get("ok"):
                if verbose:
                    raw_len = len(json.dumps(js)) if not isinstance(js.get("result"), str) else len(js.get("result"))
                    print(f"[mcp] POST {url} playbook={playbook} ok raw_len={raw_len}")
                return js.get("result") or js
            else:
                errs.append(f"{base}: bad response ({js.get('error') or 'unknown'})")
        except urllib.error.HTTPError as he:
            try: detail = he.read().decode("utf-8", "ignore")[:160].replace("\n"," ")
            except Exception: detail = str(he)
            errs.append(f"{base}: HTTP {he.code} {detail}")
        except Exception as e:
            errs.append(f"{base}: {e}")
    print(f"[mcp] WARN: call failed playbook={playbook}: {' | '.join(errs)}")
    return None

# ---- JSON extraction ----------------------------------------------------------
def _iter_embedded_json(text: str) -> Iterable[Dict[str, Any]]:
    if not text:
        return
    stack = 0; start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if stack == 0:
                start = i
            stack += 1
        elif ch == "}":
            if stack > 0:
                stack -= 1
                if stack == 0 and start is not None:
                    candidate = text[start:i+1]
                    try:
                        obj = json.loads(candidate)
                        yield obj
                    except Exception:
                        pass
                    start = None
    for line in text.splitlines():
        try:
            obj = json.loads(line)
            yield obj
        except Exception:
            continue

def _extract_result_objects(result: Dict[str, Any], verbose=False) -> List[Dict[str, Any]]:
    objs: List[Dict[str, Any]] = []
    for msglike in (result.get("msg"),):
        if msglike is None: continue
        if isinstance(msglike, dict):
            objs.append(msglike)
        elif isinstance(msglike, str):
            try:
                objs.append(json.loads(msglike))
            except Exception:
                for obj in _iter_embedded_json(msglike):
                    objs.append(obj)
    stdout = None
    ans = result.get("ansible")
    if isinstance(ans, dict):
        stdout = ans.get("stdout")
    if isinstance(stdout, str):
        for obj in _iter_embedded_json(stdout):
            objs.append(obj)
    elif isinstance(stdout, list):
        for chunk in stdout:
            if isinstance(chunk, str):
                for obj in _iter_embedded_json(chunk):
                    objs.append(obj)
    # unwrap {"msg": "{...}"}
    norm: List[Dict[str, Any]] = []
    for o in objs:
        if isinstance(o, dict) and isinstance(o.get("msg"), str):
            try:
                inner = json.loads(o["msg"])
                if isinstance(inner, dict):
                    norm.append(inner); continue
            except Exception:
                pass
        norm.append(o)
    if verbose:
        print(f"[parse] extracted {len(norm)} JSON objects")
    return norm

# ---- Host guess ---------------------------------------------------------------
def _pick_host(obj: Dict[str, Any], default: str = "unknown") -> str:
    for k in ("host","device","router","hostname","node","target","inventory_hostname"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for meta_key in ("meta","_meta","context","details"):
        meta = obj.get(meta_key)
        if isinstance(meta, dict):
            for k in ("host","hostname","device","router","node","inventory_hostname"):
                v = meta.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    ans = obj.get("ansible")
    if isinstance(ans, dict):
        v = ans.get("inventory_hostname") or ans.get("host")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default

def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

# ---- SQL ---------------------------------------------------------------------
UPSERT_BGP = """
INSERT INTO routing_bgp_peer(host,peer_ip,peer_as,state,uptime_sec,prefixes_received,collected_at,source)
VALUES(?,?,?,?,?,?,?,?)
ON CONFLICT(host,peer_ip,collected_at) DO UPDATE SET
  peer_as=excluded.peer_as,
  state=excluded.state,
  uptime_sec=excluded.uptime_sec,
  prefixes_received=excluded.prefixes_received,
  source=excluded.source
"""
UPSERT_OSPF = """
INSERT INTO routing_ospf_neighbor(host,neighbor_id,iface,state,dead_time_raw,address,collected_at)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(host,neighbor_id,collected_at) DO UPDATE SET
  iface=excluded.iface,
  state=excluded.state,
  dead_time_raw=excluded.dead_time_raw,
  address=excluded.address
"""
UPSERT_SUMMARY = """
INSERT INTO routing_summary(host,last_collected_at,peers_total,peers_established,ospf_neighbors,status,last_error)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(host) DO UPDATE SET
  last_collected_at=excluded.last_collected_at,
  peers_total=excluded.peers_total,
  peers_established=excluded.peers_established,
  ospf_neighbors=excluded.ospf_neighbors,
  status=excluded.status,
  last_error=excluded.last_error
"""

def ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS routing_bgp_peer(
      host TEXT, peer_ip TEXT, peer_as INTEGER, state TEXT, uptime_sec INTEGER,
      prefixes_received INTEGER, collected_at TEXT, source TEXT,
      PRIMARY KEY(host,peer_ip,collected_at)
    );
    CREATE TABLE IF NOT EXISTS routing_ospf_neighbor(
      host TEXT, neighbor_id TEXT, iface TEXT, state TEXT, dead_time_raw TEXT,
      address TEXT, collected_at TEXT,
      PRIMARY KEY(host,neighbor_id,collected_at)
    );
    CREATE TABLE IF NOT EXISTS routing_summary(
      host TEXT PRIMARY KEY,
      last_collected_at TEXT,
      peers_total INTEGER DEFAULT 0,
      peers_established INTEGER DEFAULT 0,
      ospf_neighbors INTEGER DEFAULT 0,
      status TEXT,
      last_error TEXT
    );
    """)

def write_sqlite(db_path: str,
                 bgp_rows: List[Tuple],
                 ospf_rows: List[Tuple],
                 summaries: Dict[str, Tuple],
                 verbose=False,
                 dry_run: bool=False,
                 logger: LogHelper | None = None):
    if dry_run:
        if verbose:
            print(f"[dry-run] would upsert bgp={len(bgp_rows)} ospf={len(ospf_rows)} hosts={len(summaries)} -> {db_path}")
        if logger:
            logger.log_event("info","db.dryrun","db","write",
                             f"Would upsert bgp={len(bgp_rows)} ospf={len(ospf_rows)} hosts={len(summaries)}",
                             bgp_rows=len(bgp_rows), ospf_rows=len(ospf_rows), hosts=len(summaries))
            logger.log_event("info","db.dryrun","db","commit","[dry-run] skipped commit")
        return
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM routing_summary WHERE host='unknown'")
    if 'unknown' in summaries:
        if verbose:
            print("[db] skip summary for host=unknown")
        summaries = {h:t for h,t in summaries.items() if h!='unknown'}
    for r in bgp_rows:
        cur.execute(UPSERT_BGP, r)
    for r in ospf_rows:
        cur.execute(UPSERT_OSPF, r)
    for host, tup in summaries.items():
        cur.execute(UPSERT_SUMMARY, tup)
    conn.commit()
    if verbose:
        print(f"[db] upsert bgp={len(bgp_rows)} ospf={len(ospf_rows)} hosts={len(summaries)} -> {db_path}")
    conn.close()

# ---- Dispatcher report --------------------------------------------------------
def _append_report(report_path: str, payload: Dict[str, Any]):
    try:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _write_report_if_requested(args, cid: str, exit_code: int, **fields):
    report_path = getattr(args, "report", None)
    if not report_path:
        return
    payload = {"ts": _iso_now(), "cid": cid, "exit": exit_code, **fields}
    _append_report(report_path, payload)

# ---- Snapshots / schema_meta --------------------------------------------------
essential_snapshot_tables = ('raw_state','normalized_state')

def _snapshot_raw_and_normalized(conn: sqlite3.Connection,
                                 version: str,
                                 objs_bgp: List[Dict[str, Any]],
                                 objs_ospf: List[Dict[str, Any]],
                                 bgp_rows: List[Tuple],
                                 ospf_rows: List[Tuple],
                                 collected_at: str,
                                 logger: LogHelper | None = None) -> None:
    if not all(_table_exists(conn, t) for t in essential_snapshot_tables):
        if logger:
            logger.log_event("warn","snapshot.tables.missing","snapshot","check","Snapshot tables not found; skip")
        return
    cur = conn.cursor()
    raw_bgp = 0
    for o in objs_bgp:
        host = _pick_host(o, "unknown")
        cur.execute(
            "INSERT INTO raw_state(version,host,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
            (version, host, 'bgp', json.dumps(o, ensure_ascii=False), collected_at)
        )
        raw_bgp += 1
    raw_ospf = 0
    for o in objs_ospf:
        host = _pick_host(o, "unknown")
        cur.execute(
            "INSERT INTO raw_state(version,host,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
            (version, host, 'ospf', json.dumps(o, ensure_ascii=False), collected_at)
        )
        raw_ospf += 1
    norm_bgp = 0
    for (host, peer_ip, remote_as, state, uptime_sec, pfx, _ts, _src) in bgp_rows:
        v = json.dumps({"peer_ip": peer_ip, "remoteAs": remote_as, "state": state, "pfxRcd": pfx}, ensure_ascii=False)
        cur.execute(
            "INSERT INTO normalized_state(version,host,kind,k,v,created_at) VALUES(?,?,?,?,?,?)",
            (version, host, 'bgp_peer', peer_ip, v, collected_at)
        )
        norm_bgp += 1
    norm_ospf = 0
    for (host, neighbor_id, iface, state, dead_time_raw, address, _ts) in ospf_rows:
        key = neighbor_id or address or "-"
        v = json.dumps({"neighbor_id": neighbor_id, "iface": iface, "state": state,
                        "dead_time_raw": dead_time_raw, "address": address}, ensure_ascii=False)
        cur.execute(
            "INSERT INTO normalized_state(version,host,kind,k,v,created_at) VALUES(?,?,?,?,?,?)",
            (version, host, 'ospf_neighbor', key, v, collected_at)
        )
        norm_ospf += 1
    conn.commit()
    if logger:
        logger.log_event("info","snapshot.ok","snapshot","write","Snapshot saved",
                         raw_bgp=raw_bgp, raw_ospf=raw_ospf,
                         norm_bgp=norm_bgp, norm_ospf=norm_ospf, version=version)

def _insert_schema_meta(conn: sqlite3.Connection, version: str, schema_sql: str | None, applied_by: str, logger: LogHelper | None = None):
    if not _table_exists(conn, 'schema_meta'):
        if logger:
            logger.log_event("warn","schema_meta.missing","snapshot","schema_meta","schema_meta table not found; skip")
        return
    sha1 = _sha1_of_file(schema_sql) if schema_sql else None
    conn.execute(
        "INSERT INTO schema_meta(version,schema_sha1,applied_at,applied_by,schema_path) VALUES(?,?,?,?,?)",
        (version, sha1 or "", _iso_now(), applied_by, schema_sql or "")
    )
    conn.commit()
    if logger:
        logger.log_event("info","schema_meta.insert.ok","snapshot","schema_meta","schema_meta row inserted", version=version, sha1=(sha1 or ""))

# --- schema_meta latest view (for rotation/inspection) ------------------------
def _ensure_schema_meta_view(conn: sqlite3.Connection):
    try:
        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS schema_meta_latest AS
            SELECT * FROM schema_meta
            ORDER BY datetime(applied_at) DESC, version DESC;
            """
        )
        conn.commit()
    except Exception:
        pass

# --- Version diff (summary_diff) ----------------------------------------------
def _compute_summary_diff(conn: sqlite3.Connection,
                          base_version: str,
                          new_version: str,
                          computed_at: str,
                          logger: LogHelper | None = None,
                          host_filter: str | None = None,
                          kind_filter: str | None = None,
                          set_unordered: bool = False) -> Dict[str, int]:
    """Compute deltas between normalized_state of two versions and store into summary_diff.
    Returns counts: {added, removed, changed, total}.
    """
    # helpers local to this compare
    def _safe_json_load(s: str):
        try:
            return json.loads(s)
        except Exception:
            return s

    def _normalize(obj):
        if isinstance(obj, dict):
            return {k: _normalize(obj[k]) for k in sorted(obj.keys())}
        if isinstance(obj, list):
            if set_unordered:
                return sorted(json.dumps(_normalize(x), ensure_ascii=False, sort_keys=True) for x in obj)
            else:
                return [_normalize(x) for x in obj]
        return obj

    def _equal(a, b) -> bool:
        return _normalize(a) == _normalize(b)

    if not _table_exists(conn, 'normalized_state') or not _table_exists(conn, 'summary_diff'):
        if logger:
            logger.log_event("warn", "diff.tables.missing", "diff", "check", "normalized_state/summary_diff missing; skip")
        return {"added": 0, "removed": 0, "changed": 0, "total": 0}

    cur = conn.cursor()

    def load_map(version: str) -> Dict[Tuple[str, str, str], str]:
        sql = "SELECT host, kind, k, v FROM normalized_state WHERE version=?"
        params: List[Any] = [version]
        if host_filter:
            sql += " AND host=?"
            params.append(host_filter)
        if kind_filter:
            sql += " AND kind=?"
            params.append(kind_filter)
        rows = cur.execute(sql, tuple(params)).fetchall()
        return {(h, kd, k): v for (h, kd, k, v) in rows}

    base = load_map(base_version)
    new = load_map(new_version)

    # Avoid duplicates for the same pair
    cur.execute("DELETE FROM summary_diff WHERE base_version=? AND new_version=?", (base_version, new_version))

    keys = set(base.keys()) | set(new.keys())
    added = removed = changed = 0

    for key in keys:
        h, kd, k = key
        b = base.get(key)
        n = new.get(key)
        if b is None and n is not None:
            cur.execute(
                "INSERT INTO summary_diff(base_version,new_version,host,kind,k,change,before,after,computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (base_version, new_version, h, kd, k, 'added', None, n, computed_at)
            )
            added += 1
        elif b is not None and n is None:
            cur.execute(
                "INSERT INTO summary_diff(base_version,new_version,host,kind,k,change,before,after,computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (base_version, new_version, h, kd, k, 'removed', b, None, computed_at)
            )
            removed += 1
        else:
            # both exist; structural compare
            b_obj = _safe_json_load(b)
            n_obj = _safe_json_load(n)
            if type(b_obj) is not type(n_obj):
                cur.execute(
                    "INSERT INTO summary_diff(base_version,new_version,host,kind,k,change,before,after,computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (base_version, new_version, h, kd, k, 'type', b, n, computed_at)
                )
                changed += 1
            elif not _equal(b_obj, n_obj):
                cur.execute(
                    "INSERT INTO summary_diff(base_version,new_version,host,kind,k,change,before,after,computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (base_version, new_version, h, kd, k, 'changed', b, n, computed_at)
                )
                changed += 1
            # identical -> no row

    conn.commit()
    total = added + removed + changed
    if logger:
        logger.log_event("info", "diff.ok", "diff", "compute",
                         "summary_diff computed", base=base_version, new=new_version,
                         added=added, removed=removed, changed=changed, total=total)
    return {"added": added, "removed": removed, "changed": changed, "total": total}

# ---- Parsers -----------------------------------------------------------------
def _get_with_aliases(src: Dict[str, Any], aliases: Dict[str, List[str]], canon: str, default: Any = None) -> Any:
    keys = aliases.get(canon, [canon])
    for k in keys:
        if k in src and src[k] not in (None, ""):
            return src[k]
    return default

def parse_bgp_objects(objs: List[Dict[str, Any]], collected_at: str, strict: bool, aliases: Dict[str, Dict[str, List[str]]]) -> Tuple[List[Tuple], Dict[str, Tuple]]:
    rows: List[Tuple] = []
    summary: Dict[str, Tuple] = {}
    for o in objs:
        host = _pick_host(o, "unknown")
        if strict and host == "unknown":
            continue
        # peer-like object
        if any(k in o for k in ("peer_ip","peerIp","neighbor")) and any(k in o for k in ("state","peerState","sessionState")):
            ap = aliases.get("bgp_peer", {})
            ip = _get_with_aliases(o, ap, "peer_ip", "-")
            state = _get_with_aliases(o, ap, "state", "-")
            remote_as = _as_int(_get_with_aliases(o, ap, "remoteAs", 0))
            pfx = _as_int(_get_with_aliases(o, ap, "pfxRcd", 0))
            total = 1
            est = 1 if state in ("Established","OK","established") else 0
            rows.append((host, ip, remote_as, state, 0, pfx, collected_at, "ansible-mcp"))
            summary[host] = (host, collected_at, total, est, 0, "ok", "")
            continue
        peers_obj = None
        bgp = o.get("bgp") if isinstance(o.get("bgp"), dict) else None
        if bgp:
            if isinstance(bgp.get("peers"), dict):
                peers_obj = bgp.get("peers")
            elif isinstance(bgp.get("neighbors"), dict):
                peers_obj = bgp.get("neighbors")
            elif isinstance(bgp.get("peers"), list):
                peers_obj = bgp.get("peers")
        if peers_obj is None:
            ipv4 = o.get("ipv4Unicast") if isinstance(o.get("ipv4Unicast"), dict) else None
            if ipv4 and isinstance(ipv4.get("peers"), dict):
                peers_obj = ipv4.get("peers")
        if strict and peers_obj is None:
            continue
        total = 0; est = 0
        if isinstance(peers_obj, dict):
            for ip, p in peers_obj.items():
                ap = aliases.get("bgp_peer", {})
                state = _get_with_aliases(p, ap, "state", "-")
                remote_as = _as_int(_get_with_aliases(p, ap, "remoteAs", 0))
                pfx = _as_int(_get_with_aliases(p, ap, "pfxRcd", 0))
                total += 1
                if state in ("Established","OK","established"):
                    est += 1
                rows.append((host, ip, remote_as, state, 0, pfx, collected_at, "ansible-mcp"))
        elif isinstance(peers_obj, list):
            for p in peers_obj:
                if not isinstance(p, dict):
                    continue
                ap = aliases.get("bgp_peer", {})
                ip = _get_with_aliases(p, ap, "peer_ip", "-")
                state = _get_with_aliases(p, ap, "state", "-")
                remote_as = _as_int(_get_with_aliases(p, ap, "remoteAs", 0))
                pfx = _as_int(_get_with_aliases(p, ap, "pfxRcd", 0))
                total += 1
                if state in ("Established","OK","established"):
                    est += 1
                rows.append((host, ip, remote_as, state, 0, pfx, collected_at, "ansible-mcp"))
        summary[host] = (host, collected_at, total, est, 0, "ok", "")
    return rows, summary

def parse_ospf_objects(objs: List[Dict[str, Any]], collected_at: str, strict: bool, aliases: Dict[str, Dict[str, List[str]]]) -> Tuple[List[Tuple], Dict[str, Tuple]]:
    rows: List[Tuple] = []
    summary: Dict[str, Tuple] = {}
    counts: Dict[str, int] = {}
    for o in objs:
        host = _pick_host(o, "unknown")
        if strict and host == "unknown":
            continue
        if any(k in o for k in ("neighbor_id","routerId","id")) and any(k in o for k in ("state","adjState")):
            ao = aliases.get("ospf_neighbor", {})
            rows.append((host,
                         _get_with_aliases(o, ao, "neighbor_id", "-"),
                         _get_with_aliases(o, ao, "iface", "-"),
                         _get_with_aliases(o, ao, "state", "-"),
                         _get_with_aliases(o, ao, "dead_time_raw", ""),
                         _get_with_aliases(o, ao, "address", ""),
                         collected_at))
            counts[host] = counts.get(host, 0) + 1
            continue
        neighbors = None
        ospf = o.get("ospf") if isinstance(o.get("ospf"), dict) else None
        if ospf and isinstance(ospf.get("neighbors"), list):
            neighbors = ospf["neighbors"]
        if neighbors is None and isinstance(o.get("neighbors"), list):
            neighbors = o["neighbors"]
        if neighbors is None and isinstance(o.get("adjacencies"), list):
            neighbors = o["adjacencies"]
        if strict and neighbors is None:
            continue
        neighbors = neighbors or []
        for n in neighbors:
            if not isinstance(n, dict):
                continue
            ao = aliases.get("ospf_neighbor", {})
            rows.append((host,
                         _get_with_aliases(n, ao, "neighbor_id", "-"),
                         _get_with_aliases(n, ao, "iface", "-"),
                         _get_with_aliases(n, ao, "state", "-"),
                         _get_with_aliases(n, ao, "dead_time_raw", ""),
                         _get_with_aliases(n, ao, "address", ""),
                         collected_at))
        counts[host] = counts.get(host, 0) + len(neighbors)
    for host, nei in counts.items():
        summary[host] = (host, collected_at, 0, 0, nei, "ok", "")
    return rows, summary

def _load_aliases(path: str) -> Dict[str, Dict[str, List[str]]]:
    m: Dict[str, Dict[str, List[str]]] = {
        "bgp_peer": {
            "peer_ip": ["peer_ip","peerIp","neighbor","id"],
            "state": ["state","peerState","sessionState"],
            "remoteAs": ["remoteAs","asn","remote_as"],
            "pfxRcd": ["pfxRcd","prefixes_received","prefixReceived"],
        },
        "ospf_neighbor": {
            "neighbor_id": ["neighbor_id","id","routerId"],
            "iface": ["iface","interface","ifname"],
            "state": ["state","adjState"],
            "dead_time_raw": ["dead_time_raw","deadTime"],
            "address": ["address","neighborAddress"],
        },
    }
    try:
        if not path or not os.path.exists(path):
            return m
        try:
            import yaml  # type: ignore
            with open(path, "r", encoding="utf-8") as f:
                y = yaml.safe_load(f)
            if isinstance(y, dict):
                for sect in ("bgp_peer","ospf_neighbor"):
                    if isinstance(y.get(sect), dict):
                        for k,v in y[sect].items():
                            if isinstance(v, list):
                                m.setdefault(sect, {})[k] = v
            return m
        except Exception:
            pass
        with open(path, "r", encoding="utf-8") as f:
            y = json.load(f)
        if isinstance(y, dict):
            for sect in ("bgp_peer","ospf_neighbor"):
                if isinstance(y.get(sect), dict):
                    for k,v in y[sect].items():
                        if isinstance(v, list):
                            m.setdefault(sect, {})[k] = v
    except Exception:
        pass
    return m

# ---- Preflight ---------------------------------------------------------------
def _check_mcp_health(args, logger: LogHelper) -> bool:
    bases: List[str] = []
    if isinstance(args.mcp_base, str) and args.mcp_base:
        bases.append(args.mcp_base.rstrip("/"))
    for b in _candidate_bases(args.port):
        if b not in bases:
            bases.append(b)
    ok_any = False; errors: List[str] = []
    for b in bases:
        ok, note = _http_get(b + "/health", timeout=5)
        if ok:
            logger.log_event("info", "mcp.health", "preflight", "mcp_health", "MCP health OK", base=b)
            ok_any = True; break
        else:
            errors.append(f"{b}: {note}")
    if not ok_any:
        logger.log_event("error", "mcp.health.fail", "preflight", "mcp_health", "MCP health check failed", details=" | ".join(errors))
    return ok_any

def _preflight(args, logger: LogHelper) -> int:
    try:
        dbp = Path(args.db)
        dbdir = (dbp.parent if dbp.parent else Path("."))
        if not dbdir.exists():
            logger.log_event("error","db.dir.missing","preflight","db_path","DB directory does not exist", path=str(dbdir))
            return EXIT_PREFLIGHT_FAIL
        if not os.access(dbdir, os.W_OK):
            logger.log_event("error","db.dir.notwritable","preflight","db_path","DB directory not writable", path=str(dbdir))
            return EXIT_PREFLIGHT_FAIL
    except Exception as e:
        logger.log_event("error","db.dir.checkfail","preflight","db_path","DB path check failed", error=str(e))
        return EXIT_PREFLIGHT_FAIL

    if getattr(args, "schema_sql", None):
        ss = args.schema_sql
        if not os.path.exists(ss):
            logger.log_event("error","schema_sql.missing","preflight","schema","schema_sql file not found", path=ss)
            return EXIT_PREFLIGHT_FAIL
        if not os.access(ss, os.R_OK):
            logger.log_event("error","schema_sql.notreadable","preflight","schema","schema_sql not readable", path=ss)
            return EXIT_PREFLIGHT_FAIL
        logger.log_event("info","schema_sql.found","preflight","schema","schema_sql found", path=ss)

    if not _check_mcp_health(args, logger):
        return EXIT_PREFLIGHT_FAIL

    if args.alias_file and args.alias_file not in ("key_aliases.yml",):
        if not os.path.exists(args.alias_file):
            logger.log_event("warn","alias_file.missing","preflight","alias","alias file not found; using built-ins", alias_file=args.alias_file)
    if not args.token:
        logger.log_event("warn","token.empty","preflight","token","MCP token is empty; proceeding without Authorization header")
    return EXIT_OK

# ---- Main --------------------------------------------------------------------
def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="SQLite DB path")
    ap.add_argument("--mcp-base", default=os.getenv("MCP_BASE","http://127.0.0.1:9000"))
    ap.add_argument("--token", default=os.getenv("MCP_TOKEN"))
    ap.add_argument("--playbook-bgp", default="show_bgp")
    ap.add_argument("--playbook-ospf", default="show_ospf")
    ap.add_argument("--port", type=int, default=int(os.getenv("AIOPS_MCP_PORT","9000")))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--strict", action="store_true", help="Enable strict schema checks; skip objects that don't match")
    ap.add_argument("--alias-file", default=os.getenv("MCP_ALIAS_FILE","key_aliases.yml"), help="Optional alias file (YAML/JSON) for key normalization")
    ap.add_argument("--dry-run", action="store_true", help="Do everything except DB writes")
    ap.add_argument("--json-log", action="store_true", help="Emit logs as JSON Lines")
    ap.add_argument("--ensure-schema", action="store_true", help="(Optional) Ensure schema before insert (no-op here; use external etl.sh)")
    ap.add_argument("--schema-sql", default=os.getenv("SCHEMA_SQL"), help="Path to schema SQL (.read) for ensure-schema")
    ap.add_argument("--report", help="Path to JSONL report file for dispatcher integration")
    ap.add_argument("--snapshot", action="store_true", help="Save raw/normalized snapshots (requires tables)")
    ap.add_argument("--schema-meta", action="store_true", help="Record schema_meta with version & schema SHA1")
    ap.add_argument("--diff", action="store_true", help="Compute diffs between two versions and write to summary_diff")
    ap.add_argument("--base", help="Base version (ISO timestamp)")
    ap.add_argument("--new", dest="new_version", help="New version (ISO timestamp)")
    ap.add_argument("--verify", action="store_true", help="Verify ETL consistency for a specific version (or latest)")
    ap.add_argument("--version", help="Version (ISO timestamp) to verify; if omitted, use the latest in raw_state/normalized_state")
    ap.add_argument("--prune", action="store_true", help="Prune old ETL snapshots (raw_state/normalized_state/summary_diff/schema_meta)")
    ap.add_argument("--keep", type=int, default=5, help="Number of most-recent versions to keep when --prune is used (default: 5)")
    ap.add_argument("--diff-host", help="Only compute diff for this host (optional)")
    ap.add_argument("--diff-kind", help="Only compute diff for this kind (e.g., bgp_peer, ospf_neighbor)")
    ap.add_argument("--set-unordered", action="store_true", help="When comparing JSON arrays, treat them as unordered sets")
    args = ap.parse_args(argv)

    _t0 = time.time()
    correlation_id = uuid.uuid4().hex
    logger = LogHelper(args.json_log, correlation_id)

    ALIASES = _load_aliases(args.alias_file)

    collected_at = _iso_now()
    version_id = collected_at

    rc = _preflight(args, logger)
    if rc != EXIT_OK:
        _write_report_if_requested(args, correlation_id, rc, event="preflight.fail")
        return rc
    logger.log_event("info","preflight.ok","preflight","done","Preflight checks passed", elapsed_ms=int((time.time()-_t0)*1000))

    # --- Diff-only mode -------------------------------------------------------
    if args.diff:
        if not args.base or not args.new_version:
            logger.log_event("error","diff.args.missing","diff","args","--diff requires --base and --new")
            _write_report_if_requested(args, correlation_id, EXIT_PREFLIGHT_FAIL, event="diff.args.missing")
            return EXIT_PREFLIGHT_FAIL
        try:
            conn = sqlite3.connect(args.db)
            counts = _compute_summary_diff(
                conn, args.base, args.new_version, _iso_now(), logger,
                host_filter=getattr(args, 'diff_host', None),
                kind_filter=getattr(args, 'diff_kind', None),
                set_unordered=bool(getattr(args, 'set_unordered', False)),
            )
            conn.close()
            _write_report_if_requested(args, correlation_id, EXIT_OK, event="diff.done", **counts,
                                       base=args.base, new=args.new_version)
            return EXIT_OK
        except Exception as e:
            logger.log_event("error","diff.error","diff","compute", f"{e}")
            _write_report_if_requested(args, correlation_id, EXIT_MCP_FAIL, event="diff.error", error=str(e))
            return EXIT_MCP_FAIL

    # --- Verify-only mode -----------------------------------------------------
    if args.verify:
        try:
            conn = sqlite3.connect(args.db)

            def latest_version(c: sqlite3.Connection) -> str | None:
                v1 = c.execute("SELECT version FROM raw_state ORDER BY created_at DESC LIMIT 1").fetchone()
                v2 = c.execute("SELECT version FROM normalized_state ORDER BY created_at DESC LIMIT 1").fetchone()
                cand = [x[0] for x in (v1, v2) if x]
                return max(cand) if cand else None

            target_ver = args.version or latest_version(conn)
            if not target_ver:
                logger.log_event("error","verify.no_version","verify","args","No version to verify")
                _write_report_if_requested(args, correlation_id, EXIT_PREFLIGHT_FAIL, event="verify.no_version")
                conn.close()
                return EXIT_PREFLIGHT_FAIL

            def exists(table: str) -> bool:
                return _table_exists(conn, table)

            required = ["raw_state","normalized_state"]
            missing = [t for t in required if not exists(t)]
            if missing:
                logger.log_event("error","verify.tables.missing","verify","pre", f"Missing tables: {missing}")
                _write_report_if_requested(args, correlation_id, EXIT_PREFLIGHT_FAIL, event="verify.tables.missing", missing=",".join(missing))
                conn.close()
                return EXIT_PREFLIGHT_FAIL

            cur = conn.cursor()
            raw_bgp = cur.execute("SELECT COUNT(*) FROM raw_state WHERE version=? AND kind='bgp'", (target_ver,)).fetchone()[0]
            raw_ospf = cur.execute("SELECT COUNT(*) FROM raw_state WHERE version=? AND kind='ospf'", (target_ver,)).fetchone()[0]
            norm_bgp = cur.execute("SELECT COUNT(*) FROM normalized_state WHERE version=? AND kind='bgp_peer'", (target_ver,)).fetchone()[0]
            norm_ospf = cur.execute("SELECT COUNT(*) FROM normalized_state WHERE version=? AND kind='ospf_neighbor'", (target_ver,)).fetchone()[0]
            bad_keys = cur.execute("SELECT COUNT(*) FROM normalized_state WHERE version=? AND (k IS NULL OR k='')", (target_ver,)).fetchone()[0]

            unknown_hosts = None
            if exists("routing_summary"):
                unknown_hosts = cur.execute("SELECT COUNT(*) FROM routing_summary WHERE host='unknown'").fetchone()[0]

            passed = (raw_bgp + raw_ospf) >= 1 and (norm_bgp + norm_ospf) >= 1 and bad_keys == 0
            details = {"version": target_ver, "raw_bgp": raw_bgp, "raw_ospf": raw_ospf,
                       "norm_bgp": norm_bgp, "norm_ospf": norm_ospf, "bad_keys": bad_keys}
            if unknown_hosts is not None:
                details["unknown_hosts"] = unknown_hosts
                passed = passed and unknown_hosts == 0

            conn.close()
            if passed:
                logger.log_event("info","verify.ok","verify","check","ETL consistency OK", **details)
                _write_report_if_requested(args, correlation_id, EXIT_OK, event="verify.ok", **details)
                return EXIT_OK
            else:
                logger.log_event("error","verify.fail","verify","check","ETL consistency failed", **details)
                _write_report_if_requested(args, correlation_id, EXIT_PREFLIGHT_FAIL, event="verify.fail", **details)
                return EXIT_PREFLIGHT_FAIL
        except Exception as e:
            logger.log_event("error","verify.error","verify","check", f"{e}")
            _write_report_if_requested(args, correlation_id, EXIT_MCP_FAIL, event="verify.error", error=str(e))
            return EXIT_MCP_FAIL

    # --- Prune-only mode ------------------------------------------------------
    if args.prune:
        try:
            conn = sqlite3.connect(args.db)
            cur = conn.cursor()

            def have(table: str) -> bool:
                return _table_exists(conn, table)

            versions = set()
            if have('normalized_state'):
                versions.update(v for (v,) in cur.execute("SELECT DISTINCT version FROM normalized_state").fetchall())
            if have('raw_state'):
                versions.update(v for (v,) in cur.execute("SELECT DISTINCT version FROM raw_state").fetchall())
            versions = sorted(versions)
            if not versions:
                logger.log_event("info","prune.empty","prune","scan","No versions present; nothing to prune")
                _write_report_if_requested(args, correlation_id, EXIT_OK, event="prune.empty")
                conn.close()
                return EXIT_OK

            keep_n = max(0, int(args.keep))
            keep_set = set(versions[-keep_n:]) if keep_n > 0 else set()
            del_list = [v for v in versions if v not in keep_set]

            if not del_list:
                logger.log_event("info","prune.nodel","prune","plan","Nothing to prune; already within keep window", keep=keep_n)
                _write_report_if_requested(args, correlation_id, EXIT_OK, event="prune.nodel", keep=keep_n)
                conn.close()
                return EXIT_OK

            if getattr(args, 'dry_run', False):
                logger.log_event("info","prune.plan","prune","dry_run","Dry-run: would delete versions",
                                 delete_count=len(del_list), keep=keep_n, versions=",".join(del_list))
                _write_report_if_requested(args, correlation_id, EXIT_OK, event="prune.plan", delete_count=len(del_list), keep=keep_n)
                conn.close()
                return EXIT_OK

            deleted = {"summary_diff": 0, "normalized_state": 0, "raw_state": 0, "schema_meta": 0}

            if have('summary_diff') and del_list:
                q = "DELETE FROM summary_diff WHERE base_version IN ({}) OR new_version IN ({})".format(
                    ",".join(["?"]*len(del_list)), ",".join(["?"]*len(del_list))
                )
                cur.execute(q, tuple(del_list+del_list))
                deleted["summary_diff"] = cur.rowcount if cur.rowcount is not None else 0

            if have('normalized_state') and del_list:
                q = "DELETE FROM normalized_state WHERE version IN ({})".format(",".join(["?"]*len(del_list)))
                cur.execute(q, tuple(del_list))
                deleted["normalized_state"] = cur.rowcount if cur.rowcount is not None else 0

            if have('raw_state') and del_list:
                q = "DELETE FROM raw_state WHERE version IN ({})".format(",".join(["?"]*len(del_list)))
                cur.execute(q, tuple(del_list))
                deleted["raw_state"] = cur.rowcount if cur.rowcount is not None else 0

            if have('schema_meta') and del_list:
                q = "DELETE FROM schema_meta WHERE version IN ({})".format(",".join(["?"]*len(del_list)))
                cur.execute(q, tuple(del_list))
                deleted["schema_meta"] = cur.rowcount if cur.rowcount is not None else 0

            conn.commit()
            _ensure_schema_meta_view(conn)
            logger.log_event("info","prune.ok","prune","delete","Prune completed", keep=keep_n, **deleted)
            _write_report_if_requested(args, correlation_id, EXIT_OK, event="prune.ok", keep=keep_n, **deleted)
            conn.close()
            return EXIT_OK
        except Exception as e:
            logger.log_event("error","prune.error","prune","delete", f"{e}")
            _write_report_if_requested(args, correlation_id, EXIT_MCP_FAIL, event="prune.error", error=str(e))
            return EXIT_MCP_FAIL

    # --- Ingest path ----------------------------------------------------------
    try:
        # Optionally ensure schema via external .read (documented; here we only log path)
        if args.ensure_schema and args.schema_sql and os.path.exists(args.schema_sql):
            # Do not execute .read here to avoid hidden migrations; leave to external wrapper.
            logger.log_event("info","ensure_schema.nop","schema","noop","ensure-schema is delegated to external script", schema=args.schema_sql)

        # Pull from MCP
        res_bgp = _call_playbook(args.playbook_bgp, args.token, args.port, verbose=args.verbose)
        res_ospf = _call_playbook(args.playbook_ospf, args.token, args.port, verbose=args.verbose)
        if res_bgp is None and res_ospf is None:
            logger.log_event("error","mcp.empty","mcp","call","Both playbooks failed or empty")
            _write_report_if_requested(args, correlation_id, EXIT_MCP_FAIL, event="mcp.empty")
            return EXIT_MCP_FAIL

        objs_bgp = _extract_result_objects(res_bgp or {}, verbose=args.verbose)
        objs_ospf = _extract_result_objects(res_ospf or {}, verbose=args.verbose)

        collected_at = _iso_now()
        version_id = collected_at

        aliases = _load_aliases(args.alias_file)
        bgp_rows, bgp_summary = parse_bgp_objects(objs_bgp, collected_at, args.strict, aliases)
        ospf_rows, ospf_summary = parse_ospf_objects(objs_ospf, collected_at, args.strict, aliases)

        # Merge summaries
        summaries: Dict[str, Tuple] = {}
        for h, t in bgp_summary.items():
            summaries[h] = t
        for h, t in ospf_summary.items():
            if h in summaries:
                # merge peers/established/ospf counts
                old = summaries[h]
                summaries[h] = (h, collected_at,
                                old[2] + t[2],     # peers_total
                                old[3] + t[3],     # peers_established
                                max(old[4], t[4]), # ospf_neighbors
                                "ok", "")
            else:
                summaries[h] = t

        write_sqlite(args.db, bgp_rows, ospf_rows, summaries, verbose=args.verbose, dry_run=args.dry_run, logger=logger)

        if not args.dry_run and args.snapshot:
            conn = sqlite3.connect(args.db)
            _snapshot_raw_and_normalized(conn, version_id, objs_bgp, objs_ospf, bgp_rows, ospf_rows, collected_at, logger)
            if args.schema_meta and args.schema_sql:
                _insert_schema_meta(conn, version_id, args.schema_sql, applied_by="mcp_ingest_state", logger=logger)
            conn.close()

        # DIFFサマリ集計
        diff_summary = None
        try:
            conn = sqlite3.connect(args.db)
            cur = conn.cursor()
            res = cur.execute("SELECT change, COUNT(*) FROM summary_diff WHERE new_version=? GROUP BY change", (version_id,)).fetchall()
            diff_summary = {row[0]: row[1] for row in res}
            conn.close()
        except Exception as e:
            diff_summary = {"error": str(e)}

        # 標準出力でDIFFサマリをJSON返却
        result = {
            "status": "ok",
            "summary": f"Ingest completed: hosts={len(summaries)}",
            "bgp_rows": len(bgp_rows),
            "ospf_rows": len(ospf_rows),
            "hosts": len(summaries),
            "diff_summary": diff_summary
        }
        print(json.dumps(result, ensure_ascii=False))
        logger.log_event("info","done","main","exit","Ingest completed",
                         bgp_rows=len(bgp_rows), ospf_rows=len(ospf_rows), hosts=len(summaries))
        _write_report_if_requested(args, correlation_id, EXIT_OK, event="ingest.ok",
                                   bgp_rows=len(bgp_rows), ospf_rows=len(ospf_rows), hosts=len(summaries))
        return EXIT_OK

    except Exception as e:
        logger.log_event("error","ingest.error","main","run", f"{e}")
        _write_report_if_requested(args, correlation_id, EXIT_MCP_FAIL, event="ingest.error", error=str(e))
        return EXIT_MCP_FAIL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
