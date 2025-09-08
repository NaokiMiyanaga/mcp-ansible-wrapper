# mcp_http.py — FastAPI app for MCP Ansible wrapper
from fastapi import FastAPI, Request, Header
from pydantic import BaseModel, Field
from typing import Optional, Dict, List
import os, json, time, datetime, pathlib, subprocess, shlex, fnmatch

# ----------------------
# Globals & helpers
# ----------------------
app = FastAPI(title="MCP Ansible Wrapper", version="1.0.0")

BASE_DIR   = pathlib.Path(os.getenv("MCP_WORKDIR", "/app")).resolve()
LOG_DIR    = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")

def _parse_allow(raw: Optional[str]) -> List[str]:
    """Allow patterns: supports comma or whitespace separated (e.g. 'playbooks/*.yml,*.yml')."""
    raw = (raw or "playbooks/*.yml")
    items = []
    for tok in raw.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            items.append(tok)
    return items

def _allowed(playbook: str, patterns: List[str]) -> bool:
    # normalize relative paths (no absolute allowed)
    p = playbook.lstrip("./")
    return any(fnmatch.fnmatch(p, pat) for pat in patterns)

def _log(event: str, data: Dict):
    try:
        fname = LOG_DIR / f"session-{datetime.datetime.now():%Y%m%d-%H%M%S}.jsonl"
        rec = {"ts": _now_iso(), "event": event, **(data or {})}
        with fname.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # logging must not crash the server
        print("[log-error]", e)

def _env_debug() -> bool:
    return os.getenv("DEBUG_MODE", "1") == "1"

def _env_token() -> str:
    return os.getenv("MCP_TOKEN", "secret123")

def _env_allow() -> List[str]:
    return _parse_allow(os.getenv("MCP_ALLOW"))

# ----------------------
# Models
# ----------------------
class RunBody(BaseModel):
    playbook: str = Field(..., description="Path to an ansible playbook (relative to /app)")
    limit: Optional[str] = Field("all", description="Host pattern for -l/--limit")
    extra_vars: Optional[Dict] = Field(default_factory=dict, description="Variables passed to --extra-vars")

# ----------------------
# Health / Debug / Refresh
# ----------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "status": 200,
        "workdir": str(BASE_DIR),
        "allow": _env_allow(),
        "debug": _env_debug()
    }

@app.get("/debug")
def debug_status():
    return {"ok": True, "debug": _env_debug(), "allow": _env_allow()}

@app.post("/debug")
async def debug_toggle(req: Request):
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    on = bool(body.get("on", True))
    os.environ["DEBUG_MODE"] = "1" if on else "0"
    _log("debug_toggle", {"on": on})
    return {"ok": True, "debug": _env_debug()}

@app.post("/refresh")
def refresh():
    # いまは環境変数の再読込だけ（プロセス再起動はしない）
    allow = _env_allow()
    _log("refresh", {"allow": allow, "workdir": str(BASE_DIR)})
    return {"ok": True, "allow": allow, "workdir": str(BASE_DIR)}

# ----------------------
# /mcp/run — main entry
# ----------------------
@app.post("/mcp/run")
def mcp_run(payload: RunBody,
            authorization: Optional[str] = Header(default=None)):
    t0 = time.time()

    # Auth (Bearer)
    expected = _env_token()
    if expected:
        provided = (authorization or "")
        if not provided.startswith("Bearer "):
            _log("auth_fail", {"reason": "no_bearer"})
            return {"ok": False, "exit_code": 401, "stdout": "", "stderr": "Unauthorized (no bearer token)"}
        token = provided.split(" ", 1)[1].strip()
        if token != expected:
            _log("auth_fail", {"reason": "token_mismatch"})
            return {"ok": False, "exit_code": 401, "stdout": "", "stderr": "Unauthorized (token mismatch)"}

    playbook = payload.playbook.strip()
    limit    = (payload.limit or "all").strip()
    xvars    = payload.extra_vars or {}

    # Allowlist
    allow_patterns = _env_allow()
    if not _allowed(playbook, allow_patterns):
        _log("allow_block", {"playbook": playbook, "allow": allow_patterns})
        return {"ok": False, "exit_code": 400, "stdout": "",
                "stderr": f"playbook not allowed: {playbook}"}

    # Paths
    pb_path = (BASE_DIR / playbook.lstrip("./")).resolve()
    inv_ini = (BASE_DIR / "inventory.ini").resolve()
    ans_cfg = (BASE_DIR / "ansible.cfg").resolve()

    if not pb_path.exists():
        _log("playbook_missing", {"path": str(pb_path)})
        return {"ok": False, "exit_code": 2, "stdout": "", "stderr": f"playbook not found: {pb_path}"}
    if not inv_ini.exists():
        _log("inventory_missing", {"path": str(inv_ini)})
        return {"ok": False, "exit_code": 2, "stdout": "", "stderr": f"inventory not found: {inv_ini}"}

    # ansible-playbook command
    cmd = [
        "ansible-playbook",
        str(pb_path),
        "-i", str(inv_ini),
        "-l", limit or "all",
        "-e", json.dumps(xvars, ensure_ascii=False)
    ]
    # Make sure we run in BASE_DIR so relative includes work
    _log("ansible_request", {
        "cmd": " ".join(shlex.quote(c) for c in cmd),
        "workdir": str(BASE_DIR),
        "extra_vars": xvars
    })

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True
        )
        dt = time.time() - t0
        out = proc.stdout or ""
        err = proc.stderr or ""
        _log("ansible_result", {
            "exit_code": proc.returncode,
            "elapsed_sec": round(dt, 3),
            "stdout_tail": out[-2000:],  # 過大なログは末尾だけ
            "stderr_tail": err[-2000:]
        })
        return {
            "ok": (proc.returncode == 0),
            "exit_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "elapsed_sec": dt
        }
    except FileNotFoundError as e:
        # ansible-playbook が無い
        _log("ansible_error", {"error": str(e)})
        return {"ok": False, "exit_code": 127, "stdout": "", "stderr": "ansible-playbook not found"}
    except Exception as e:
        _log("ansible_error", {"error": str(e)})
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": str(e)}