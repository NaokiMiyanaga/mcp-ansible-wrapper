import os, json, subprocess, tempfile, re, time, uuid
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urlunparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
from thinking import _plan_from_text
from knowledge import safe_lower, search_playbook, load_playbook_index

from fastapi import FastAPI, Request, HTTPException
from typing import Any, Dict

# -------- JSONL logger (MCP) --------
_START_TS = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S")
_MCP_LOG_DIR = Path(os.getenv("MCP_LOG_DIR", "/app/logs")).resolve()
_MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
_MCP_LOG_FILE = _MCP_LOG_DIR / f"mcp_events_{_START_TS}.jsonl"
_REQ_ID: Optional[str] = None

def _now_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()

def _mcp_log(id: str, no: int, tag: str, content: Any):
    rec = {"id": id, "ts_jst": _now_jst(), "no": no, "actor": "ansible-mcp", "tag": tag, "content": content}
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

# app = FastAPI(title="ansible-mcp")

app = FastAPI()


def _err_payload(code: str, message: str, details: Optional[Dict[str, Any]] = None, status: int = 200) -> JSONResponse:
    body: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if _REQ_ID:
        body["request_id"] = _REQ_ID
    return JSONResponse(body, status_code=status)

def _unauth(msg: str = 'missing bearer token'):
    return _err_payload("unauthorized", msg, status=401)

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


# Helper to coerce request id from body or headers
def _coerce_req_id_from(request: Request, body: dict | None) -> str:
    rid = None
    if isinstance(body, dict):
        rid = body.get("id") or body.get("request_id")
    if not rid:
        rid = request.headers.get("X-Request-Id")
#    return rid or str(uuid.uuid4())
    return rid


def _resolve_playbook_path(identifier: str) -> Optional[Path]:
    """Best-effort resolution of playbook identifier to filesystem path."""
    if not isinstance(identifier, str):
        return None
    ident = identifier.strip()
    if not ident:
        return None

    candidates = []
    path_obj = Path(ident)
    if path_obj.is_absolute():
        candidates.append(path_obj)
    else:
        candidates.append((BASE_DIR / path_obj).resolve())
        if not ident.startswith("playbooks/"):
            if not ident.endswith(('.yml', '.yaml')):
                candidates.append((BASE_DIR / "playbooks" / f"{ident}.yml").resolve())
                candidates.append((BASE_DIR / "playbooks" / f"{ident}.yaml").resolve())
            else:
                candidates.append((BASE_DIR / "playbooks" / ident).resolve())

    ident_lower = safe_lower(ident)
    if ident_lower:
        try:
            index = load_playbook_index(BASE_DIR)
        except Exception:
            index = []
        for item in index or []:
            pb = item.get("playbook")
            if not pb:
                continue
            intent_name = safe_lower(item.get("intent"))
            stem_name = safe_lower(Path(pb).stem)
            if ident_lower in (intent_name, stem_name):
                candidates.append((BASE_DIR / pb).resolve())

    for cand in candidates:
        try:
            if cand.exists():
                return cand
        except Exception:
            continue
    return None


def _collect_extra_vars(args: Dict[str, Any]) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    for key in ("default_vars", "vars", "extra_vars"):
        val = args.get(key)
        if isinstance(val, dict):
            extra.update(val)
    for key, value in args.items():
        if key in {"playbook", "path", "default_vars", "vars", "extra_vars"}:
            continue
        extra[key] = value
    return extra

# ---- Tools/tags registry ----
SERVER_VERSION = os.getenv("MCP_SERVER_VERSION", "v1")

@app.get("/health")
async def health(request: Request):
    rid = _coerce_req_id_from(request, None)
    info = {
        "ok": True,
        "ts_jst": _now_jst(),
        "id": rid,
        "server_version": SERVER_VERSION,
        "mode": EFFECTIVE_MODE,
        "mode_reason": MODE_REASON,
        "ansible_bin": ANSIBLE_BIN,
        "require_auth": REQUIRE_AUTH,
        "base_dir": str(BASE_DIR)
    }
    return info

