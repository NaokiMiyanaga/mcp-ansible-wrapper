# -*- coding: utf-8 -*-
"""
Minimal MCP FastAPI app with unified JSONL event logging (JST) and dated filenames.

File naming (host):
- /Users/naoki/devNet/mcp-ansible-wrapper/logs/mcp_events_20250909-202429.jsonl

Container path:
- /app/logs/mcp_events_20250909-202429.jsonl  (assuming ./logs is mounted to /app/logs)
"""
import os
import json
import pathlib
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from pydantic import BaseModel

# ===== Unified JSONL event logger (MCP) =====
JST = timezone(timedelta(hours=9))
RUN_TS = "20250909-202429"  # fixed at process start
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
AUDIT_PATH = os.path.join(LOG_DIR, f"mcp_events_{RUN_TS}.jsonl")

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
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
# ===== /logger =====

app = FastAPI()

class RunPayload(BaseModel):
    text: Optional[str] = None
    decision: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

@app.get("/health")
def health():
    return {"ok": True, "ts_jst": _jst_now_iso()}

@app.post("/run")
async def run(payload: RunPayload, request: Request):
    # 5) mcp request: received from chainlit
    log_event(5, "mcp", payload.dict(), "mcp request")

    # 6) mcp gpt input: construct prompt for GPT (placeholder)
    gpt_input = {"prompt": (payload.text or "").strip(), "decision": payload.decision}
    log_event(6, "mcp", gpt_input, "mcp gpt input")

    # 7) mcp gpt output: stubbed GPT response (replace with real call)
    gpt_output = {"summary": (payload.text or "")[:200], "decision": payload.decision or "pass"}
    log_event(7, "mcp", gpt_output, "mcp gpt output")

    # 8) mcp reply: final reply produced by MCP
    reply = {
        "ok": True,
        "echo": payload.text,
        "decision": gpt_output["decision"],
        "summary": gpt_output["summary"],
        "ts_jst": _jst_now_iso()
    }
    log_event(8, "mcp", reply, "mcp reply")
    return reply
