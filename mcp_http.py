import os, json, subprocess, tempfile
from pathlib import Path
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
from zoneinfo import ZoneInfo

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
    _mcp_log(-1, "health", info)
    return info

def _plan_from_text(text: str) -> Dict[str, Any]:
    t = (text or "").lower()
    host = "r1"
    for h in ["r1", "r2", "l2a", "l2b"]:
        if h in t:
            host = h
            break
    feature = "bgp" if ("bgp" in t or "ルーティング" in t or "経路" in t) else "bgp"
    return {"host": host, "feature": feature, "playbook": ("playbooks/show_bgp_deep.yml" if ("詳細" in text or "neighbor" in t or "ネイバー" in text or "deep" in t) else "playbooks/show_bgp.yml")}

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
    cmd = [ANSIBLE_BIN, playbook, "-e", f"@{ev}"]
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

    plan = _plan_from_text(text)
    _mcp_log(7, "mcp gpt input", {"prompt": text, "decision": decision, "score": score, "plan": plan})
    explicit_pb: Optional[str] = payload.get("playbook") if isinstance(payload, dict) else None
    chosen_pb = explicit_pb or plan.get("playbook")

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

    reply = _run_ansible(str(pb_path), extra_vars)
    debug = {
        "no7_plan": {"prompt": text, "decision": decision, "score": score, "plan": plan},
        "no8_request": {"module": "ansible", "payload": {"playbook": str(pb_path), "vars": extra_vars},
                        "effective_mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON, "ansible_bin": ANSIBLE_BIN},
        "no9_reply": reply,
    }
    summary = f"ホスト「{extra_vars.get('host')}」の {extra_vars.get('feature')} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
    resp = {"ok": True, "decision": decision, "summary": summary, "score": score,
            "ansible": {"rc": reply["rc"], "ok": reply["ok"]}, "ts_jst": _now_jst(), "debug": debug}
    _mcp_log(10, "mcp gpt output", {"summary": summary})
    _mcp_log(11, "mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
    return resp
