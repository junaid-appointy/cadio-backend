"""Forma web API — the product surface.

Everything is project-scoped. Runs, references, and conversation all belong to
a project and persist in SQLite (~/.forma/forma.db); files live under
~/.forma/projects/<pid>/. Conversations resume across reload and restart
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
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import affect, config
from ..agent.orchestrator import DEFAULT_MODEL, Orchestrator
from ..engines.precision import PrecisionEngine
from ..store import Store
from . import history
from .providers import ProviderError, list_models

ROOT = config.REPO_ROOT
FRONTEND_DIST = ROOT / "frontend" / "dist"

ASSET_MAX_BYTES = 40 * 1024 * 1024
IMAGE_MIMES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
# reference geometry: agent measures these with inspect_geometry (not shown as images)
GEOMETRY_EXTS = {".step": "model/step", ".stp": "model/step", ".stl": "model/stl"}

app = FastAPI(title="Forma", version="0.1.0")
engine = PrecisionEngine()
store = Store()


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
        meta["artifact_urls"] = {kind: f"{base}/{name}" for kind, name in meta.get("artifacts", {}).items()}
        meta["program_url"] = f"{base}/program.py"
    return meta


def _result_to_meta(run_id: str, result_dict: dict) -> dict:
    """Normalize an engine ExecutionResult dict for storage: keep facts, store
    artifacts as bare filenames (URLs are computed per-project at read time)."""
    meta = {k: result_dict.get(k) for k in
            ("ok", "params", "manifest", "bbox", "volume_mm3", "validation", "error")}
    meta["run_id"] = run_id
    meta["artifacts"] = {kind: Path(p).name for kind, p in result_dict.get("artifacts", {}).items()}
    return meta


def _save_run(pid: str, run_id: str, result_dict: dict, label: str,
              parent_run_id: str | None = None) -> dict:
    meta = _result_to_meta(run_id, result_dict)
    run = store.add_run(pid, run_id, label, bool(meta.get("ok")), meta, parent_run_id)
    # precompute the parameter→affected-faces map in the background so clicking a
    # parameter highlights instantly (see /affect endpoint for the lazy fallback)
    if meta.get("ok") and meta.get("manifest"):
        run_dir = config.project_runs_dir(pid) / run_id
        threading.Thread(
            target=_precompute_affect,
            args=(run_dir, result_dict.get("params", {}), result_dict.get("manifest", [])),
            daemon=True,
        ).start()
    return _run_payload(pid, run)


def _precompute_affect(run_dir: Path, params: dict, manifest: list) -> None:
    try:
        code = (run_dir / "program.py").read_text()
        affect.build_and_cache(engine, code, params, manifest, run_dir)
    except Exception:
        pass  # best-effort; the endpoint recomputes on demand if this failed


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
def list_projects():
    return store.list_projects()


@app.post("/api/projects")
def create_project(req: ProjectCreate):
    return store.create_project(req.name)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    proj = store.get_project(pid)
    if not proj:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return proj


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, req: ProjectPatch):
    proj = store.update_project(pid, name=req.name, archived=req.archived)
    if not proj:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return proj


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    if not store.delete_project(pid):
        return JSONResponse({"error": "project not found"}, status_code=404)
    return {"ok": True}


@app.delete("/api/projects/{pid}/assets/{asset_id}")
def delete_asset(pid: str, asset_id: str):
    if not store.delete_asset(pid, asset_id):
        return JSONResponse({"error": "asset not found"}, status_code=404)
    return {"ok": True}


@app.get("/api/projects/{pid}/runs")
def project_runs(pid: str):
    return [_run_payload(pid, r) for r in store.list_runs(pid)]


@app.get("/api/projects/{pid}/runs/{run_id}/affect")
def run_affect(pid: str, run_id: str):
    """{param_name: [affected face index, ...]} for a run. Served from the cached
    affect.json if the background precompute finished; otherwise computed now."""
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
        return {}
    code = (run_dir / "program.py").read_text()
    return affect.build_and_cache(engine, code, meta.get("params", {}), meta.get("manifest", []), run_dir)


@app.get("/api/projects/{pid}/history")
def project_history(pid: str):
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
def preview(req: PreviewRequest):
    """Fast, throwaway run for realtime slider tweaks: no STEP/GLB, no history."""
    pdir = config.PREVIEW_DIR / uuid.uuid4().hex[:12]
    result = engine.execute(req.code, req.params, pdir, preview=True)
    meta = result.to_dict()
    meta.pop("run_dir", None)
    meta["preview"] = True
    if result.ok:
        meta["artifact_urls"] = {
            kind: f"/previews/{pdir.name}/{Path(p).name}" for kind, p in result.artifacts.items()
        }
    _prune_previews()
    return JSONResponse(meta, status_code=200 if result.ok else 422)


@app.post("/api/projects/{pid}/execute")
def project_execute(pid: str, req: ExecuteRequest):
    if not store.get_project(pid):
        return JSONResponse({"error": "project not found"}, status_code=404)
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
    result = engine.execute(req.code, req.params, config.project_runs_dir(pid) / run_id)
    payload = _save_run(pid, run_id, result.to_dict(), req.label or "", req.parent_run_id)
    return JSONResponse(payload, status_code=200 if result.ok else 422)


# ---- assets ---------------------------------------------------------------

@app.get("/api/projects/{pid}/assets")
def project_assets(pid: str):
    return store.list_assets(pid)


@app.post("/api/projects/{pid}/assets")
async def upload_asset(pid: str, file: UploadFile):
    if not store.get_project(pid):
        return JSONResponse({"error": "project not found"}, status_code=404)
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
    name = Path(file.filename or "file").name[:80]
    return store.add_asset(pid, asset_id, fname, name, store_mime)


# ---- providers ------------------------------------------------------------

class ProviderModelsRequest(BaseModel):
    provider: str
    api_key: str


@app.post("/api/providers/models")
def provider_models(req: ProviderModelsRequest):
    try:
        return {"models": list_models(req.provider, req.api_key)}
    except ProviderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ---- websocket chat -------------------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket, project: str | None = None):
    await ws.accept()
    pid = project
    if not pid or not store.get_project(pid):
        await ws.send_json({"type": "error", "message": "unknown or missing project"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    answers_q: queue.Queue = queue.Queue()
    chat_q: asyncio.Queue = asyncio.Queue()
    closed = False
    ask_pending = False

    def send_threadsafe(payload: dict) -> None:
        if closed:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop).result(timeout=30)
        except Exception:
            pass

    def ask_user(questions: list[dict]) -> list[dict]:
        nonlocal ask_pending
        send_threadsafe({"type": "ask_user", "questions": questions})
        ask_pending = True
        try:
            answers = answers_q.get()
        finally:
            ask_pending = False
        return answers if answers is not None else []

    def on_event(event: dict) -> None:
        if event.get("type") == "run":
            run_id = event["run_id"]
            payload = _save_run(pid, run_id, event["result"], event.get("label", ""))
            store.add_message(pid, "event", {"kind": "run", "run_id": run_id})
            send_threadsafe({"type": "run", "meta": payload})
        else:
            send_threadsafe(event)

    def on_message(role: str, record: dict) -> None:
        store.add_message(pid, role, record)

    def asset_path(aid: str) -> Path | None:
        a = store.get_asset(pid, aid)
        if not a:
            return None
        p = config.project_refs_dir(pid) / a["file"]
        return p if p.exists() else None

    def inspect_asset(aid: str) -> dict:
        from ..engines.precision.inspect import inspect_geometry
        p = asset_path(aid)
        if not p:
            return {"error": f"no such reference geometry: {aid!r}"}
        return inspect_geometry(p)

    def trace_asset(aid: str, opts: dict):
        """Image -> build123d program (traced outline). str on success, {'error'} on failure."""
        from .. import trace
        a = store.get_asset(pid, aid)
        p = asset_path(aid)
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

    orch = Orchestrator(
        engine, config.project_runs_dir(pid),
        ask_user=ask_user, on_event=on_event, on_message=on_message,
        inspect_asset=inspect_asset, trace_asset=trace_asset,
    )
    # rebuild the agent's memory from stored history (resume)
    records = store.get_messages(pid)
    if records:
        orch.set_history(history.to_llm_messages(records, asset_path))

    async def worker():
        while True:
            text, images = await chat_q.get()
            await ws.send_json({"type": "status", "state": "thinking"})
            try:
                reply = await asyncio.to_thread(orch.send, text, images)
                await ws.send_json({"type": "assistant", "text": reply})
            except Exception as exc:
                if orch.messages and orch.messages[-1].get("role") == "user":
                    orch.messages.pop()
                try:
                    await ws.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                except Exception:
                    return
            try:
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                return

    worker_task = asyncio.create_task(worker())
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "init":
                if data.get("model"):
                    orch.model = data["model"]
                orch.api_key = data.get("api_key") or None
                await ws.send_json({"type": "ready", "model": orch.model})
            elif kind == "chat" and data.get("text", "").strip():
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
                chat_q.put_nowait((text, images))
            elif kind == "answers":
                answers_q.put(data.get("answers", []))
            elif kind == "stop":
                orch.request_stop()
                if ask_pending:
                    answers_q.put(None)
    except WebSocketDisconnect:
        pass
    finally:
        closed = True
        answers_q.put(None)
        worker_task.cancel()


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


# ---- static mounts --------------------------------------------------------

app.mount("/files", StaticFiles(directory=config.PROJECTS_DIR), name="files")
app.mount("/previews", StaticFiles(directory=config.PREVIEW_DIR), name="previews")

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
        return JSONResponse(
            {"detail": "frontend not built — run `npm run build` in frontend/, "
                       "or use the Vite dev server (`npm run dev`)"},
            status_code=503,
        )
