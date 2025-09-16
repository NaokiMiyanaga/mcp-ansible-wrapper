# Helper to coerce request id from body or headers
import os, json, subprocess, tempfile, re, time, uuid
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urlunparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml

# -------- Config --------
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "1") == "1"
MCP_TOKEN = os.getenv("MCP_TOKEN", "")
ANSIBLE_BIN = os.getenv("ANSIBLE_BIN", "/usr/local/bin/ansible-playbook")
EFFECTIVE_MODE = "exec" if Path(ANSIBLE_BIN).exists() else "dry"
MODE_REASON = "exec: ansible present" if EFFECTIVE_MODE == "exec" else "dry: ansible not found"
BASE_DIR = Path(__file__).resolve().parent

_START_TS = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")
_MCP_LOG_DIR = Path(os.getenv("MCP_LOG_DIR", "/app/logs")).resolve()
_MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
_MCP_LOG_FILE = _MCP_LOG_DIR / f"mcp_events_{_START_TS}.jsonl"

def _now_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()

def _mcp_log(id: str, no: int, tag: str, content: Any):
    rec = {"id": id, "ts_jst": _now_jst(), "no": no, "actor": "mcp", "tag": tag, "content": content}
    try:
        with _MCP_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

app = FastAPI(title="MCP")

def _coerce_req_id_from(request: Request, body: dict | None) -> str:
    rid = None
    if isinstance(body, dict):
        rid = body.get("id") or body.get("request_id")
    if not rid:
        rid = request.headers.get("X-Request-Id")
    return rid or str(uuid.uuid4())

# ---- Tools/tags registry ----
SERVER_VERSION = os.getenv("MCP_SERVER_VERSION", "v1")

# ---- Dynamic enum (routers) discovery with TTL cache ----
_HOSTS_CACHE: list[str] = []
_HOSTS_CACHE_TS: float = 0.0

def _extract_machine_json(stdout: str) -> Optional[Dict[str, Any]]:
    try:
        m = re.search(r"Machine summary\s*\(JSON\)\s*:\s*([\s\S]+)$", stdout, re.IGNORECASE)
        if not m:
            return None
        jm = re.search(r"(\{[\s\S]*\})", m.group(1))
        if not jm:
            return None
        return json.loads(jm.group(1))
    except Exception:
        return None

def _discover_hosts() -> list[str]:
    global _HOSTS_CACHE, _HOSTS_CACHE_TS
    ttl = int(os.getenv("MCP_TOOLS_ENUM_TTL", "60"))
    now = time.time()
    if _HOSTS_CACHE and (now - _HOSTS_CACHE_TS) < ttl:
        return list(_HOSTS_CACHE)
    hosts: list[str] = []
    mode = None
    try:
        pb = (BASE_DIR / "playbooks" / "routers_list.yml").resolve()
        if pb.exists():
            rep = _run_ansible(str(pb), {})
            mode = rep.get("mode")
            stdout = rep.get("stdout") or ""
            if isinstance(stdout, str) and stdout:
                obj = _extract_machine_json(stdout)
                arr = (obj or {}).get("routers") if isinstance(obj, dict) else None
                if isinstance(arr, list):
                    hosts = [str(x) for x in arr if str(x)]
    except Exception:
        hosts = []
    # Optional fallback using env (lab/demo convenience)
    fb = os.getenv("MCP_TOOLS_ENUM_FALLBACK", "")
    if not hosts and fb:
        hosts = [h.strip() for h in fb.split(",") if h.strip()]
    try:
        _mcp_log(-1, "tools enum discovery", {"mode": mode, "hosts": hosts})
    except Exception:
        pass
    _HOSTS_CACHE = list(hosts)
    _HOSTS_CACHE_TS = now
    return hosts

def _tools_catalog() -> list[dict]:
    # Determine enum embedding mode
    enum_mode = os.getenv("MCP_TOOLS_ENUM_MODE", "auto").lower()  # embed | hint | auto
    hosts: list[str] = []
    if enum_mode in ("embed", "auto"):
        try:
            hosts = _discover_hosts()
        except Exception:
            hosts = []
    host_prop: Dict[str, Any] = {
        "type": "string",
        "description": "Target router (e.g., r1)",
        "x-enum-source": "routers_list",  # dynamic enum hint
    }
    if hosts:
        # Embed enum while keeping hint and a timestamp
        host_prop["enum"] = hosts
        host_prop["x-enum-ts"] = _now_jst()
    return [
        {
            "id": "playbooks/network_overview.yml",
            "title": "Network overview",
            "description": "Summarize lab topology (counts of routers/L2/hosts)",
            "tags": ["inventory", "count"],
            "inputs_schema": {"type": "object", "properties": {}, "required": []},
            "examples": ["評価環境の構成は？", "試験環境の構成を教えて"],
            "version": "v1"
        },
        {
            "id": "playbooks/routers_list.yml",
            "title": "Routers list",
            "description": "List L3 devices (routers) in the environment",
            "tags": ["inventory", "routers"],
            "inputs_schema": {"type": "object", "properties": {}, "required": []},
            "examples": ["L3デバイス一覧を教えて"],
            "version": "v1"
        },
        {
            "id": "playbooks/show_bgp.yml",
            "title": "Show BGP",
            "description": "Show BGP summary for a given host",
            "tags": ["routing", "bgp", "status"],
            "inputs_schema": {"type": "object", "properties": {"host": host_prop}, "required": ["host"]},
            "examples": ["r1のBGPの状態は？"],
            "version": "v1"
        },
        {
            "id": "playbooks/show_ospf.yml",
            "title": "Show OSPF",
            "description": "Show OSPF neighbors for a given host",
            "tags": ["routing", "ospf", "status"],
            "inputs_schema": {"type": "object", "properties": {"host": host_prop}, "required": ["host"]},
            "examples": ["r2のOSPFの状態は？"],
            "version": "v1"
        },
    ]

