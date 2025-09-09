# -*- coding: utf-8 -*-
"""
MCP: robust No.9 + playbook proposal + exec/stub diagnostics
"""
import os, re, json, pathlib, threading, subprocess, shutil
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from pydantic import BaseModel

# ===== Config & logger =====
JST = timezone(timedelta(hours=9))
RUN_TS = "20250909-232006"
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
AUDIT_PATH = os.path.join(LOG_DIR, f"mcp_events_20250909-232006.jsonl")
_log_lock = threading.Lock()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL_PLAN = os.environ.get("OPENAI_MODEL_PLAN", "gpt-4o-mini")
OPENAI_MODEL_SUM  = os.environ.get("OPENAI_MODEL_SUM", "gpt-4o-mini")

# Resolve ansible binary
ENV_ANSIBLE_BIN = os.environ.get("ANSIBLE_BIN")
AUTO_ANSIBLE_BIN = shutil.which("ansible-playbook")
DEFAULT_ANSIBLE_BIN = "/usr/local/bin/ansible-playbook"
ANSIBLE_BIN = ENV_ANSIBLE_BIN or AUTO_ANSIBLE_BIN or DEFAULT_ANSIBLE_BIN

# Decide effective mode
REQ_MODE = (os.environ.get("ANSIBLE_RUN", "stub") or "stub").lower()  # 'exec' or 'stub'
if REQ_MODE == "exec" and (ANSIBLE_BIN and os.path.exists(ANSIBLE_BIN)):
    EFFECTIVE_MODE = "exec"
    MODE_REASON = "exec: ansible-playbook available"
elif REQ_MODE == "exec":
    EFFECTIVE_MODE = "stub"
    MODE_REASON = f"stub: ANSIBLE_BIN not found at {ANSIBLE_BIN}"
else:
    EFFECTIVE_MODE = "stub"
    MODE_REASON = "stub: ANSIBLE_RUN!=exec"

DEBUG_MCP = os.environ.get("DEBUG_MCP", "1") not in ("0","false","False")
ALLOW_PLAYBOOK_CREATE = os.environ.get("ALLOW_PLAYBOOK_CREATE","1") not in ("0","false","False")

def _jst_now_iso() -> str:
    return datetime.now(JST).isoformat()

def log_event(no: int, actor: str, content: Any, tag: str) -> None:
    rec = {
        "ts_jst": _jst_now_iso(),
        "no": int(no),
        "actor": actor,
        "content": content,
        "tag": tag,
    }
    line = json.dumps(rec, ensure_ascii=False)
    with _log_lock:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")

app = FastAPI(title="MCP")

class RunPayload(BaseModel):
    text: Optional[str] = None
    decision: Optional[str] = None
    score: Optional[int] = None
    extra: Optional[Dict[str, Any]] = None

# ---- Fallback extractors ----
def fallback_extract(text: Optional[str]) -> Dict[str, str]:
    tl = (text or "").lower()
    host_m = re.search(r"\b(r\d+)\b", tl)
    feature = "bgp"
    if re.search(r"\bospf\b", tl): feature = "ospf"
    elif re.search(r"\bbfd\b", tl): feature = "bfd"
    return {"host": host_m.group(1) if host_m else "r1", "feature": feature}

# ---- GPT planning ----
def gpt_plan(text: str) -> Dict[str, Any]:
    # fallbacks if no API
    if not OPENAI_API_KEY:
        f = fallback_extract(text)
        return {
            "host": f["host"], "feature": f["feature"],
            "playbook": f"playbooks/show_{f['feature']}.yml",
            "rationale": "fallback regex (no OPENAI_API_KEY)",
            "used_gpt": False
        }
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        sys = (
            "あなたはネットワーク運用のSREです。"
            "ユーザ入力から host と feature を抽出し、Ansibleプレイブック名を1つ提案。"
            "返答は JSON (host, feature, playbook, rationale) のみ。"
        )
        user = f"発話: {text}\n返答: JSONのみ"
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL_PLAN,
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0.0,
        )
        try:
            js = json.loads(resp.choices[0].message.content)
        except Exception:
            js = {}
        host = (js.get("host") or "").strip() or fallback_extract(text)["host"]
        feature = (js.get("feature") or "").strip() or fallback_extract(text)["feature"]
        playbook = (js.get("playbook") or "").strip() or f"playbooks/show_{feature}.yml"
        rationale = js.get("rationale") or "auto-planned by GPT"
        return {"host": host, "feature": feature, "playbook": playbook, "rationale": rationale, "used_gpt": True}
    except Exception as e:
        f = fallback_extract(text)
        return {
            "host": f["host"], "feature": f["feature"],
            "playbook": f"playbooks/show_{f['feature']}.yml",
            "rationale": "fallback due to error: " + str(e),
            "used_gpt": False
        }

