"""CADIO web API — the product surface.

Everything is project-scoped. Runs, references, and conversation all belong to
a project and persist in SQLite (~/.cadio/cadio.db); files live under
~/.cadio/projects/<pid>/. Conversations resume across reload and restart
because the orchestrator's memory is rebuilt from stored messages.

REST:
  GET  /api/config                         default model + provider keys present
  GET  /api/example                        starter program
  GET/POST   /api/projects                 list / create projects
  GET/PATCH  /api/projects/{pid}           summary / rename / archive
  GET  /api/projects/{pid}/runs            saved versions (newest first)
  GET  /api/projects/{pid}/assets          reference images
  POST /api/projects/{pid}/assets          upload a reference image
  GET  /api/projects/{pid}/history         chat scrollback (ChatItem shapes)
  POST /api/projects/{pid}/execute         run + save a version (no LLM)
  POST /api/preview                        fast throwaway run (no LLM, no save)
  POST /api/providers/models               list a key's models (key-test)
  GET  /files/{pid}/...                     run artifacts + references
  GET  /previews/...                        ephemeral preview STLs
  GET  /                                    the React app

WebSocket /ws/chat?project=<pid> — the agent loop for one project:
  client -> {"type":"init","model":...,"api_key":...}   configure (key in
            connection memory only; never persisted/logged) -> {"type":"ready"}
  client -> {"type":"chat","text":...,"assets":[id,...]} user message
  client -> {"type":"answers","answers":[...]}           reply to ask_user
  client -> {"type":"stop"}                              cancel the turn
  server -> status | ask_user | run | assistant | error
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .. import affect, config, select, versions
from ..agent.orchestrator import DEFAULT_MODEL, Orchestrator
from ..engines.precision import PrecisionEngine
from ..storage import store as object_store
from ..store import Store
from . import auth, history, limits
from .auth import get_current_user, require_project
from .limits import rate_limit
from .providers import ProviderError, list_models

ROOT = config.REPO_ROOT
FRONTEND_DIST = ROOT / "frontend" / "dist"

ASSET_MAX_BYTES = 40 * 1024 * 1024
# resume windowing: replay at most this many conversation turns into the agent's
# memory (generous — normal chats are untouched; only pathologically long ones cap).
HISTORY_MAX_TURNS = int(os.environ.get("CADIO_HISTORY_MAX_TURNS", "40"))
IMAGE_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
# reference geometry: agent measures these with inspect_geometry (not shown as images)
GEOMETRY_EXTS = {".step": "model/step", ".stp": "model/step", ".stl": "model/stl"}

# live chat sockets (for a clean "going away" on shutdown).
_active_ws: set[WebSocket] = set()
SHUTDOWN_DRAIN_S = 15.0  # max we wait for in-flight builds before killing workers

# Per-PROJECT agent sessions. An agent turn (and its build slot) belongs to the
# project, not the websocket that started it — so a refresh/reconnect re-attaches
# to the same session and the in-flight build keeps going (see ProjectSession).
_sessions: dict[str, "ProjectSession"] = {}
_sessions_lock = threading.Lock()


def _inflight_build_count() -> int:
    return sum(1 for s in _sessions.values() if s.busy)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # startup: nothing to pre-warm (the pool spins up lazily on first build)
    yield
    # shutdown (SIGTERM / reload / deploy): finish what we can, then leave cleanly.
    # 1) let in-flight builds finish so users don't lose an active turn (bounded).
    deadline = time.monotonic() + SHUTDOWN_DRAIN_S
    while _inflight_build_count() > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    # 2) tell each connected client we're going away and close with 1001 (not the
    #    abrupt 1012), so the browser reconnects gracefully instead of erroring.
    for ws in list(_active_ws):
        try:
            await ws.send_json({"type": "status", "state": "idle"})
            await ws.close(code=1001)
        except Exception:
            pass
    # 3) kill the warm CAD workers so a reload/restart/deploy never orphans the
    #    OCCT subprocesses.
    engine.shutdown()


app = FastAPI(title="CADIO", version="0.1.0", lifespan=_lifespan)
engine = PrecisionEngine()
store = Store()
# belt-and-suspenders for hard interpreter exits that skip the lifespan shutdown
atexit.register(engine.shutdown)

# --- auth: signed session cookie + Google OAuth (see auth.py) ---------------
# SessionMiddleware covers http + websocket scopes, so ws_chat authenticates by
# reading ws.session. Cookie is SameSite=Lax (same-origin behind nginx today).
auth.configure(store)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    session_cookie="cadio_session",
    max_age=30 * 24 * 3600,
    # cross-origin frontend => cookie must be SameSite=None; Secure to ride on
    # its fetch/websocket calls; single-origin stays Lax.
    same_site=auth.cookie_same_site(),
    https_only=auth.cookie_secure(),
)
app.include_router(auth.router)
# compress JSON responses — the pick map (face_ids.json is ~1 MB of ints on a
# detailed model), run/history lists, etc. gzip ~10x on text; binary meshes barely
# compress but are cache-immutable (below) so each downloads at most once. Range
# requests aren't used by the viewer (STLLoader/GLB fetch whole files).
app.add_middleware(GZipMiddleware, minimum_size=1024)
# reject oversized bodies early (added last → outermost → runs before route work)
app.add_middleware(limits.BodyLimitMiddleware)
# CORS last so it's the OUTERMOST layer (handles preflight before anything else).
# Only enabled when the frontend is on its own origin; allow_credentials so the
# session cookie is accepted cross-site (requires explicit origins, not "*").
if config.CADIO_FRONTEND_URLS:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CADIO_FRONTEND_URLS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _mem_stats() -> dict:
    """Container memory ceiling + current usage, read straight from the cgroup so
    we can tell (from outside the cluster) whether builds are OOM-killing the pod.
    Values in MB. Best-effort: returns {} if the files aren't present."""
    def _read_int(path: str) -> int | None:
        try:
            v = open(path).read().strip()
            return int(v) if v.isdigit() else None
        except Exception:
            return None

    mb = lambda b: round(b / 1048576) if b is not None else None
    # cgroup v2 first, then v1
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    usage = _read_int("/sys/fs/cgroup/memory.current") or _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    out: dict = {"limit_mb": mb(limit), "usage_mb": mb(usage)}
    # peak RSS of this process (ru_maxrss is KB on Linux)
    try:
        import resource
        out["rss_peak_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    except Exception:
        pass
    if out.get("limit_mb") and out.get("usage_mb"):
        out["used_pct"] = round(100 * out["usage_mb"] / out["limit_mb"])
    return out


@app.get("/healthz")
def healthz():
    """Liveness probe for supervisors / load balancers — unauthenticated, no DB
    or engine work, so it stays green even under load and never leaks state."""
    return {"ok": True, "connections": len(_active_ws), "inflight_builds": _inflight_build_count(),
            "mem": _mem_stats()}


# Developer-only diagnostics. These logs are for us (server console), never shown
# to users — the UI only ever gets generalized messages. uvicorn doesn't touch the
# root logger, so we give ours its own handler and keep it off root (no double
# lines). Set CADIO_LOG_LEVEL=DEBUG to trace every socket message when hunting a bug.
log = logging.getLogger("cadio.api")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] cadio: %(message)s", "%H:%M:%S"))
    log.addHandler(_handler)
    log.setLevel(os.environ.get("CADIO_LOG_LEVEL", "INFO").upper())
    log.propagate = False


