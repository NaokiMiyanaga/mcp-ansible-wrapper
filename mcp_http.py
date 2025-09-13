import os, json, subprocess, tempfile
from pathlib import Path
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml

# -------- JSONL logger (MCP) --------
_START_TS = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")
_MCP_LOG_DIR = Path(os.getenv("MCP_LOG_DIR", "/app/logs")).resolve()
_MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
_MCP_LOG_FILE = _MCP_LOG_DIR / f"mcp_events_{_START_TS}.jsonl"

def _now_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()

def _mcp_log(no: int, tag: str, content: Any):
    rec = {"ts_jst": _now_jst(), "no": no, "actor": "mcp", "tag": tag, "content": content}
    try:
        with _MCP_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

# -------- Config --------
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "1") == "1"
MCP_TOKEN = os.getenv("MCP_TOKEN", "")
ANSIBLE_BIN = os.getenv("ANSIBLE_BIN", "/usr/local/bin/ansible-playbook")
EFFECTIVE_MODE = "exec" if Path(ANSIBLE_BIN).exists() else "dry"
MODE_REASON = "exec: ansible present" if EFFECTIVE_MODE == "exec" else "dry: ansible not found"
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="MCP")

def _unauth(msg='missing bearer token'):
    return JSONResponse({"ok": False, "error": msg}, status_code=401)

async def _auth(request: Request):
    if not REQUIRE_AUTH:
        return True, None
    got = request.headers.get("authorization", "")
    if not got.startswith("Bearer "):
        return False, _unauth("missing bearer token")
    token = got.split(" ", 1)[1].strip()
    if not MCP_TOKEN or token != MCP_TOKEN:
        return False, _unauth("invalid token")
    return True, None

@app.get("/health")
def health():
    info = {
        "ok": True, "ts_jst": _now_jst(),
        "effective_mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON,
        "ansible_bin": ANSIBLE_BIN, "require_auth": REQUIRE_AUTH, "token_set": bool(MCP_TOKEN),
        "base_dir": str(BASE_DIR),
    }
    if os.getenv("MCP_LOG_HEALTH", "0") == "1":
        _mcp_log(-1, "health", info)
    return info

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

@app.post("/run")
async def run(request: Request):
    ok, resp = await _auth(request)
    if not ok:
        return resp

    body = await request.json()
    _mcp_log(6, "mcp request", {"body": body})

    text = body.get("text", "")
    decision = body.get("decision", "run")
    score = body.get("score", 1)
    payload = (body.get("payload") or {})
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else []
    intent = body.get("intent") or "run"

    plan = _plan_from_text(text)
    _mcp_log(7, "mcp gpt input", {"prompt": text, "decision": decision, "score": score, "plan": plan})
    explicit_pb: Optional[str] = payload.get("playbook") if isinstance(payload, dict) else None
    chosen_pb = explicit_pb or (candidates[0] if candidates else None) or plan.get("playbook")

    pb_path = Path(chosen_pb)
    if not pb_path.is_absolute():
        pb_path = (BASE_DIR / pb_path).resolve()
    if not pb_path.exists():
        err = {
            "ok": False, "error": f"playbook not found: {pb_path}",
            "hint": "COPY playbooks/ into the image", "ts_jst": _now_jst()
        }
        _mcp_log(11, "mcp reply", {"status": 400, **err})
        return JSONResponse(err, status_code=400)

    extra_vars = {"host": plan["host"], "feature": plan["feature"], "score": score, "decision": decision}
    user_vars = payload.get("vars") if isinstance(payload, dict) else None
    if isinstance(user_vars, dict):
        extra_vars.update(user_vars)

    # If propose_create intent is sent, do not run Ansible; return proposal
    if intent == "propose_create":
        dbg = {
            "propose_new_playbook": {
                "feature": plan["feature"],
                "suggested_path": str(pb_path),
                "vars_suggest": extra_vars,
                "template_hint": "/app/knowledge/templates/playbook.new.yml.j2",
            }
        }
        summary = f"Playbook 提案: {pb_path.name}（feature={plan['feature']}）"
        resp = {"ok": True, "decision": decision, "summary": summary, "score": score,
                "ansible": {"rc": 0, "ok": True}, "ts_jst": _now_jst(), "debug": dbg}
        _mcp_log(11, "mcp reply", {"status": 200, "summary": summary, "intent": "propose_create"})
        return resp

    reply = _run_ansible(str(pb_path), extra_vars)
    debug = {
        "no7_plan": {"prompt": text, "decision": decision, "score": score, "plan": plan},
        "no8_request": {"module": "ansible", "payload": {"playbook": str(pb_path), "vars": extra_vars},
                        "effective_mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON, "ansible_bin": ANSIBLE_BIN},
        "no9_reply": reply,
    }
    summary = f"ホスト「{extra_vars.get('host')}」の {extra_vars.get('feature')} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
    debug["no10_output"] = {"summary": summary}
    resp = {"ok": True, "decision": decision, "summary": summary, "score": score,
            "ansible": {"rc": reply["rc"], "ok": reply["ok"]}, "ts_jst": _now_jst(), "debug": debug}
    _mcp_log(10, "mcp gpt output", {"summary": summary})
    _mcp_log(11, "mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
    return resp
