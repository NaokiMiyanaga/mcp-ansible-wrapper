import os
import json
import datetime
import asyncio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import subprocess

app = FastAPI()

# === 設定 ===
MCP_TOKEN = os.environ.get("MCP_TOKEN", "secret123")
MCP_ALLOW = os.environ.get("MCP_ALLOW", "playbooks/*.yml").split()
MCP_WORKDIR = os.environ.get("MCP_WORKDIR", "/app")
DEBUG_MODE = True  # 既定で ON

# === セッションID & ログファイル ===
SESSION_ID = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_DIR = os.path.join(MCP_WORKDIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"session-{SESSION_ID}.jsonl")


def _log_event(event_type: str, payload: dict):
    """1行JSONでログに追記"""
    try:
        with open(LOG_FILE, "a") as f:
            rec = {
                "ts": datetime.datetime.now().isoformat(),
                "event": event_type,
                "payload": payload,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARN] logging failed: {e}")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """すべてのリクエスト/レスポンスをロギング"""
    body = await request.body()
    _log_event("request", {
        "path": request.url.path,
        "method": request.method,
        "body": body.decode("utf-8", errors="ignore"),
    })
    try:
        response = await call_next(request)
    except Exception as e:
        _log_event("error", {"error": str(e)})
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    resp_body = b""
    async for chunk in response.body_iterator:
        resp_body += chunk
    _log_event("response", {
        "status": response.status_code,
        "body": resp_body.decode("utf-8", errors="ignore"),
    })
    return Response(content=resp_body, status_code=response.status_code,
                    headers=dict(response.headers))


# === ヘルスチェック ===
@app.get("/health")
async def health():
    return {"ok": True, "status": 200, "allow": MCP_ALLOW, "debug": DEBUG_MODE}


# === デバッグ制御 ===
@app.get("/debug/on")
async def debug_on():
    global DEBUG_MODE
    DEBUG_MODE = True
    _log_event("debug", {"mode": "on"})
    return {"ok": True, "debug": DEBUG_MODE}


@app.get("/debug/off")
async def debug_off():
    global DEBUG_MODE
    DEBUG_MODE = False
    _log_event("debug", {"mode": "off"})
    return {"ok": True, "debug": DEBUG_MODE}


@app.get("/debug")
async def debug_status():
    return {"ok": True, "debug": DEBUG_MODE}


# === トレース出力 ===
@app.get("/trace")
async def trace_all():
    try:
        with open(LOG_FILE, "r") as f:
            lines = [json.loads(l) for l in f.readlines()]
        return {"ok": True, "session": SESSION_ID, "events": lines}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/trace/tail")
async def trace_tail(lines: int = 100):
    try:
        with open(LOG_FILE, "r") as f:
            data = f.readlines()[-lines:]
        return {"ok": True, "session": SESSION_ID,
                "events": [json.loads(l) for l in data]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/trace/raw")
async def trace_raw():
    def iterfile():
        with open(LOG_FILE, "rb") as f:
            while chunk := f.read(1024):
                yield chunk
    return StreamingResponse(iterfile(), media_type="text/plain")


@app.get("/trace/stream")
async def trace_stream():
    async def event_gen():
        with open(LOG_FILE, "r") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    yield f"data: {line}\n\n"
                else:
                    await asyncio.sleep(1)
    return StreamingResponse(event_gen(), media_type="text/event-stream")


# === MCP 実行 ===
@app.post("/mcp/run")
async def mcp_run(request: Request):
    try:
        auth = request.headers.get("Authorization", "")
        if not auth.endswith(MCP_TOKEN):
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

        data = await request.json()
        playbook = data.get("playbook")
        limit = data.get("limit", "all")
        extra_vars = data.get("extra_vars", {})

        if not playbook:
            return {"ok": False, "error": "playbook required"}

        # 許可リスト検証
        allowed = any(
            playbook.startswith(pat.replace("*", "")) for pat in MCP_ALLOW
        )
        if not allowed:
            return {"ok": False, "error": f"playbook not allowed: {playbook}"}

        # パス正規化
        abs_path = os.path.abspath(os.path.join(MCP_WORKDIR, playbook))
        if not abs_path.startswith(MCP_WORKDIR):
            return {"ok": False, "error": "invalid playbook path"}

        cmd = [
            "ansible-playbook", abs_path, "-i", os.path.join(MCP_WORKDIR, "inventory.ini"),
            "--limit", limit
        ]
        if extra_vars:
            cmd += ["--extra-vars", json.dumps(extra_vars)]

        _log_event("ansible_cmd", {"cmd": cmd})

        proc = subprocess.run(cmd, capture_output=True, text=True)
        result = {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        _log_event("ansible_result", result)
        return result

    except Exception as e:
        _log_event("error", {"error": str(e)})
        return {"ok": False, "error": str(e)}