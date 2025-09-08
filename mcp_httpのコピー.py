import os, fnmatch
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

APP = FastAPI()
app = APP  # uvicorn mcp_http:app / mcp_http:APP 両対応

WORKDIR = Path(os.getenv("MCP_WORKDIR", "/app")).resolve()
TOKEN   = os.getenv("MCP_TOKEN", "secret123")
# 例: "playbooks/*.yml *.yml" や "playbooks/*.yml,*.yml" どちらもOK
_allow_raw = os.getenv("MCP_ALLOW", "playbooks/*.yml")
ALLOW_PATTERNS = [p for p in _allow_raw.replace(",", " ").split() if p.strip()]

def _to_rel_under_workdir(p: str) -> str:
    pt = Path(p)
    if not pt.is_absolute():
        pt = (WORKDIR / pt).resolve()
    else:
        pt = pt.resolve()
    try:
        rel = pt.relative_to(WORKDIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="playbook must be under /app")
    return rel.as_posix()

def _allowed(rel_path: str) -> bool:
    return any(fnmatch.fnmatch(rel_path, pat) for pat in ALLOW_PATTERNS)

class RunRequest(BaseModel):
    playbook: str
    limit: Optional[str] = None
    extra_vars: Optional[dict] = None

@APP.get("/health")
def health():
    return {"ok": True, "status": 200, "allow": ALLOW_PATTERNS, "debug": True}

@APP.get("/debug")
def debug_status():
    return {"debug": True}

@APP.post("/debug/on")
@APP.get("/debug/on")
def debug_on():
    return {"debug": True}

@APP.post("/debug/off")
@APP.get("/debug/off")
def debug_off():
    return {"debug": False}

@APP.post("/mcp/run")
def run_playbook(body: RunRequest):
    rel = _to_rel_under_workdir(body.playbook)
    if not _allowed(rel):
        return {"ok": False, "error": f"playbook not allowed: {rel}"}

    import subprocess, json
    cmd = ["ansible-playbook", (WORKDIR / rel).as_posix()]
    if body.limit:
        cmd += ["-l", body.limit]
    if body.extra_vars:
        cmd += ["--extra-vars", json.dumps(body.extra_vars)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(WORKDIR), capture_output=True, text=True, timeout=90
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))