# --- RAG: knowledge-driven host/playbook selection ---
_KB_PLAYBOOK_MAP = (BASE_DIR / 'knowledge' / 'playbook_map.yaml').resolve()

def _load_playbook_map():
    try:
        with open(_KB_PLAYBOOK_MAP, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def _extract_host(text: str, kb: dict) -> str:
    t = (text or '').lower()
    host = 'r1'
    aliases = ((kb.get('aliases') or {}).get('host')) or {}
    for h in aliases.keys():
        if isinstance(h, str) and (h in t or h.lower() in t):
            return h
    for h, vals in aliases.items():
        if any(isinstance(v, str) and (v in text or v.lower() in t) for v in (vals or [])):
            return h
    for cand in ['r1','r2','l2a','l2b']:
        if cand in t:
            return cand
    return host

def _pick_playbook_by_kb(feature: str, text: str, kb: dict) -> str:
    defaults = (kb.get('defaults') or {}).get(feature) or {}
    prefer = defaults.get('prefer') or []
    t = (text or '').lower()
    for rule in prefer:
        when = rule.get('when') or {}
        any_kw = when.get('any_keywords') or []
        if any(isinstance(k,str) and (k in text or k.lower() in t) for k in any_kw):
            f = rule.get('file')
            if isinstance(f,str) and f:
                return f
    fb = defaults.get('fallback') or []
    if fb:
        f = fb[0].get('file')
        if isinstance(f,str) and f:
            return f
    return ''
def _plan_from_text(text: str) -> Dict[str, Any]:
    kb = _load_playbook_map()
    t = (text or '').lower()
    # Determine feature from intent (inventory -> ospf -> bgp -> others)
    if any(k in text or k in t for k in ['inventory','構成','台数','デバイス','機器','ノード','一覧','何台','評価環境','試験環境','lab-net']):
        feature = 'inventory'
    elif ('ospf' in t or 'OSPF' in text or 'エリア' in text or 'LSA' in text):
        feature = 'ospf'
    elif ('bgp' in t or 'ルーティング' in text or '経路' in text):
        feature = 'bgp'
    elif ('vlan' in t or 'VLAN' in text):
        feature = 'vlan'
    elif ('interface' in t or 'インタフェース' in text or 'IF' in text):
        feature = 'interface'
    elif ('isis' in t or 'ISIS' in text):
        feature = 'isis'
    elif ('snmp' in t or 'SNMP' in text):
        feature = 'snmp'
    elif ('log' in t or 'ログ' in text):
        feature = 'logs'
    else:
        feature = 'bgp'
    host = _extract_host(text, kb)
    pb = _pick_playbook_by_kb(feature, text, kb) or 'playbooks/show_bgp.yml'
    return {"host": host, "feature": feature, "playbook": pb}
def _run_ansible(playbook: str, extra_vars: Dict[str, Any]) -> Dict[str, Any]:
    if EFFECTIVE_MODE != "exec":
        return {
            "ok": True, "mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON,
            "ansible_bin": ANSIBLE_BIN, "playbook": playbook, "vars": extra_vars,
            "stdout": "", "stderr": "", "rc": 0
        }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(extra_vars, f, ensure_ascii=False)
        ev = f.name
    inv = str(Path(os.getenv("ANSIBLE_INVENTORY", "/app/inventory.ini")).resolve())
    cmd = [ANSIBLE_BIN, playbook, "-i", inv, "-e", f"@{ev}"]
    _mcp_log(8, "mcp ansible request", {"cmd": cmd})
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    _mcp_log(9, "mcp ansible reply", {"rc": p.returncode, "stdout": out[:4000], "stderr": err[:2000]})
    return {
        "ok": p.returncode == 0, "mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON,
        "ansible_bin": ANSIBLE_BIN, "playbook": playbook, "vars": extra_vars,
        "stdout": out, "stderr": err, "rc": p.returncode
    }

# --- MCP endpoint: client-style payload compat ---