# -------- /schema endpoint --------
@app.get("/schema")
async def schema(request: Request):
    # Auth (allow unauth schema if desired; keep consistent with /tools/*)
    ok, resp = await _auth(request)
    if not ok:
        return resp
    rid = _coerce_req_id_from(request, None)
    body = {
        "ok": True,
        "id": rid,
        "ts_jst": _now_jst(),
        "result": {
            "protocol": "mcp/1.0",
            "transport": "http",
            "capabilities": {"tools": True},
            "server_version": SERVER_VERSION,
            "endpoints": [
                {"path": "/tools/list", "method": "GET"},
                {"path": "/tools/call", "method": "POST"},
            ],
        },
    }
    return JSONResponse(body, status_code=200)

# -------- /tools/list endpoint --------
@app.get("/tools/list")
async def tools_list(request: Request):
    ok, resp = await _auth(request)
    if not ok:
        return resp
    rid = _coerce_req_id_from(request, None)
    tools = [
        {"name": "mcp.test.echo", "description": "Echo text", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "ansible.playbooks.list", "description": "List playbooks", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}, "include_fs": {"type": "boolean"}}}},
        {"name": "ansible.playbook", "description": "Run a playbook by intent or name", "input_schema": {"type": "object", "properties": {"playbook": {"type": "string"}, "default_vars": {"type": "object"}}}},
        {"name": "ansible.playbook_catalog", "description": "Catalog of playbooks (list/info)",
         "input_schema": {"type": "object", "properties": {"action": {"type": "string"}, "category": {"type": "string"}, "name": {"type": "string"}}, "required": ["action"]}},
        {"name": "ansible.select_playbook", "description": "Select a candidate playbook for an intent", "input_schema": {"type": "object", "properties": {"action": {"type": "string"}, "host": {"type": "string"}}}},
        {"name": "ansible.inventory", "description": "Show inventory (ansible-inventory --list)", "input_schema": {"type": "object", "properties": {}}},
        {"name": "playbook.run", "description": "Run a playbook (path, vars)", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "vars": {"type": "object"}}}},
    ]
    out = {
        "ok": True,
        "id": rid,
        "ts_jst": _now_jst(),
        "result": {"tools": tools, "count": len(tools)},
    }
    return JSONResponse(out, status_code=200)

