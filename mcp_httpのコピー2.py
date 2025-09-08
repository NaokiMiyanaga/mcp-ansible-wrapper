import os
import fnmatch
import subprocess
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
APP = FastAPI(title="MCP Ansible Wrapper", version="1.0.0")
app = APP  # uvicorn mcp_http:app でも mcp_http:APP でもOKにするため

# -----------------------------------------------------------------------------
# Config / Paths
# -----------------------------------------------------------------------------
BASE_DIR = Path(os.getenv("MCP_WORKDIR", "/app")).resolve()
MCP_TOKEN = os.getenv("MCP_TOKEN", "secret123")

# 許可パターン（スペース or カンマ区切り対応）
_raw = os.getenv("MCP_ALLOW", "playbooks/*.yml")
ALLOW_PATTERNS = [p.strip() for p in _raw.replace(",", " ").split() if p.strip()]

LOG_DIR = (BASE_DIR / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 起動単位で1ファイルのJSONLにまとめる（tailしやすくする）
SESSION_TS = datetime.now().strftime("%Y%m%d-%H%M%S")
SESSION_FILE = LOG_DIR / f"session-{SESSION_TS}.jsonl"

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _safe_json_dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"_nonserializable": str(obj), "_error": str(e)})

def _log(kind: str, payload: dict):
    # ログの失敗で /health を巻き添えにしない
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SESSION_FILE, "a") as f:
            f.write(_safe_json_dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "payload": payload
            }) + "\n")
    except Exception as e:
        # どうしても書けない場合は捨てる（/health を優先）
        pass

def _to_rel_under_base(path_str: str) -> str:
    p = Path(path_str)
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    else:
        p = p.resolve()
    try:
        rel = p.relative_to(BASE_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="playbook must be under /app")
    return rel.as_posix()

def _is_allowed(rel_path: str) -> bool:
    return any(fnmatch.fnmatch(rel_path, pat) for pat in ALLOW_PATTERNS)

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class RunRequest(BaseModel):
    playbook: str
    limit: Optional[str] = None
    extra_vars: Optional[dict] = None

# -----------------------------------------------------------------------------
# Health / Trace endpoints
# -----------------------------------------------------------------------------
@APP.get("/health")
def health():
    # ここは絶対に 200 を返す（内部状態に依存しない）
    return {
        "ok": True,
        "status": 200,
        "allow": ALLOW_PATTERNS,
        "debug": True,
        "workdir": BASE_DIR.as_posix(),
        "logfile": SESSION_FILE.as_posix(),
    }

@APP.get("/trace")
def trace_all():
    try:
        if not SESSION_FILE.exists():
            return {"file": SESSION_FILE.as_posix(), "entries": []}
        with open(SESSION_FILE, "r") as f:
            lines = f.readlines()
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except Exception:
                entries.append({"_raw": line})
        return {"file": SESSION_FILE.as_posix(), "entries": entries}
    except Exception as e:
        # traceが壊れてもサーバは生きたまま
        return {"file": SESSION_FILE.as_posix(), "error": str(e), "entries": []}

@APP.get("/trace/tail")
def trace_tail(lines: int = Query(200, ge=1, le=2000)):
    try:
        if not SESSION_FILE.exists():
            return {"file": SESSION_FILE.as_posix(), "entries": []}
        with open(SESSION_FILE, "r") as f:
            buf = f.readlines()[-lines:]
        entries = []
        for line in buf:
            try:
                entries.append(json.loads(line))
            except Exception:
                entries.append({"_raw": line})
        return {"file": SESSION_FILE.as_posix(), "entries": entries}
    except Exception as e:
        return {"file": SESSION_FILE.as_posix(), "error": str(e), "entries": []}

# -----------------------------------------------------------------------------
# Main endpoint
# -----------------------------------------------------------------------------
@APP.post("/mcp/run")
def run_playbook(body: RunRequest):
    # 認証（必要ならここでトークン検査を入れる）
    # 例: from fastapi import Header; pass Authorization header
    # ただ今回は /health が落ちないように簡素に維持

    rel = _to_rel_under_base(body.playbook)

    if not _is_allowed(rel):
        msg = f"playbook not allowed: {rel}"
        _log("allow_deny", {"playbook": rel, "reason": msg, "allow": ALLOW_PATTERNS})
        return {"ok": False, "error": msg}

    # デフォルトで "all" を使う（localhost事故の防止）
    limit = body.limit or "all"

    cmd = ["ansible-playbook", (BASE_DIR / rel).as_posix()]
    if limit:
        cmd += ["-l", limit]
    if body.extra_vars:
        cmd += ["--extra-vars", json.dumps(body.extra_vars, ensure_ascii=False)]

    # ansible の色コードを抑制（ログ可読性）
    env = dict(os.environ)
    env["ANSIBLE_FORCE_COLOR"] = "0"

    t0 = time.time()
    _log("ansible_request", {"cmd": cmd})

    try:
        p = subprocess.run(
            cmd, cwd=str(BASE_DIR),
            capture_output=True, text=True,
            timeout=180, env=env
        )
        res = {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
            "elapsed_sec": round(time.time() - t0, 3)
        }
        _log("ansible_result", {
            "code": p.returncode,
            "stdout_head": p.stdout[:2000],
            "stderr_head": p.stderr[:1000]
        })
        return res

    except subprocess.TimeoutExpired:
        _log("ansible_timeout", {"cmd": cmd})
        raise HTTPException(status_code=504, detail="ansible-playbook timeout")
    except Exception as e:
        _log("ansible_exception", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))