# ---- run payload shaping --------------------------------------------------

def _run_payload(pid: str, run: dict) -> dict:
    """Client-facing run meta: engine facts + computed artifact URLs."""
    meta = dict(run["meta"])
    meta["run_id"] = run["run_id"]
    meta["label"] = run["label"]
    meta["created_at"] = run["created_at"]
    meta["parent_run_id"] = run.get("parent_run_id")
    if meta.get("ok"):
        base = f"/files/{pid}/runs/{run['run_id']}"
        meta["artifact_urls"] = {kind: config.public_url(f"{base}/{name}")
                                 for kind, name in meta.get("artifacts", {}).items()}
        meta["program_url"] = config.public_url(f"{base}/program.py")
    return meta


def _current_model_note(pid: str, session: "ProjectSession", run_id: str,
                        live_params: dict | None) -> str | None:
    """Agent-facing note anchoring the next edit to the version ON SCREEN when it
    differs from the agent's last build (a manual save, a Code-tab run, or an
    older version the user scrolled back to). None when they're already in sync —
    the agent built exactly this run, so its memory already holds the program.

    The program is embedded only when it TEXTUALLY differs from the agent's last
    build (so a param-only save stays lightweight); the params are always
    restated so a manual tweak is preserved instead of reset to the defaults."""
    if run_id == session.orch.last_run_id:
        return None
    run = store.get_run(pid, run_id)
    if not run or not run.get("ok"):
        return None
    prog_path = config.project_runs_dir(pid) / run_id / "program.py"
    program = prog_path.read_text() if prog_path.exists() else None
    include_program = bool(program) and program.strip() != (session.orch.last_program or "").strip()
    baseline = run["meta"].get("params") or {}
    params = live_params or baseline
    return versions.current_model_note(
        versions.version_name(run), run_id, params, program if include_program else None)


