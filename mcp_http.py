# latest mcp_http.py content placeholder


# ===== SAFE PATCH BEGIN (idempotent) =====
try:
    app
except NameError:
    try:
        from fastapi import FastAPI
        app = FastAPI(title="MCP Service")
    except Exception as _e:
        from fastapi import FastAPI
        app = FastAPI(title="MCP Service")

# Reuse APP if present
try:
    if 'APP' in globals() and 'app' not in globals():
        app = APP
except Exception:
    pass

def __mcp_safe_register_routes(_app):
    from fastapi import APIRouter
    r = APIRouter()

    @r.get("/health")
    async def _health():
        return {"ok": True}

    @r.get("/")
    async def _root():
        return {"ok": True, "service": "mcp"}

    _app.include_router(r)

try:
    __mcp_safe_register_routes(app)
except Exception as _e:
    from fastapi import FastAPI
    app = FastAPI(title="MCP Service")
    __mcp_safe_register_routes(app)
# ===== SAFE PATCH END =====

# ===== SAFE PATCH BEGIN =====
try:
    app  # reuse if it exists
except NameError:
    from fastapi import FastAPI
    app = FastAPI(title="MCP Service")

def __mcp_safe_register_routes(_app):
    from fastapi import APIRouter
    r = APIRouter()
    @r.get("/health")
    async def _health(): return {"ok": True}
    @r.get("/")
    async def _root(): return {"ok": True, "service": "mcp"}
    _app.include_router(r)

try:
    __mcp_safe_register_routes(app)
except Exception:
    from fastapi import FastAPI
    app = FastAPI(title="MCP Service")
    __mcp_safe_register_routes(app)
# ===== SAFE PATCH END =====
# ===== MCP SAFE MINIMAL PATCH (idempotent, non-invasive) =====
# This block only ensures that:
#  - a FastAPI `app` object exists (reusing APP/application if present)
#  - GET /health and GET / routes exist (created only if missing)
# Nothing else is modified.
try:
    from fastapi import FastAPI
except Exception:  # FastAPI not installed or import error
    FastAPI = None  # type: ignore

def _ensure_app():
    glb = globals()
    # Prefer an existing object if present
    for name in ("app", "APP", "application"):
        if name in glb and glb[name] is not None:
            return glb.get("app") or glb[name]
    # Create only if FastAPI is available
    if FastAPI is not None:
        return FastAPI(title="MCP Service")
    # As a last resort, defer (caller must provide app)
    return None

app = _ensure_app()  # type: ignore

def _has_route(_app, path:str, method:str="GET")->bool:
    try:
        for r in getattr(_app, "routes", []):
            if getattr(r, "path", None) == path and method.upper() in getattr(r, "methods", set()):
                return True
    except Exception:
        pass
    return False

# Only register when we actually have an app and the route is missing
if app is not None:
    if not _has_route(app, "/health", "GET"):
        @app.get("/health")
        async def __mcp_health():
            return {"ok": True}
    if not _has_route(app, "/", "GET"):
        @app.get("/")
        async def __mcp_root():
            return {"ok": True, "service": "mcp"}
# ===== END MCP SAFE MINIMAL PATCH =====

