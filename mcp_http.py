# -*- coding: utf-8 -*-
"""
MCP FastAPI app: robust 6→11 flow with always-on logging
- JSONL logging (JST), dated filenames
- Numbering (mcp): 6(request),7(gpt input),8(ansible req),9(ansible reply),10(gpt output),11(final reply)
- Safer extraction of host/feature; No.10 includes used_ansible flag
"""
import os, re, json, pathlib, threading
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from pydantic import BaseModel

# ===== Config & logger =====
JST = timezone(timedelta(hours=9))
RUN_TS = "20250909-220232"
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
AUDIT_PATH = os.path.join(LOG_DIR, f"mcp_events_{RUN_TS}.jsonl")
_log_lock = threading.Lock()

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

def extract_host(text: Optional[str]) -> str:
    if not text:
        return "r1"
    m = re.search(r"\b(r\d+)\b", text.lower())
    return m.group(1) if m else "r1"

def extract_feature(text: Optional[str]) -> str:
    if not text:
        return "bgp"
    tl = text.lower()
    if re.search(r"\bbgp\b", tl):
        return "bgp"
    if re.search(r"\bospf\b", tl):
        return "ospf"
    if re.search(r"\bbfd\b", tl):
        return "bfd"
    return "bgp"

def call_ansible(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Stub for Ansible integration. Replace with real execution."""
    try:
        return {
            "ok": True,
            "playbook": payload.get("playbook", "<stub>"),
            "vars": payload.get("vars", {}),
            "stdout": "stubbed ansible result",
            "rc": 0
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/health", summary="Health")
def health():
    return {"ok": True, "ts_jst": _jst_now_iso()}

@app.post("/run", summary="Run")
async def run(payload: RunPayload, request: Request):
    # 6) request from Chainlit
    try:
        log_event(6, "mcp", payload.dict(), "mcp request")
    except Exception as _:
        pass

    # 7) GPT input (request to GPT) — Before Ansible
    gpt_input = {"prompt": (payload.text or "").strip(), "decision": payload.decision, "score": payload.score}
    try:
        log_event(7, "mcp", gpt_input, "mcp gpt input")
    except Exception as _:
        pass

    # Extract targets
    host = extract_host(payload.text)
    feature = extract_feature(payload.text)

    # 8) ansible request
    playbook = "playbooks/show_%s.yml" % feature if feature else "playbooks/show_bgp.yml"
    ansible_payload = {
        "playbook": playbook,
        "vars": {"host": host, "feature": feature, "score": payload.score, "decision": payload.decision},
    }
    try:
        log_event(8, "mcp", {"module": "ansible", "payload": ansible_payload}, "mcp ansible request")
    except Exception as _:
        pass

    # 9) ansible reply
    try:
        ansible_reply = call_ansible(ansible_payload)
    except Exception as e:
        ansible_reply = {"ok": False, "error": str(e)}
    try:
        log_event(9, "mcp", ansible_reply, "mcp ansible reply")
    except Exception as _:
        pass

    # 10) gpt output (reply) — After Ansible (stub summary)
    gpt_output = {
        "summary": f"{host} の {feature} 状態を取得しました（stub）",
        "decision": (payload.decision or "mcp"),
        "score": payload.score,
        "used_ansible": True,
        "ansible_rc": ansible_reply.get("rc"),
        "ansible_ok": ansible_reply.get("ok", True)
    }
    try:
        log_event(10, "mcp", gpt_output, "mcp gpt output")
    except Exception as _:
        pass

    # 11) final reply
    reply = {
        "ok": bool(ansible_reply.get("ok", True)),
        "decision": gpt_output.get("decision"),
        "summary": gpt_output.get("summary"),
        "score": gpt_output.get("score"),
        "ansible": {"rc": ansible_reply.get("rc"), "ok": ansible_reply.get("ok", True)},
        "ts_jst": _jst_now_iso()
    }
    if not ansible_reply.get("ok", True):
        reply["error"] = ansible_reply.get("error", "ansible error")
    try:
        log_event(11, "mcp", reply, "mcp reply")
    except Exception as _:
        pass

    return reply