def _result_to_meta(run_id: str, result_dict: dict) -> dict:
    """Normalize an engine ExecutionResult dict for storage: keep facts, store
    artifacts as bare filenames (URLs are computed per-project at read time)."""
    meta = {k: result_dict.get(k) for k in
            ("ok", "params", "manifest", "bbox", "volume_mm3", "validation", "error", "duration_s")}
    meta["run_id"] = run_id
    meta["artifacts"] = {kind: Path(p).name for kind, p in result_dict.get("artifacts", {}).items()}
    return meta


def _save_run(pid: str, run_id: str, result_dict: dict, label: str,
              parent_run_id: str | None = None, origin: str = "agent") -> dict:
    meta = _result_to_meta(run_id, result_dict)
    run = store.add_run(pid, run_id, label, bool(meta.get("ok")), meta, parent_run_id,
                        origin=origin)
    # precompute the parameter→affected-faces map in the background so clicking a
    # parameter highlights instantly (see /affect endpoint for the lazy fallback)
    if meta.get("ok") and meta.get("manifest"):
        run_dir = config.project_runs_dir(pid) / run_id
        _ensure_precompute(run_dir, result_dict.get("params", {}), result_dict.get("manifest", []))
    # mirror the run's artifacts to R2 (durable copy; local disk stays a cache).
    # Best-effort and off the request path — inert when R2 is disabled.
    if meta.get("ok") and object_store.enabled:
        run_dir = config.project_runs_dir(pid) / run_id
        threading.Thread(
            target=lambda: object_store.put_dir(run_dir, f"{pid}/runs/{run_id}"),
            daemon=True,
        ).start()
    return _run_payload(pid, run)


# affect maps run the CAD engine once per parameter, so they must never happen on
# an HTTP request thread (that starved the shared worker pool and hung /affect —
# the source of the "socket hang up" disconnects). Instead we build them in the
# background, deduped per run, and gated by a semaphore so the interactive
# agent/preview always keeps a pool worker free. The gate allows up to
# (pool_size - 1) affect builds at once — so a bigger pool lets several users'
# affect builds proceed concurrently instead of queueing behind one global lock.
_affect_jobs: set[str] = set()          # run_dirs with a build queued or running
_affect_reg_lock = threading.Lock()     # guards _affect_jobs
_AFFECT_CONCURRENCY = int(os.environ.get(
    "CADIO_AFFECT_CONCURRENCY", str(max(1, engine._pool_size - 1))))
_affect_gate = threading.BoundedSemaphore(_AFFECT_CONCURRENCY)


def _ensure_precompute(run_dir: Path, params: dict, manifest: list) -> None:
    """Start a background affect build for this run unless one is already pending."""
    key = str(run_dir)
    with _affect_reg_lock:
        if key in _affect_jobs:
            return
        _affect_jobs.add(key)

    def job() -> None:
        try:
            with _affect_gate:  # cap concurrency so a live agent keeps a worker
                code = (run_dir / "program.py").read_text()
                affect.build_and_cache(engine, code, params, manifest, run_dir)
        except Exception:
            # best-effort; the client retries and the endpoint re-queues
            log.warning("affect build failed for %s", key, exc_info=True)
        finally:
            with _affect_reg_lock:
                _affect_jobs.discard(key)

    threading.Thread(target=job, daemon=True).start()


# ---- config / example -----------------------------------------------------

@app.get("/api/config")
def get_config():
    provider_keys = {
        p: bool(os.environ.get(f"{p.upper()}_API_KEY"))
        for p in ("anthropic", "openai", "gemini", "xai")
    }
    return {"default_model": DEFAULT_MODEL, "provider_keys": provider_keys}


@app.get("/api/example")
def example_program():
    return {"code": (ROOT / "examples" / "simple_box.py").read_text()}


# ---- projects -------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = "Untitled project"


class ProjectPatch(BaseModel):
    name: str | None = None
    archived: bool | None = None


@app.get("/api/projects")
def list_projects(user: dict = Depends(get_current_user)):
    return store.list_projects(user["id"])


@app.post("/api/projects")
def create_project(req: ProjectCreate, user: dict = Depends(rate_limit("create_project", 30, 86400))):
    return store.create_project(req.name, user["id"])


@app.get("/api/projects/{pid}")
def get_project(pid: str, user: dict = Depends(get_current_user)):
    return require_project(pid, user)


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, req: ProjectPatch, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    return store.update_project(pid, name=req.name, archived=req.archived)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    store.delete_project(pid)
    return {"ok": True}