# ---- Playbook proposal / creation ----
def propose_new_playbook(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    suggested = plan.get("playbook") or ""
    # safe relative path under playbooks/
    base = "playbooks"
    name = os.path.basename(suggested) or f"show_{plan.get('feature','x')}.yml"
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)
    target = os.path.join(base, safe)
    return {"path": target, "feature": plan.get("feature"), "host": plan.get("host")}

def scaffold_playbook(path:str, host:str, feature:str, text:str) -> str:
    yaml = f"""---
# Auto-scaffolded at {_jst_now_iso()}
# Purpose : {feature} status/ops
# Hint    : fill tasks to query device {host}
# Context : user said -> {text}
- name: {feature} ops for {host}
  hosts: all
  gather_facts: no
  tasks:
    - name: TODO implement {feature} tasks
      debug:
        msg: "scaffold for {feature} on {host}"
"""
    try:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml)
        return "created"
    except Exception as e:
        return f"error: {e}"

# ---- Ansible execution (with diagnostics) ----
def call_ansible(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = EFFECTIVE_MODE
    reason = MODE_REASON
    if mode != "exec":
        # stub path
        return {
            "ok": (REQ_MODE != "exec"),  # if user wanted exec but we stubbed, mark as failure
            "mode": mode,
            "mode_reason": reason,
            "playbook": payload.get("playbook", "<stub>"),
            "vars": payload.get("vars", {}),
            "stdout": "stubbed ansible result" if REQ_MODE != "exec" else "",
            "stderr": "" if REQ_MODE != "exec" else f"ansible-playbook not available: {ANSIBLE_BIN}",
            "rc": 0 if REQ_MODE != "exec" else 127
        }
    # exec path
    try:
        vars_json = json.dumps(payload.get("vars", {}))
        proc = subprocess.run(
            [ANSIBLE_BIN, payload["playbook"], "-e", vars_json],
            capture_output=True, text=True, timeout=180
        )
        return {
            "ok": proc.returncode == 0,
            "mode": mode,
            "mode_reason": reason,
            "ansible_bin": ANSIBLE_BIN,
            "playbook": payload.get("playbook"),
            "vars": payload.get("vars", {}),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "rc": proc.returncode
        }
    except Exception as e:
        return {
            "ok": False,
            "mode": mode,
            "mode_reason": f"exec exception: {e}",
            "playbook": payload.get("playbook"),
            "vars": payload.get("vars", {}),
            "stdout": "",
            "stderr": str(e),
            "rc": 2
        }

# ---- GPT summary ----
def gpt_summarize(host: str, feature: str, ansible_reply: Dict[str, Any]) -> Dict[str, Any]:
    summary = f"{host} の {feature} 状態を取得しました（stub要約）"
    used_gpt = False
    if OPENAI_API_KEY:
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            sys = (
                "あなたはネットワーク運用のSREです。"
                "以下のAnsible実行結果(JSON/テキスト)を読み、オペレータ向けに簡潔な要約を返してください。"
            )
            body = json.dumps(ansible_reply, ensure_ascii=False)[:8000]
            user = f"# 対象\nhost={host}, feature={feature}\n\n# 結果\n{body}"
            resp = openai.chat.completions.create(
                model=OPENAI_MODEL_SUM,
                messages=[{"role":"system","content":sys},{"role":"user","content":user}],
                temperature=0.2,
            )
            summary = resp.choices[0].message.content.strip()
            used_gpt = True
        except Exception:
            pass
    return {"summary": summary, "used_gpt": used_gpt}

@app.get("/health")
def health():
    return {
        "ok": True,
        "ts_jst": _jst_now_iso(),
        "effective_mode": EFFECTIVE_MODE,
        "mode_reason": MODE_REASON,
        "ansible_bin": ANSIBLE_BIN
    }

@app.post("/run")
async def run(payload: RunPayload, request: Request):
    # 6) request
    log_event(6, "mcp", payload.dict(), "mcp request")

    # create_playbook path
    if payload.extra and payload.extra.get("action") == "create_playbook":
        target = (payload.extra.get("path") or "").strip()
        host = (payload.extra.get("host") or "r1").strip()
        feature = (payload.extra.get("feature") or "bgp").strip()
        result = "denied"
        if ALLOW_PLAYBOOK_CREATE and target and target.startswith("playbooks/") and target.endswith(".yml"):
            result = scaffold_playbook(target, host, feature, payload.text or "")
        reply = {"ok": result=="created", "intent":"create_playbook", "path": target, "result": result, "ts_jst": _jst_now_iso()}
        log_event(11, "mcp", reply, "mcp reply")
        return reply

    # 7) planning
    plan = gpt_plan(payload.text or "")
    gpt_input = {
        "prompt": (payload.text or "").strip(),
        "decision": payload.decision,
        "score": payload.score,
        "plan": {"host": plan["host"], "feature": plan["feature"], "playbook": plan["playbook"], "used_gpt": plan["used_gpt"]},
        "rationale": plan.get("rationale")
    }
    log_event(7, "mcp", gpt_input, "mcp gpt input")

    # Propose new playbook if not exists
    suggested = plan["playbook"]
    pb_path = suggested if os.path.isabs(suggested) else os.path.join(".", suggested)
    exists = os.path.exists(pb_path)
    if not exists:
        proposal = propose_new_playbook(plan)
        reply = {
            "ok": True,
            "decision": payload.decision or "mcp",
            "summary": "実行可能なプレイブックが見つかりません。新規作成を提案します。",
            "intent": "needs_playbook",
            "ts_jst": _jst_now_iso()
        }
        if DEBUG_MCP:
            reply["debug"] = {"no7_plan": gpt_input, "propose_new_playbook": proposal}
        log_event(11, "mcp", reply, "mcp reply")
        return reply

    # 8) ansible request
    ansible_payload = {
        "playbook": suggested,
        "vars": {"host": plan["host"], "feature": plan["feature"], "score": payload.score, "decision": payload.decision}
    }
    log_event(8, "mcp", {"module": "ansible", "payload": ansible_payload,
                         "effective_mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON, "ansible_bin": ANSIBLE_BIN}, "mcp ansible request")

    # 9) ansible reply (always include mode diagnostics)
    ansible_reply = call_ansible(ansible_payload)
    log_event(9, "mcp", ansible_reply, "mcp ansible reply")

    # 10) summary
    gpt_out = gpt_summarize(plan["host"], plan["feature"], ansible_reply)
    gpt_output = {
        "summary": gpt_out["summary"] if ansible_reply.get("ok", False) else "Ansible 実行に失敗しました。",
        "decision": (payload.decision or "mcp"),
        "score": payload.score,
        "used_ansible": True,
        "plan_used_gpt": plan["used_gpt"],
        "summary_used_gpt": gpt_out["used_gpt"],
        "ansible_rc": ansible_reply.get("rc"),
        "ansible_ok": ansible_reply.get("ok", True)
    }
    log_event(10, "mcp", gpt_output, "mcp gpt output")

    # 11) final reply (+debug)
    reply = {
        "ok": bool(ansible_reply.get("ok", True)),
        "decision": gpt_output.get("decision"),
        "summary": gpt_output.get("summary"),
        "score": gpt_output.get("score"),
        "ansible": {"rc": ansible_reply.get("rc"), "ok": ansible_reply.get("ok", True)},
        "ts_jst": _jst_now_iso()
    }
    if not ansible_reply.get("ok", True):
        reply["error"] = (ansible_reply.get("stderr") or ansible_reply.get("mode_reason") or "ansible error")[:2000]

    if DEBUG_MCP:
        reply["debug"] = {
            "no7_plan": gpt_input,
            "no8_request": {"module": "ansible", "payload": ansible_payload,
                            "effective_mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON, "ansible_bin": ANSIBLE_BIN},
            "no9_reply": ansible_reply
        }

    log_event(11, "mcp", reply, "mcp reply")
    return reply
