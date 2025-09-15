import os, json, subprocess, tempfile, re, time, uuid
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urlunparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
from thinking import _plan_from_text

# -------- JSONL logger (MCP) --------
_START_TS = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")
_MCP_LOG_DIR = Path(os.getenv("MCP_LOG_DIR", "/app/logs")).resolve()
_MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
_MCP_LOG_FILE = _MCP_LOG_DIR / f"mcp_events_{_START_TS}.jsonl"
_REQ_ID: Optional[str] = None

def _now_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()

def _mcp_log(no: int, tag: str, content: Any):
    rec = {"ts_jst": _now_jst(), "no": no, "actor": "mcp", "tag": tag, "content": content}
    if _REQ_ID:
        rec["request_id"] = _REQ_ID
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

def _err_payload(code: str, message: str, details: Optional[Dict[str, Any]] = None, status: int = 200) -> JSONResponse:
    body: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if _REQ_ID:
        body["request_id"] = _REQ_ID
    return JSONResponse(body, status_code=status)

def _unauth(msg: str = 'missing bearer token'):
    return _err_payload("unauthorized", msg, status=401)

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


# Helper to coerce request id from body or headers
def _coerce_req_id_from(request: Request, body: dict | None) -> str:
    rid = None
    if isinstance(body, dict):
        rid = body.get("id") or body.get("request_id")
    if not rid:
        rid = request.headers.get("X-Request-Id")
    return rid or str(uuid.uuid4())

# ---- Tools/tags registry ----
SERVER_VERSION = os.getenv("MCP_SERVER_VERSION", "v1")

@app.get("/health")
async def health(request: Request):
    rid = request.get("id") or request.get("request_id")
    info = {
        "ok": True,
        "ts_jst": _now_jst(),
        "id": rid,
        "server_version": SERVER_VERSION,
        "mode": EFFECTIVE_MODE,
        "mode_reason": MODE_REASON,
        "ansible_bin": ANSIBLE_BIN,
        "require_auth": REQUIRE_AUTH,
        "base_dir": str(BASE_DIR),
    }
    return info


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

@app.post("/mcp")
async def mcp(request: Request):
    # Auth guard
    ok, resp = await _auth(request)
    if not ok:
        return resp

    # Parse body & set correlation id
    body = await request.json()
    global _REQ_ID
    try:
        _REQ_ID = _coerce_req_id_from(request, body)
    except Exception:
        _REQ_ID = None

    # Normalize input: prefer {tool, vars, origin_text}; allow {name, arguments}
    tool = body.get("tool") or body.get("name")
    vars_ = body.get("vars") or body.get("arguments") or {}
    if not isinstance(vars_, dict):
        vars_ = {"value": vars_}
    origin_text = body.get("origin_text") or ""

    _mcp_log(6, "mcp request", {"body": {"tool": tool, "vars": vars_, "origin_text": origin_text}})

    # 1) Lightweight tool: echo (no Ansible)
    if tool == "mcp.test.echo":
        msg = vars_.get("text") if isinstance(vars_, dict) else ""
        resp = {"ok": True, "text": msg or "(empty)", "ts_jst": _now_jst()}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(11, "mcp reply", {"status": 200, "summary": "echo"})
        return JSONResponse(resp, status_code=200)

    # 2) Explicit playbook execution only (no planning)
    if isinstance(tool, str) and tool.startswith("playbooks/") and tool.endswith(".yml"):
        pb_path = Path(tool)
        if not pb_path.is_absolute():
            pb_path = (BASE_DIR / pb_path).resolve()
        if not pb_path.exists():
            details = {"path": str(pb_path)}
            err_resp = _err_payload("unknown_tool", f"playbook not found: {pb_path}", details=details, status=400)
            _mcp_log(11, "mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found: {pb_path}", "details": details}})
            return err_resp

        # Use provided vars as-is
        extra_vars = dict(vars_) if isinstance(vars_, dict) else {}
        reply = _run_ansible(str(pb_path), extra_vars)

        # Compose summary (host/feature are optional)
        host = extra_vars.get("host")
        feature = extra_vars.get("feature")
        if host and feature:
            summary = f"ホスト「{host}」の {feature} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
        else:
            summary = f"{pb_path.name} 実行（mode={reply['mode']}）"

        dbg = {"request": {"tool": tool, "vars": extra_vars}, "ansible": reply}
        resp = {"ok": True, "summary": summary, "ansible": {"rc": reply["rc"], "ok": reply["ok"]}, "ts_jst": _now_jst(), "debug": dbg}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(11, "mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
        return JSONResponse(resp, status_code=200)

    # 3) Unsupported tool (no implicit planning)
    details = {"tool": tool}
    err_resp = _err_payload("unsupported_tool", f"unsupported tool for /mcp: {tool}", details=details, status=400)
    _mcp_log(11, "mcp reply", {"status": 400, "error": {"code": "unsupported_tool", "message": f"unsupported tool for /mcp: {tool}", "details": details}})
    return err_resp

@app.post("/run")
async def run(request: Request):
    ok, resp = await _auth(request)
    if not ok:
        return resp

    body = await request.json()
    # capture correlation id (accept JSON id/request_id or X-Request-Id header)
    global _REQ_ID
    try:
        _REQ_ID = _coerce_req_id_from(request, body)
    except Exception:
        _REQ_ID = None
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
        details = {"path": str(pb_path)}
        err_resp = _err_payload("unknown_tool", f"playbook not found: {pb_path}", details=details, status=400)
        _mcp_log(11, "mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found: {pb_path}", "details": details}})
        return err_resp

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
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
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
    if _REQ_ID:
        resp["request_id"] = _REQ_ID
    _mcp_log(10, "mcp gpt output", {"summary": summary})
    _mcp_log(11, "mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
    return resp