@app.delete("/api/projects/{pid}/assets/{asset_id}")
def delete_asset(pid: str, asset_id: str, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    if not store.delete_asset(pid, asset_id):
        return JSONResponse({"error": "asset not found"}, status_code=404)
    return {"ok": True}


@app.get("/api/projects/{pid}/runs")
def project_runs(pid: str, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    return [_run_payload(pid, r) for r in store.list_runs(pid)]


@app.get("/api/projects/{pid}/runs/{run_id}/affect")
def run_affect(pid: str, run_id: str, user: dict = Depends(get_current_user)):
    """{param_name: [affected face index, ...]} for a run. Served from the cached
    affect.json; if it isn't built yet we kick off the background build and return
    202 so the client polls — the build never runs on this request thread (that
    starved the worker pool and hung the app; see _ensure_precompute)."""
    require_project(pid, user)
    run = store.get_run(pid, run_id)
    if not run:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_dir = config.project_runs_dir(pid) / run_id
    cached = affect.affect_path(run_dir)
    if cached.exists():
        try:
            return json.loads(cached.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    meta = run["meta"]
    if not meta.get("ok") or not (run_dir / "program.py").exists():
        return {}  # nothing to build — a genuinely empty map
    _ensure_precompute(run_dir, meta.get("params", {}), meta.get("manifest", []))
    return JSONResponse({}, status_code=202)  # building — client should retry


@app.get("/api/projects/{pid}/runs/{run_id}/facemap")
def run_facemap(pid: str, run_id: str, user: dict = Depends(get_current_user)):
    """The pick map the viewer needs for instant hover/selection, computed by the
    sandbox at build time: facet→face ids, per-face metadata (type/radius/name),
    and per-edge polylines. {} when a run predates the map or has none."""
    require_project(pid, user)
    run = store.get_run(pid, run_id)
    if not run:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_dir = config.project_runs_dir(pid) / run_id

    def _read(name: str, default):
        path = run_dir / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default

    return {
        "face_ids": _read("face_ids.json", []),
        "faces": _read("faces.json", []),
        "edges": _read("edges.json", []),
        "parts": _read("parts.json", []),
    }


class SelectRequest(BaseModel):
    face: int
    anchor: int | None = None  # BREP face id the viewer resolved the click to
    precise: bool = False  # Alt-click: describe the single BREP face, not its part


@app.post("/api/projects/{pid}/runs/{run_id}/select")
def run_select(pid: str, run_id: str, req: SelectRequest, user: dict = Depends(get_current_user)):
    """Resolve a clicked facet into a highlightable face region + a stable,
    agent-facing description of the part it belongs to (see cadio/select.py)."""
    require_project(pid, user)
    run = store.get_run(pid, run_id)
    if not run:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_dir = config.project_runs_dir(pid) / run_id
    desc = select.describe_selection(run_dir, req.face, anchor=req.anchor, precise=req.precise)
    if desc is None:
        return JSONResponse({"error": "could not resolve selection"}, status_code=422)
    desc["note"] = select.selection_note(desc)
    return desc


@app.get("/api/projects/{pid}/history")
def project_history(pid: str, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    records = store.get_messages(pid)
    runs = {r["run_id"]: _run_payload(pid, r) for r in store.list_runs(pid)}
    assets = {a["id"]: a for a in store.list_assets(pid)}
    return history.to_ui_items(records, runs.get, assets.get)


# ---- execution ------------------------------------------------------------

class ExecuteRequest(BaseModel):
    code: str
    params: dict | None = None
    label: str | None = None
    parent_run_id: str | None = None


class PreviewRequest(BaseModel):
    code: str
    params: dict | None = None


def _prune_previews(keep: int = 12) -> None:
    dirs = sorted(config.PREVIEW_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in dirs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


@app.post("/api/preview")
def preview(req: PreviewRequest, user: dict = Depends(rate_limit("preview", 120, 60))):
    """Fast, throwaway run for realtime slider tweaks: no STEP/GLB, no history."""
    pdir = config.PREVIEW_DIR / uuid.uuid4().hex[:12]
    result = engine.execute(req.code, req.params, pdir, preview=True)
    meta = result.to_dict()
    meta.pop("run_dir", None)
    meta["preview"] = True
    if result.ok:
        meta["artifact_urls"] = {
            kind: config.public_url(f"/previews/{pdir.name}/{Path(p).name}")
            for kind, p in result.artifacts.items()
        }
    _prune_previews()
    return JSONResponse(meta, status_code=200 if result.ok else 422)


@app.post("/api/projects/{pid}/execute")
def project_execute(pid: str, req: ExecuteRequest, user: dict = Depends(rate_limit("execute", 20, 60))):
    require_project(pid, user)
    if not limits.check_daily_quota(store, user["id"]):
        return JSONResponse({"error": "daily build quota reached — try again tomorrow"}, status_code=429)
    if not limits.acquire_build_slot(user["id"]):
        return JSONResponse({"error": "a build is already running — wait for it to finish"}, status_code=429)
    try:
        run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
        result = engine.execute(req.code, req.params, config.project_runs_dir(pid) / run_id)
        payload = _save_run(pid, run_id, result.to_dict(), req.label or "", req.parent_run_id,
                            origin="manual")
        # a manual save is a checkpoint in the conversation: record a UI-only
        # marker so it shows as a chip in the chat (live + on reload) the same
        # way an agent build shows a run card. Kept distinct from a "run" event
        # (kind=checkpoint) because its program is NOT in the agent's LLM memory —
        # the current-model note is what feeds it back to the agent, not replay.
        if result.ok:
            store.add_message(pid, "event", {"kind": "checkpoint", "run_id": run_id})
    finally:
        limits.release_build_slot(user["id"])
    return JSONResponse(payload, status_code=200 if result.ok else 422)


# ---- assets ---------------------------------------------------------------

@app.get("/api/projects/{pid}/assets")
def project_assets(pid: str, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    return store.list_assets(pid)


@app.post("/api/projects/{pid}/assets")
async def upload_asset(pid: str, file: UploadFile, user: dict = Depends(rate_limit("upload", 10, 60))):
    require_project(pid, user)
    mime = file.content_type or ""
    ext = Path(file.filename or "").suffix.lower()
    # images by mime; reference geometry by extension (STEP/STL mimes are unreliable)
    if mime in IMAGE_MIMES:
        store_ext, store_mime = IMAGE_MIMES[mime], mime
    elif ext in GEOMETRY_EXTS:
        store_ext, store_mime = ext, GEOMETRY_EXTS[ext]
    else:
        return JSONResponse(
            {"error": "unsupported file — upload an image (PNG/JPG/WEBP/GIF) or geometry (STEP/STL)"},
            status_code=400)
    data = await file.read()
    if len(data) > ASSET_MAX_BYTES:
        return JSONResponse({"error": "file larger than 40MB"}, status_code=400)
    asset_id = uuid.uuid4().hex[:12]
    fname = f"{asset_id}{store_ext}"
    config.project_refs_dir(pid).mkdir(parents=True, exist_ok=True)
    (config.project_refs_dir(pid) / fname).write_bytes(data)
    # reference uploads are NOT regenerable, so mirror them to R2 durably (best-
    # effort; the local copy stays as the cache). Served via the same /files
    # fallback as run artifacts.
    if object_store.enabled:
        threading.Thread(
            target=lambda: object_store.put(f"{pid}/refs/{fname}", data, store_mime),
            daemon=True,
        ).start()
    name = Path(file.filename or "file").name[:80]
    return store.add_asset(pid, asset_id, fname, name, store_mime)


# ---- providers ------------------------------------------------------------

class ProviderModelsRequest(BaseModel):
    provider: str
    api_key: str


@app.post("/api/providers/models")
def provider_models(req: ProviderModelsRequest, user: dict = Depends(rate_limit("models", 6, 60))):
    try:
        return {"models": list_models(req.provider, req.api_key)}
    except ProviderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ---- auth / health --------------------------------------------------------

@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """The signed-in user (401 if not) — the frontend's auth bootstrap. The
    auth_enabled flag lets the UI hide the sign-out when running without OAuth
    (local dev), where it would be a no-op."""
    return {**user, "auth_enabled": auth.auth_enabled()}


@app.get("/api/healthz")
def healthz():
    """Unauthenticated liveness probe (Docker healthcheck)."""
    return {"ok": True}


# ---- per-project agent session --------------------------------------------


class ProjectSession:
    """Owns one project's agent: its Orchestrator, in-flight turn, and build slot.

    The turn runs in a background thread owned by the SESSION, not by any socket,
    so a websocket refresh/reconnect just re-`attach()`es to the same session:
    the build keeps running, its live events go to whichever socket is currently
    attached, and the build slot is released when the TURN finishes. A message
    sent while a turn is running is correctly refused ('already running'), and the
    reconnecting client is shown the in-progress status instead of an empty chat.
    """

    def __init__(self, pid: str, uid: str):
        self.pid = pid
        self.uid = uid
        self.lock = threading.Lock()
        self.busy = False
        self.sink: Callable[[dict], None] | None = None  # current attached client
        self.answers_q: queue.Queue = queue.Queue()
        self.ask_pending = False
        self.pending_ask: list[dict] | None = None       # re-sent on reattach
        self.orch = Orchestrator(
            engine, config.project_runs_dir(pid),
            ask_user=self._ask_user, on_event=self._on_event, on_message=self._on_message,
            inspect_asset=self._inspect_asset, trace_asset=self._trace_asset,
        )
        records = store.get_messages(pid)
        if records:
            self.orch.set_history(
                history.to_llm_messages(records, self._asset_path, max_turns=HISTORY_MAX_TURNS))
            self._restore_agent_anchor(records)

    def _restore_agent_anchor(self, records: list[dict]) -> None:
        """After a reconnect the orchestrator is rebuilt from the DB with an empty
        last_run_id/last_program. Recover the id + program of the agent's LAST
        build (a 'run' event, never a manual 'checkpoint' — those aren't in the
        agent's memory) so the current-model note only fires when the on-screen
        version really differs from what the agent built."""
        for rec in reversed(records):
            if rec["role"] == "event" and rec["content"].get("kind") == "run":
                rid = rec["content"].get("run_id")
                if not rid:
                    return
                self.orch.last_run_id = rid
                prog = config.project_runs_dir(self.pid) / str(rid) / "program.py"
                if prog.exists():
                    self.orch.last_program = prog.read_text()
                return

    # --- client attach/detach (called from the event loop) ---
    def attach(self, sink: Callable[[dict], None]) -> tuple[bool, list[dict] | None]:
        """Make `sink` the live destination. Returns (busy, pending_ask) so the
        caller can await-send the in-progress status/question on the new socket."""
        with self.lock:
            self.sink = sink
            return self.busy, self.pending_ask

    def detach(self, sink: Callable[[dict], None]) -> None:
        with self.lock:
            if self.sink is sink:
                self.sink = None

    def deliver(self, payload: dict) -> None:
        """Best-effort live push to the attached socket (called from the turn
        thread). No client attached → dropped; the DB copy makes it recoverable."""
        sink = self.sink
        if sink is not None:
            sink(payload)

    # --- orchestrator callbacks (project-scoped; persist always, push if attached) ---
    def _on_message(self, role: str, record: dict) -> None:
        store.add_message(self.pid, role, record)

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "run":
            run_id = event["run_id"]
            payload = _save_run(self.pid, run_id, event["result"], event.get("label", ""))
            store.add_message(self.pid, "event", {"kind": "run", "run_id": run_id})
            self.deliver({"type": "run", "meta": payload})
        else:
            self.deliver(event)

    def _ask_user(self, questions: list[dict]) -> list[dict]:
        self.pending_ask = questions
        self.ask_pending = True
        self.deliver({"type": "ask_user", "questions": questions})
        try:
            answers = self.answers_q.get()
        finally:
            self.ask_pending = False
            self.pending_ask = None
        return answers if answers is not None else []

    def _asset_path(self, aid: str) -> Path | None:
        a = store.get_asset(self.pid, aid)
        if not a:
            return None
        p = config.project_refs_dir(self.pid) / a["file"]
        return p if p.exists() else None

    def _inspect_asset(self, aid: str) -> dict:
        from ..engines.precision.inspect import inspect_geometry
        p = self._asset_path(aid)
        if not p:
            return {"error": f"no such reference geometry: {aid!r}"}
        return inspect_geometry(p)

    def _trace_asset(self, aid: str, opts: dict):
        from .. import trace
        a = store.get_asset(self.pid, aid)
        p = self._asset_path(aid)
        if not a or not p:
            return {"error": f"no such image: {aid!r}"}
        if not a["mime"].startswith("image/"):
            return {"error": "build_from_image needs an image (PNG/JPG/WEBP)"}
        try:
            tr = trace.trace_polygons(p)
        except ValueError as exc:
            return {"error": f"could not trace the image: {exc}"}
        return trace.generate_program(
            tr, opts.get("width_mm", 40.0), opts.get("logo_height_mm", 2.0),
            opts.get("base_thickness_mm", 1.5))

    # --- turn lifecycle ---
    def submit_answers(self, answers: list) -> None:
        if self.ask_pending:
            self.answers_q.put(answers)

    def request_stop(self) -> None:
        self.orch.request_stop()
        if self.ask_pending:
            self.answers_q.put(None)

    def start_turn(self, text: str, images: list[dict]) -> str | None:
        """Kick off a turn in a session-owned thread. Returns an error string if
        the turn can't start (busy / quota / slot), else None."""
        with self.lock:
            if self.busy:
                return "You already have a build running — wait for it to finish."
            if not limits.check_daily_quota(store, self.uid):
                return "Daily build quota reached — try again tomorrow."
            if not limits.acquire_build_slot(self.uid):
                return "You already have a build running — wait for it to finish."
            self.busy = True
        threading.Thread(target=self._run_turn, args=(text, images), daemon=True).start()
        return None

    def _run_turn(self, text: str, images: list[dict]) -> None:
        self.deliver({"type": "status", "state": "thinking"})
        try:
            reply = self.orch.send(text, images)
            self.deliver({"type": "assistant", "text": reply})
        except Exception:
            if self.orch.messages and self.orch.messages[-1].get("role") == "user":
                self.orch.messages.pop()
            ref = uuid.uuid4().hex[:6]
            log.error("session %s turn failed ref=%s", self.pid, ref, exc_info=True)
            self.deliver({"type": "error",
                          "message": f"Something went wrong handling that request. Please try again. (ref {ref})"})
        finally:
            limits.release_build_slot(self.uid)
            with self.lock:
                self.busy = False
            self.deliver({"type": "status", "state": "idle"})
            _maybe_evict_session(self)


def _get_session(pid: str, uid: str) -> ProjectSession:
    with _sessions_lock:
        s = _sessions.get(pid)
        if s is None:
            s = ProjectSession(pid, uid)
            _sessions[pid] = s
        return s


def _maybe_evict_session(session: ProjectSession) -> None:
    """Drop an idle session (no turn running, no client) so its orchestrator memory
    is freed; it rebuilds from the DB on the next connect."""
    with _sessions_lock:
        if not session.busy and session.sink is None and _sessions.get(session.pid) is session:
            del _sessions[session.pid]


# ---- websocket chat -------------------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket, project: str | None = None):
    await ws.accept()
    cid = uuid.uuid4().hex[:8]  # correlation id: ties every log line for this socket together
    # SessionMiddleware populates ws.session for websocket scopes, so the cookie
    # authenticates the socket without any extra handshake.
    uid = auth.current_uid(ws.session)
    if not uid:
        log.info("ws %s rejected: not signed in", cid)
        await ws.send_json({"type": "error", "message": "not signed in"})
        await ws.close(code=4401)
        return
    pid = project
    proj = store.get_project(pid) if pid else None
    if not proj or proj.get("user_id") != uid:
        log.info("ws %s rejected: unknown/unowned project=%r", cid, pid)
        await ws.send_json({"type": "error", "message": "unknown or missing project"})
        await ws.close()
        return
    log.info("ws %s open project=%s", cid, pid)
    _active_ws.add(ws)

    loop = asyncio.get_running_loop()
    chat_bucket = limits.new_bucket(10, 60)  # per-connection: 10 chat messages/min
    closed = {"v": False}

    def sink(payload: dict) -> None:
        """Thread-safe push to THIS socket. Called from the turn thread; blocks it
        briefly so a slow/dead client can't stall the build, then gives up."""
        if closed["v"]:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop).result(timeout=8)
            log.debug("ws %s -> %s", cid, payload.get("type"))
        except Exception:
            log.warning("ws %s send failed type=%s", cid, payload.get("type"), exc_info=True)

    session = _get_session(pid, uid)
    busy, pending_ask = session.attach(sink)
    if busy:
        # a turn from a previous connection is still running — show it so the chat
        # isn't empty, and re-send any outstanding question. The reply arrives here
        # when the turn completes.
        await ws.send_json({"type": "status", "state": "thinking"})
        if pending_ask:
            await ws.send_json({"type": "ask_user", "questions": pending_ask})

    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            log.debug("ws %s <- %s", cid, kind)  # type only; never the api_key or message body
            if kind == "init":
                if data.get("model"):
                    session.orch.model = data["model"]
                session.orch.api_key = data.get("api_key") or None
                # busy => a turn from a previous connection is still running and we
                # just adopted it; tell the client so the thinking dots make sense.
                await ws.send_json({"type": "ready", "model": session.orch.model,
                                    "busy": session.busy})
            elif kind == "chat" and data.get("text", "").strip():
                if not chat_bucket.take():
                    await ws.send_json({"type": "error",
                                        "message": "Too many messages — wait a moment before sending more."})
                    continue
                images, geometry = _split_assets(pid, data.get("assets") or [])
                text = data["text"]
                if images:  # ids so the agent can trace a flat logo with build_from_image
                    ilines = "\n".join(f"- id={img['id']}" for img in images)
                    text += ("\n\n[Attached image ids — for a flat logo/icon/graphic, use "
                             f"build_from_image to trace the real outline:\n{ilines}]")
                if geometry:  # ids so the agent can inspect_geometry
                    glines = "\n".join(f"- id={g['id']} ({g['name']})" for g in geometry)
                    text += ("\n\n[Reference geometry attached — measure with "
                             f"inspect_geometry before asking for known dimensions:\n{glines}]")
                # picked model parts: {"run_id", "faces":[seed facet idx | {seed,
                # face: BREP id, precise}], "edges":[id], "region": brush summary}
                sel = data.get("selection")
                if isinstance(sel, dict) and sel.get("run_id"):
                    faces: list[int | dict] = []
                    for f in (sel.get("faces") or []):
                        if isinstance(f, int):
                            faces.append(f)
                        elif isinstance(f, dict) and isinstance(f.get("seed"), int):
                            faces.append({
                                "seed": f["seed"],
                                "face": f["face"] if isinstance(f.get("face"), int) else None,
                                "precise": bool(f.get("precise")),
                            })
                    edges = [e for e in (sel.get("edges") or []) if isinstance(e, int)]
                    region = sel.get("region") if isinstance(sel.get("region"), dict) else None
                    if (faces or edges or region) and store.get_run(pid, str(sel["run_id"])):
                        note = select.build_note(
                            config.project_runs_dir(pid) / str(sel["run_id"]),
                            faces, edges, region)
                        if note:
                            text += "\n\n" + note
                # anchor the edit to the version the user is actually looking at:
                # {run_id, params} of the on-screen model. Injected only when it
                # differs from the agent's last build (manual save / older version)
                # so the agent edits what's on screen instead of its stale copy.
                cur = data.get("current")
                if isinstance(cur, dict) and cur.get("run_id"):
                    cnote = _current_model_note(
                        pid, session, str(cur["run_id"]),
                        cur["params"] if isinstance(cur.get("params"), dict) else None)
                    if cnote:
                        text += "\n\n" + cnote
                err = session.start_turn(text, images)
                if err:
                    await ws.send_json({"type": "error", "message": err})
                    await ws.send_json({"type": "status", "state": "idle"})
            elif kind == "answers":
                session.submit_answers(data.get("answers", []))
            elif kind == "stop":
                session.request_stop()
    except WebSocketDisconnect as exc:
        # normal client-side close (tab closed, refresh, navigated away, blip). The
        # turn keeps running in the session; a reconnect re-attaches to it.
        log.info("ws %s closed by client code=%s", cid, getattr(exc, "code", "?"))
    except Exception:
        log.error("ws %s receive loop crashed", cid, exc_info=True)
    finally:
        closed["v"] = True
        _active_ws.discard(ws)
        session.detach(sink)
        _maybe_evict_session(session)
        log.info("ws %s teardown project=%s", cid, pid)


def _split_assets(pid: str, asset_ids: list) -> tuple[list[dict], list[dict]]:
    """ids -> (image attachments [{id,path}], geometry attachments [{id,name}]).
    Images ride the message as vision blocks; geometry is inspected on request."""
    images, geometry = [], []
    for aid in asset_ids[:8]:
        if not (isinstance(aid, str) and aid.isalnum()):
            continue
        a = store.get_asset(pid, aid)
        if not a:
            continue
        p = config.project_refs_dir(pid) / a["file"]
        if not p.exists():
            continue
        if a["mime"].startswith("image/"):
            images.append({"id": aid, "path": str(p)})
        else:
            geometry.append({"id": aid, "name": a["name"]})
    return images, geometry


# ---- authenticated file serving -------------------------------------------
# Run artifacts and previews used to be public StaticFiles mounts. They're now
# per-request authenticated (project ownership for /files) so one user can't read
# another's models. The session cookie is same-origin, so <img>/STLLoader/<a
# download> keep working unchanged; path-traversal is blocked via resolve().

def _safe_file(base: Path, rel: str) -> Path | None:
    target = (base / rel).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        return None
    return target


# run artifacts + reference uploads are content-addressed (a run_id / asset_id dir
# never changes once written), so they're safe to cache hard. This stops the viewer
# re-downloading the (up to 13 MB) STL every time you switch between saved versions.
# FileResponse also sets ETag/Last-Modified, so even a hard reload gets a 304.
_IMMUTABLE = "public, max-age=31536000, immutable"


@app.get("/files/{pid}/{path:path}")
def project_file(pid: str, path: str, user: dict = Depends(get_current_user)):
    require_project(pid, user)
    base = config.project_dir(pid)
    target = (base / path).resolve()
    if not target.is_relative_to(base.resolve()):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not target.is_file():
        # local cache miss (fresh/ephemeral container, or an evicted artifact):
        # repopulate from R2 if we have a durable copy there, else it's gone.
        if not object_store.download_to(f"{pid}/{path}", target):
            return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, headers={"Cache-Control": _IMMUTABLE})


@app.get("/previews/{path:path}")
def preview_file(path: str, user: dict = Depends(get_current_user)):
    # any signed-in user; preview dirs are unguessable 12-hex and pruned to 12
    target = _safe_file(config.PREVIEW_DIR, path)
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target)


# ---- static frontend ------------------------------------------------------

if FRONTEND_DIST.exists():
    # Vite emits hashed bundles under /assets; mount that, then a catch-all
    # returns index.html so client-side routes (/p/<id>) deep-link correctly.
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="static")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)  # favicon, etc.
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/")
    def index():
        # Standalone API image (frontend ships as its own container). `/` is a
        # benign 200 so edge/load-balancer health checks that probe the root pass
        # — the SPA lives at its own origin; see /healthz for the liveness probe.
        return JSONResponse(
            {"service": "cadio-api", "ok": True,
             "detail": "CADIO API — the web UI runs as a separate frontend."},
        )