# -------- /tools/call endpoint --------
@app.post("/tools/call")
async def tools_call(request: Request):
    # Auth guard
    ok, resp = await _auth(request)
    if not ok:
        return resp

    # Parse body & set correlation id
    body = await request.json()
    global _REQ_ID
    try:
        _REQ_ID = _coerce_req_id_from(request, body)
    except Exception:
        _REQ_ID = None

    # Normalize input: prefer {name, arguments}; accept {tool, vars}
    name = body.get("name") or body.get("tool")
    args = body.get("arguments") or body.get("vars") or {}
    if not isinstance(args, dict):
        args = {"value": args}

    _mcp_log(_REQ_ID, 6, "ansible-mcp request", {"body": {"name": name, "arguments": args}})

    rid = body.get("id")

    # 1) Echo
    if name == "mcp.test.echo":
        msg = args.get("text") if isinstance(args, dict) else ""
        resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": {"text": msg or "(empty)"}}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": "echo"})
        return JSONResponse(resp, status_code=200)

    # 2-a) Select playbook (planning only)
    if name == "ansible.select_playbook":
        action = safe_lower(args.get("action")) if isinstance(args, dict) else ""
        host = (args.get("host") if isinstance(args, dict) else None) or "r1"
        extra_vars = dict(args) if isinstance(args, dict) else {}

        # --- Direct fallback: if user passed an explicit playbook but no action keyword ---
        direct_playbook = None
        if not action:
            # Accept either explicit "playbook" field or legacy arg
            dp = extra_vars.get("playbook")
            if isinstance(dp, str) and dp.strip():
                direct_playbook = dp.strip()
        if direct_playbook:
            intent_name = safe_lower(Path(direct_playbook).stem.replace(".yml", ""))
            plan = {
                "playbook": direct_playbook,
                "extra_vars": {k: v for k, v in extra_vars.items() if k != "action"},
                "score": 0.0,
                "intent": intent_name,
            }
            plan["extra_vars"].setdefault("host", host)
            _direct_candidates = [{"intent": intent_name, "playbook": direct_playbook, "score": 0.0}]
            result = {
                "summary": f"Selected {Path(plan['playbook']).name} (direct)",
                "plan": plan,
                "candidates": _direct_candidates,
            }
            resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": result["summary"], "plan": plan, "candidates": _direct_candidates, "mode": "direct_playbook"})
            return JSONResponse(resp, status_code=200)

        # --- Normal similarity search path ---
        candidates = search_playbook(action, BASE_DIR, topk=5)
        chosen_item = None
        if candidates:
            for sc, it in candidates:
                if safe_lower(it.get("intent")) == action:
                    chosen_item = (sc, it)
                    break
            if not chosen_item:
                chosen_item = candidates[0]

        if not chosen_item:
            details = {"received": args, "hint": "add to knowledge/playbook_index.yaml or supply playbook field"}
            err = _err_payload("no_plan", f"no playbook matched for action='{action}'", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "no_plan", "details": details}})
            return err

        score, item = chosen_item
        default_vars = item.get("default_vars", {}) if isinstance(item, dict) else {}
        plan = {
            "playbook": item.get("playbook"),
            "extra_vars": {**default_vars, **{k: v for k, v in extra_vars.items() if k != "action"}},
            "score": round(float(score), 4),
            "intent": item.get("intent"),
        }
        plan["extra_vars"].setdefault("host", host)

        tops = [{"intent": it.get("intent"),
                 "playbook": it.get("playbook"),
                 "score": round(float(sc), 4)} for sc, it in candidates]

        result = {
            "summary": f"Selected {Path(plan['playbook']).name if plan.get('playbook') else '(none)'}",
            "plan": plan,
            "candidates": tops,
        }
        resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": result["summary"], "plan": plan, "candidates": tops, "mode": "search"})
        return JSONResponse(resp, status_code=200)

    # 2-ab) Playbook catalog (list/info)
    if name == "ansible.playbook_catalog":
        action = safe_lower(args.get("action")) if isinstance(args, dict) else ""
        category = args.get("category") if isinstance(args, dict) else None
        # Load index
        index = load_playbook_index(BASE_DIR)
        # Normalize helper
        def norm_entry(it: Dict[str, Any]) -> Dict[str, Any]:
            out = {
                "playbook": it.get("playbook"),
                "intent": it.get("intent"),
                "description": it.get("description"),
            }
            # Optional fields if present in index
            for k in ("title", "target_category", "required_capabilities", "optional_capabilities", "tags"):
                if it.get(k) is not None:
                    out[k] = it.get(k)
            return out
        # list
        if action in ("list", "ls", "all", "*"):
            items = []
            for it in (index or []):
                if category and it.get("target_category") and it.get("target_category") != category:
                    continue
                items.append(norm_entry(it))
            result = {"playbooks": items, "count": len(items)}
            resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": f"catalog list ({len(items)})", "category": category})
            return JSONResponse(resp, status_code=200)
        # info
        if action in ("info", "show", "get"):
            key = None
            if isinstance(args, dict):
                key = args.get("name") or args.get("id") or args.get("playbook")
            key_l = safe_lower(key)
            entry = None
            if key_l:
                for it in (index or []):
                    intent_l = safe_lower(it.get("intent"))
                    pb = it.get("playbook")
                    stem_l = safe_lower(Path(pb).stem if isinstance(pb, str) else None)
                    if key_l in (intent_l, stem_l) or (isinstance(pb, str) and key_l == safe_lower(pb)):
                        entry = it
                        break
            if not entry:
                details = {"received": args}
                err = _err_payload("not_found", f"no such playbook: {key}", details=details, status=404)
                _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 404, "error": {"code": "not_found", "key": key}})
                return err
            result = {"playbook": norm_entry(entry)}
            resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": f"catalog info {key}"})
            return JSONResponse(resp, status_code=200)

    # 2-b) Playbook listing
    if name == "ansible.playbooks.list":
        q = safe_lower(args.get("q")) if isinstance(args, dict) else ""
        include_fs = bool(args.get("include_fs")) if isinstance(args, dict) else False

        index = load_playbook_index(BASE_DIR)
        items = []
        for it in (index or []):
            intent = it.get("intent")
            desc = it.get("description", "")
            pb = it.get("playbook")
            text = " ".join([safe_lower(intent), safe_lower(desc), safe_lower(pb)])
            if not q or (q in text):
                items.append({
                    "source": "index",
                    "intent": intent,
                    "playbook": pb,
                    "description": desc,
                    "default_vars": it.get("default_vars", {}),
                })
        if include_fs:
            pb_dir = (BASE_DIR / "playbooks").resolve()
            if pb_dir.exists() and pb_dir.is_dir():
                for p in sorted(list(pb_dir.rglob("*.yml")) + list(pb_dir.rglob("*.yaml"))):
                    rel = str(p.relative_to(BASE_DIR))
                    if not any(x.get("playbook") == rel for x in items):
                        t = safe_lower(rel)
                        if not q or (q in t):
                            items.append({
                                "source": "fs",
                                "intent": None,
                                "playbook": rel,
                                "description": "(no index entry)",
                                "default_vars": {},
                            })
        result = {"items": items, "count": len(items)}
        resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": f"playbooks list ({len(items)})", "filtered": bool(q), "include_fs": include_fs})
        return JSONResponse(resp, status_code=200)

    # 3) Inventory listing
    if name == "ansible.inventory":
        inv_path = str(Path(os.getenv("ANSIBLE_INVENTORY", "/app/inventory.ini")).resolve())
        cmd = ["ansible-inventory", "-i", inv_path, "--list"]
        try:
            _mcp_log(_REQ_ID, 8, "ansible-mcp inventory request", {"cmd": cmd})
            out = subprocess.check_output(cmd, text=True)
            data = json.loads(out)
            result = {"inventory": data, "inventory_path": inv_path}
            resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": "inventory list", "hosts": list(data.get("_meta", {}).get("hostvars", {}).keys())})
            return JSONResponse(resp, status_code=200)
        except subprocess.CalledProcessError as e:
            details = {"rc": e.returncode, "stderr": getattr(e, 'stderr', None), "cmd": cmd}
            err = _err_payload("inventory_failed", f"ansible-inventory failed (rc={e.returncode})", details=details, status=500)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 500, "error": details})
            return err
        except Exception as e:
            details = {"error": str(e), "cmd": cmd}
            err = _err_payload("inventory_error", "failed to obtain inventory", details=details, status=500)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 500, "error": details})
            return err

    # 4) Run playbook (standard)
    if name in ("playbook.run", "ansible.playbook"):
        identifier = args.get("path") or args.get("playbook")
        if not identifier:
            return _json_error("bad_arguments", "missing 'path'/'playbook' for playbook execution", status=400)
        pb_path = _resolve_playbook_path(identifier)
        if not pb_path:
            details = {"playbook": identifier}
            err_resp = _err_payload("unknown_tool", f"playbook not found or unsupported: {identifier}", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found or unsupported: {identifier}", "details": details}})
            return err_resp
        extra_vars = _collect_extra_vars(args if isinstance(args, dict) else {})
        reply = _run_ansible(str(pb_path), extra_vars)
        host = extra_vars.get("host")
        feature = extra_vars.get("feature")
        if host and feature:
            summary = f"ホスト「{host}」の {feature} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
        else:
            summary = f"{pb_path.name} 実行（mode={reply['mode']}）"
        result = {
            "summary": summary,
            "ansible": {
                "rc": reply["rc"],
                "ok": reply["ok"],
                "mode": reply.get("mode"),
                "mode_reason": reply.get("mode_reason"),
                "stdout": reply.get("stdout", ""),
                "stderr": reply.get("stderr", ""),
            },
        }
        resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"], "playbook": str(pb_path)})
        return JSONResponse(resp, status_code=200)

    # 5) Direct playbook path (compat)
    if isinstance(name, str) and name.startswith("playbooks/") and (name.endswith(".yml") or name.endswith(".yaml")):
        pb_path = Path(name)
        if not pb_path.is_absolute():
            pb_path = (BASE_DIR / pb_path).resolve()
        if not pb_path.exists():
            details = {"path": str(pb_path)}
            err_resp = _err_payload("unknown_tool", f"playbook not found: {pb_path}", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found: {pb_path}", "details": details}})
            return err_resp
        extra_vars = dict(args) if isinstance(args, dict) else {}
        reply = _run_ansible(str(pb_path), extra_vars)
        host = extra_vars.get("host")
        feature = extra_vars.get("feature")
        if host and feature:
            summary = f"ホスト「{host}」の {feature} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
        else:
            summary = f"{pb_path.name} 実行（mode={reply['mode']}）"
        result = {
            "summary": summary,
            "ansible": {
                "rc": reply["rc"],
                "ok": reply["ok"],
                "mode": reply.get("mode"),
                "mode_reason": reply.get("mode_reason"),
                "stdout": reply.get("stdout", ""),
                "stderr": reply.get("stderr", ""),
            },
        }
        resp = {"ok": True, "id": rid, "ts_jst": _now_jst(), "result": result}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
        return JSONResponse(resp, status_code=200)

    # 6) Unsupported
    return _json_error("unsupported_tool", f"unsupported tool: {name}", status=400)

# -------- /tools/call endpoint --------

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
    inv = str(Path(os.getenv("ANSIBLE_INVENTORY", "/app/inventory.ini")).resolve())
    cmd = [ANSIBLE_BIN, playbook, "-i", inv, "-e", f"@{ev}"]
    _mcp_log(_REQ_ID, 8, "ansible-mcp ansible request", {"cmd": cmd})
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    _mcp_log(_REQ_ID, 9, "ansible-mcp ansible reply", {"rc": p.returncode, "stdout": out[:4000], "stderr": err[:2000]})
    return {
        "ok": p.returncode == 0, "mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON,
        "ansible_bin": ANSIBLE_BIN, "playbook": playbook, "vars": extra_vars,
        "stdout": out, "stderr": err, "rc": p.returncode
    }

# --- MCP endpoint: client-style payload compat ---

@app.post("/mcp")
async def mcp(request: Request):
    # Auth guard
    ok, resp = await _auth(request)
    if not ok:
        return resp

    # Parse body & set correlation id
    body = await request.json()
    global _REQ_ID
    try:
        _REQ_ID = _coerce_req_id_from(request, body)
    except Exception:
        _REQ_ID = None

    # Normalize input: prefer {tool, vars, origin_text}; allow {name, arguments}
    tool = body.get("tool") or body.get("name")
    vars_ = body.get("vars") or body.get("arguments") or {}
    if not isinstance(vars_, dict):
        vars_ = {"value": vars_}
    origin_text = body.get("origin_text") or ""

    _mcp_log(_REQ_ID, 6, "ansible-mcp request", {"body": {"tool": tool, "vars": vars_, "origin_text": origin_text}})

    # 1) Lightweight tool: echo (no Ansible)
    if tool == "mcp.test.echo":
        msg = vars_.get("text") if isinstance(vars_, dict) else ""
        resp = {"ok": True, "text": msg or "(empty)", "ts_jst": _now_jst()}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": "echo"})
        return JSONResponse(resp, status_code=200)

    # 2-a) Select playbook (planner, RAG-backed) — return plan only, do not execute
    if tool == "ansible.select_playbook":
        extra_vars = dict(vars_) if isinstance(vars_, dict) else {}
        action = safe_lower(extra_vars.get("action")) if extra_vars else ""
        host = (extra_vars.get("host") if isinstance(extra_vars.get("host"), str) else None) or "r1"

        explicit_playbook = extra_vars.get("playbook")
        if isinstance(explicit_playbook, str) and explicit_playbook.strip():
            playbook_path = explicit_playbook.strip()
            cleaned_vars = {k: v for k, v in extra_vars.items() if k != "action"}
            cleaned_vars.setdefault("playbook", playbook_path)
            cleaned_vars.setdefault("host", host)

            plan = {
                "playbook": playbook_path,
                "extra_vars": cleaned_vars,
                "score": 1.0,
                "intent": extra_vars.get("intent") or action or None,
            }

            resp = {
                "ok": True,
                "summary": f"Selected {Path(playbook_path).name}",
                "plan": plan,
                "candidates": [
                    {
                        "intent": extra_vars.get("intent") or action or None,
                        "playbook": playbook_path,
                        "score": 1.0,
                    }
                ],
                "ts_jst": _now_jst(),
            }
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(
                _REQ_ID,
                11,
                "ansible-mcp reply",
                {"status": 200, "summary": resp["summary"], "plan": plan},
            )
            return JSONResponse(resp, status_code=200)

        candidates = search_playbook(action, BASE_DIR, topk=5)
        chosen_item = None
        if candidates:
            for sc, it in candidates:
                if safe_lower(it.get("intent")) == action:
                    chosen_item = (sc, it)
                    break
            if not chosen_item:
                chosen_item = candidates[0]

        if not chosen_item:
            details = {"received": vars_, "hint": "add to knowledge/playbook_index.yaml"}
            err = _err_payload("no_plan", f"no playbook matched for action='{action}'", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "no_plan", "details": details}})
            return err

        score, item = chosen_item
        default_vars = item.get("default_vars", {}) if isinstance(item, dict) else {}
        plan = {
            "playbook": item.get("playbook"),
            "extra_vars": {**default_vars, **{k: v for k, v in extra_vars.items() if k != "action"}},
            "score": round(float(score), 4),
            "intent": item.get("intent"),
        }
        plan["extra_vars"].setdefault("host", host)

        tops = [{"intent": it.get("intent"),
                 "playbook": it.get("playbook"),
                 "score": round(float(sc), 4)} for sc, it in candidates]

        resp = {
            "ok": True,
            "summary": f"Selected {Path(plan['playbook']).name if plan.get('playbook') else '(none)'}",
            "plan": plan,
            "candidates": tops,
            "ts_jst": _now_jst(),
        }
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": resp["summary"], "plan": plan, "candidates": tops})
        return JSONResponse(resp, status_code=200)


    # 2-b) Playbook listing (from knowledge index, optional FS scan)
    if tool == "ansible.playbooks.list":
        q = safe_lower(vars_.get("q")) if isinstance(vars_, dict) else ""
        include_fs = bool(vars_.get("include_fs")) if isinstance(vars_, dict) else False
        # Load from knowledge index
        index = load_playbook_index(BASE_DIR)
        items = []
        for it in (index or []):
            intent = it.get("intent")
            desc = it.get("description", "")
            pb = it.get("playbook")
            text = " ".join([safe_lower(intent), safe_lower(desc), safe_lower(pb)])
            if not q or (q in text):
                items.append({
                    "source": "index",
                    "intent": intent,
                    "playbook": pb,
                    "description": desc,
                    "default_vars": it.get("default_vars", {}),
                })

        # Optionally include playbooks discovered on filesystem
        if include_fs:
            pb_dir = (BASE_DIR / "playbooks").resolve()
            if pb_dir.exists() and pb_dir.is_dir():
                for p in sorted(list(pb_dir.rglob("*.yml")) + list(pb_dir.rglob("*.yaml"))):
                    rel = str(p.relative_to(BASE_DIR))
                    # If not already in index list, append as filesystem item
                    if not any(x.get("playbook") == rel for x in items):
                        t = safe_lower(rel)
                        if not q or (q in t):
                            items.append({
                                "source": "fs",
                                "intent": None,
                                "playbook": rel,
                                "description": "(no index entry)",
                                "default_vars": {},
                            })
        resp = {
            "ok": True,
            "count": len(items),
            "items": items,
            "ts_jst": _now_jst(),
        }
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": f"playbooks list ({len(items)})", "filtered": bool(q), "include_fs": include_fs})
        return JSONResponse(resp, status_code=200)

    # 2-b') Playbook catalog (list/info)
    if tool == "ansible.playbook_catalog":
        action = safe_lower(vars_.get("action")) if isinstance(vars_, dict) else ""
        category = vars_.get("category") if isinstance(vars_, dict) else None
        index = load_playbook_index(BASE_DIR)

        def norm_entry(it: Dict[str, Any]) -> Dict[str, Any]:
            out = {
                "playbook": it.get("playbook"),
                "intent": it.get("intent"),
                "description": it.get("description"),
            }
            for k in ("title", "target_category", "required_capabilities", "optional_capabilities", "tags"):
                if it.get(k) is not None:
                    out[k] = it.get(k)
            return out

        if action in ("list", "ls", "all", "*"):
            items = []
            for it in (index or []):
                if category and it.get("target_category") and it.get("target_category") != category:
                    continue
                items.append(norm_entry(it))
            resp = {"ok": True, "playbooks": items, "count": len(items), "ts_jst": _now_jst()}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": f"catalog list ({len(items)})", "category": category})
            return JSONResponse(resp, status_code=200)

        if action in ("info", "show", "get"):
            key = None
            if isinstance(vars_, dict):
                key = vars_.get("name") or vars_.get("id") or vars_.get("playbook")
            key_l = safe_lower(key)
            entry = None
            if key_l:
                for it in (index or []):
                    intent_l = safe_lower(it.get("intent"))
                    pb = it.get("playbook")
                    stem_l = safe_lower(Path(pb).stem if isinstance(pb, str) else None)
                    if key_l in (intent_l, stem_l) or (isinstance(pb, str) and key_l == safe_lower(pb)):
                        entry = it
                        break
            if not entry:
                details = {"received": vars_}
                err = _err_payload("not_found", f"no such playbook: {key}", details=details, status=404)
                _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 404, "error": {"code": "not_found", "key": key}})
                return err
            resp = {"ok": True, "playbook": norm_entry(entry), "ts_jst": _now_jst()}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": f"catalog info {key}"})
            return JSONResponse(resp, status_code=200)

    # 3) Inventory listing (ansible-inventory --list)
    if tool == "ansible.inventory":
        inv_path = str(Path(os.getenv("ANSIBLE_INVENTORY", "/app/inventory.ini")).resolve())
        cmd = ["ansible-inventory", "-i", inv_path, "--list"]
        try:
            _mcp_log(_REQ_ID, 8, "ansible-mcp inventory request", {"cmd": cmd})
            out = subprocess.check_output(cmd, text=True)
            data = json.loads(out)
            resp = {"ok": True, "inventory": data, "inventory_path": inv_path, "ts_jst": _now_jst()}
            if _REQ_ID:
                resp["request_id"] = _REQ_ID
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": "inventory list", "hosts": list(data.get("_meta", {}).get("hostvars", {}).keys())})
            return JSONResponse(resp, status_code=200)
        except subprocess.CalledProcessError as e:
            details = {"rc": e.returncode, "stderr": getattr(e, 'stderr', None), "cmd": cmd}
            err = _err_payload("inventory_failed", f"ansible-inventory failed (rc={e.returncode})", details=details, status=500)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 500, "error": details})
            return err
        except Exception as e:
            details = {"error": str(e), "cmd": cmd}
            err = _err_payload("inventory_error", "failed to obtain inventory", details=details, status=500)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 500, "error": details})
            return err

    # 2) Explicit playbook execution via playbook.run / ansible.playbook
    if tool in ("playbook.run", "ansible.playbook"):
        identifier = vars_.get("path") or vars_.get("playbook")
        if not identifier:
            details = {"vars": vars_}
            err_resp = _err_payload("bad_arguments", "missing 'path'/'playbook' for playbook execution", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "bad_arguments", "details": details}})
            return err_resp

        pb_path = _resolve_playbook_path(identifier)
        if not pb_path:
            details = {"playbook": identifier}
            err_resp = _err_payload("unknown_tool", f"playbook not found or unsupported: {identifier}", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found or unsupported: {identifier}", "details": details}})
            return err_resp

        extra_vars = _collect_extra_vars(vars_ if isinstance(vars_, dict) else {})
        reply = _run_ansible(str(pb_path), extra_vars)
        host = extra_vars.get("host")
        feature = extra_vars.get("feature")
        if host and feature:
            summary = f"ホスト「{host}」の {feature} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
        else:
            summary = f"{pb_path.name} 実行（mode={reply['mode']}）"

        resp = {
            "ok": True,
            "summary": summary,
            "ansible": {
                "rc": reply["rc"],
                "ok": reply["ok"],
                "mode": reply.get("mode"),
                "mode_reason": reply.get("mode_reason"),
                "stdout": reply.get("stdout", ""),
                "stderr": reply.get("stderr", ""),
            },
            "ts_jst": _now_jst(),
        }
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        resp["debug"] = {"request": {"tool": tool, "vars": extra_vars}, "ansible": reply}
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"], "playbook": str(pb_path)})
        return JSONResponse(resp, status_code=200)

    # 2) Explicit playbook execution only (no planning)
    if isinstance(tool, str) and tool.startswith("playbooks/") and (tool.endswith(".yml") or tool.endswith(".yaml")):
        pb_path = Path(tool)
        if not pb_path.is_absolute():
            pb_path = (BASE_DIR / pb_path).resolve()
        if not pb_path.exists():
            details = {"path": str(pb_path)}
            err_resp = _err_payload("unknown_tool", f"playbook not found: {pb_path}", details=details, status=400)
            _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found: {pb_path}", "details": details}})
            return err_resp

        # Use provided vars as-is
        extra_vars = dict(vars_) if isinstance(vars_, dict) else {}
        reply = _run_ansible(str(pb_path), extra_vars)

        # Compose summary (host/feature are optional)
        host = extra_vars.get("host")
        feature = extra_vars.get("feature")
        if host and feature:
            summary = f"ホスト「{host}」の {feature} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
        else:
            summary = f"{pb_path.name} 実行（mode={reply['mode']}）"

        dbg = {"request": {"tool": tool, "vars": extra_vars}, "ansible": reply}
        resp = {"ok": True, "summary": summary, "ansible": {"rc": reply["rc"], "ok": reply["ok"]}, "ts_jst": _now_jst(), "debug": dbg}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
        return JSONResponse(resp, status_code=200)

    # 4) Unsupported tool (no implicit planning)
    details = {"tool": tool}
    err_resp = _err_payload("unsupported_tool", f"unsupported tool for /mcp: {tool}", details=details, status=400)
    _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "unsupported_tool", "message": f"unsupported tool for /mcp: {tool}", "details": details}})
    return err_resp

