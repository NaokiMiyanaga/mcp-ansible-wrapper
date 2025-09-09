# -*- coding: utf-8 -*-
"""
MCP FastAPI app: GPT-planned Ansible + GPT summary (with fallbacks)
- JSONL logging (JST), dated filenames
- Numbering (mcp): 6(request),7(gpt input / plan),8(ansible req),9(ansible reply),10(gpt output / summary),11(final reply)
- Reply includes "debug" with No.7/8/9 for UI display (toggle via DEBUG_MCP, default=on)
"""
import os, re, json, pathlib, threading, subprocess
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from pydantic import BaseModel

# ===== Config & logger =====
JST = timezone(timedelta(hours=9))
RUN_TS = "20250909-225158"
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
AUDIT_PATH = os.path.join(LOG_DIR, f"mcp_events_{20250909-225158}.jsonl")
_log_lock = threading.Lock()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL_PLAN = os.environ.get("OPENAI_MODEL_PLAN", "gpt-4o-mini")
OPENAI_MODEL_SUM  = os.environ.get("OPENAI_MODEL_SUM", "gpt-4o-mini")
ANSIBLE_BIN = os.environ.get("ANSIBLE_BIN", "ansible-playbook")
ANSIBLE_RUN = os.environ.get("ANSIBLE_RUN", "stub")  # "exec" で本物実行
DEBUG_MCP = os.environ.get("DEBUG_MCP", "1") not in ("0","false","False")

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
            "ユーザの日本語を解析し、対象装置(host=r1/r2等)と機能(feature=bgp/ospf/bfd)を抽出し、"
            "実行すべきAnsibleプレイブックを1つ提案してください。返答はJSONで、"
            "keys=[host, feature, playbook, rationale] のみを含めてください。"
        )
        user = f"発話: {text}\n返答: JSONのみを返してください。"
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL_PLAN,
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0.0,
        )
        content = resp.choices[0].message.content
        try:
            js = json.loads(content)
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

# ---- Ansible execution ----
def call_ansible(payload: Dict[str, Any]) -> Dict[str, Any]:
    if ANSIBLE_RUN != "exec":
        return {
            "ok": True,
            "playbook": payload.get("playbook", "<stub>"),
            "vars": payload.get("vars", {}),
            "stdout": "stubbed ansible result",
            "rc": 0
        }
    try:
        vars_json = json.dumps(payload.get("vars", {}))
        proc = subprocess.run(
            [ANSIBLE_BIN, payload["playbook"], "-e", vars_json],
            capture_output=True, text=True, timeout=180
        )
        return {
            "ok": proc.returncode == 0,
            "playbook": payload.get("playbook"),
            "vars": payload.get("vars", {}),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "rc": proc.returncode
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

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

@app.get("/health", summary="Health")
def health():
    return {"ok": True, "ts_jst": _jst_now_iso()}

@app.post("/run", summary="Run")
async def run(payload: RunPayload, request: Request):
    # 6) request from Chainlit
    log_event(6, "mcp", payload.dict(), "mcp request")

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

    # 8) ansible request
    ansible_payload = {
        "playbook": plan["playbook"],
        "vars": {"host": plan["host"], "feature": plan["feature"], "score": payload.score, "decision": payload.decision},
    }
    log_event(8, "mcp", {"module": "ansible", "payload": ansible_payload}, "mcp ansible request")

    # 9) ansible reply
    ansible_reply = call_ansible(ansible_payload)
    log_event(9, "mcp", ansible_reply, "mcp ansible reply")

    # 10) summary
    gpt_out = gpt_summarize(plan["host"], plan["feature"], ansible_reply)
    gpt_output = {
        "summary": gpt_out["summary"],
        "decision": (payload.decision or "mcp"),
        "score": payload.score,
        "used_ansible": True,
        "plan_used_gpt": plan["used_gpt"],
        "summary_used_gpt": gpt_out["used_gpt"],
        "ansible_rc": ansible_reply.get("rc"),
        "ansible_ok": ansible_reply.get("ok", True)
    }
    log_event(10, "mcp", gpt_output, "mcp gpt output")

    # 11) final reply (+debug bundle for UI)
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

    if DEBUG_MCP:
        reply["debug"] = {
            "no7_plan": gpt_input,
            "no8_request": {"module": "ansible", "payload": ansible_payload},
            "no9_reply": ansible_reply
        }

    log_event(11, "mcp", reply, "mcp reply")
    return reply
