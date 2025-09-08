# --- 先頭付近 ---
import os, glob, json, time, datetime, pathlib
from fastapi import FastAPI, Request
APP = FastAPI()

DEBUG_MODE = os.getenv("DEBUG_MODE", "1") == "1"
BASE_DIR   = pathlib.Path(os.getenv("MCP_WORKDIR", "/app")).resolve()
LOG_DIR    = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def _parse_allow(raw: str):
    pats = []
    for tok in (raw or "playbooks/*.yml").replace(",", " ").split():
        t = tok.strip()
        if t: pats.append(t)
    return pats

ALLOW_PATTERNS = _parse_allow(os.getenv("MCP_ALLOW"))

def _log(ev, data):
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        f = LOG_DIR / f"session-{ts}.jsonl"
        with f.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps({"ts": time.time(), "ev": ev, **(data or {})},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass

@APP.get("/health")
def health():
    return {"ok": True, "status": 200, "allow": ALLOW_PATTERNS,
            "debug": DEBUG_MODE, "workdir": str(BASE_DIR)}

@APP.get("/debug")
def debug_status():
    return {"ok": True, "debug": DEBUG_MODE, "allow": ALLOW_PATTERNS}

@APP.post("/debug")
async def debug_toggle(req: Request):
    global DEBUG_MODE
    body = await req.json()
    on = bool(body.get("on", True))
    DEBUG_MODE = on
    _log("debug_toggle", {"on": on})
    return {"ok": True, "debug": DEBUG_MODE}

@APP.post("/refresh")
def refresh():
    global ALLOW_PATTERNS, BASE_DIR
    ALLOW_PATTERNS = _parse_allow(os.getenv("MCP_ALLOW"))
    BASE_DIR = pathlib.Path(os.getenv("MCP_WORKDIR", "/app")).resolve()
    _log("refreshed", {"allow": ALLOW_PATTERNS, "workdir": str(BASE_DIR)})
    return {"ok": True, "allow": ALLOW_PATTERNS, "workdir": str(BASE_DIR)}