@app.post("/run")
async def run(request: Request):
    ok, resp = await _auth(request)
    if not ok:
        return resp

    body = await request.json()
    # capture correlation id (accept JSON id/request_id or X-Request-Id header)
    global _REQ_ID
    try:
        _REQ_ID = _coerce_req_id_from(request, body)
    except Exception:
        _REQ_ID = None
    _mcp_log(_REQ_ID, 6, "ansible-mcp request", {"body": body})

    text = body.get("text", "")
    decision = body.get("decision", "run")
    score = body.get("score", 1)
    payload = (body.get("payload") or {})
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else []
    intent = body.get("intent") or "run"

    plan = _plan_from_text(text)
    _mcp_log(_REQ_ID, 7, "ansible-mcp gpt input", {"prompt": text, "decision": decision, "score": score, "plan": plan})
    explicit_pb: Optional[str] = payload.get("playbook") if isinstance(payload, dict) else None
    chosen_pb = explicit_pb or (candidates[0] if candidates else None) or plan.get("playbook")

    pb_path = Path(chosen_pb)
    if not pb_path.is_absolute():
        pb_path = (BASE_DIR / pb_path).resolve()
    if not pb_path.exists():
        details = {"path": str(pb_path)}
        err_resp = _err_payload("unknown_tool", f"playbook not found: {pb_path}", details=details, status=400)
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 400, "error": {"code": "unknown_tool", "message": f"playbook not found: {pb_path}", "details": details}})
        return err_resp

    extra_vars = {"host": plan["host"], "feature": plan["feature"], "score": score, "decision": decision}
    user_vars = payload.get("vars") if isinstance(payload, dict) else None
    if isinstance(user_vars, dict):
        extra_vars.update(user_vars)

    # If propose_create intent is sent, do not run Ansible; return proposal
    if intent == "propose_create":
        dbg = {
            "propose_new_playbook": {
                "feature": plan["feature"],
                "suggested_path": str(pb_path),
                "vars_suggest": extra_vars,
                "template_hint": "/app/knowledge/templates/playbook.new.yml.j2",
            }
        }
        summary = f"Playbook 提案: {pb_path.name}（feature={plan['feature']}）"
        resp = {"ok": True, "decision": decision, "summary": summary, "score": score,
                "ansible": {"rc": 0, "ok": True}, "ts_jst": _now_jst(), "debug": dbg}
        if _REQ_ID:
            resp["request_id"] = _REQ_ID
        _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": summary, "intent": "propose_create"})
        return resp

    reply = _run_ansible(str(pb_path), extra_vars)
    debug = {
        "no7_plan": {"prompt": text, "decision": decision, "score": score, "plan": plan},
        "no8_request": {"module": "ansible", "payload": {"playbook": str(pb_path), "vars": extra_vars},
                        "effective_mode": EFFECTIVE_MODE, "mode_reason": MODE_REASON, "ansible_bin": ANSIBLE_BIN},
        "no9_reply": reply,
    }
    summary = f"ホスト「{extra_vars.get('host')}」の {extra_vars.get('feature')} を {pb_path.name} で確認しました（mode={reply['mode']}）。"
    debug["no10_output"] = {"summary": summary}
    resp = {"ok": True, "decision": decision, "summary": summary, "score": score,
            "ansible": {"rc": reply["rc"], "ok": reply["ok"]}, "ts_jst": _now_jst(), "debug": debug}
    if _REQ_ID:
        resp["request_id"] = _REQ_ID
    _mcp_log(_REQ_ID, 10, "ansible-mcp gpt output", {"summary": summary})
    _mcp_log(_REQ_ID, 11, "ansible-mcp reply", {"status": 200, "summary": summary, "ansible_rc": reply["rc"]})
    return resp

# --- Utility for /tools/call error replies ---
def _json_error(code, msg, status=400):
    body = {"ok": False, "error": {"code": code, "message": msg}}
    if _REQ_ID:
        body["request_id"] = _REQ_ID
    return JSONResponse(body, status_code=status)
