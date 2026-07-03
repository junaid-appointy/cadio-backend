"""Forma web API — the product surface.

REST:
  POST /api/execute      {code, params?, label?} -> run + validate (no LLM; used
                         by the params panel and code editor)
  GET  /api/runs         list past runs (newest first)
  GET  /api/config       default model + which provider keys the server has
  GET  /api/example      starter program
  GET  /runs/...         static artifacts (stl/step/glb/program.py)
  GET  /                 the workspace page

WebSocket /ws/chat — the agent loop:
  client -> {"type":"init","model":...,"api_key":...}  configure the session
            (key held in connection memory only; never persisted or logged);
            server replies {"type":"ready","model":...}. `?model=` query param
            remains as a fallback when no init is sent.
  client -> {"type":"chat","text":...}          user message
  client -> {"type":"answers","answers":[...]}  reply to an ask_user form
  server -> {"type":"status","state":"thinking"|"running_cad"|"idle",...}
  server -> {"type":"ask_user","questions":[{question,default}...]}
  server -> {"type":"run","meta":{...}}         a version was built (or failed)
  server -> {"type":"assistant","text":...}     final agent text for the turn
  server -> {"type":"error","message":...}

Run:  uvicorn forma.api.app:app --reload
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent.orchestrator import DEFAULT_MODEL, Orchestrator
from ..engines.precision import PrecisionEngine
from .providers import ProviderError, list_models

from ..config import ASSETS_DIR, REPO_ROOT, RUNS_DIR

ROOT = REPO_ROOT
FRONTEND_DIST = ROOT / "frontend" / "dist"

ASSET_MAX_BYTES = 15 * 1024 * 1024
ASSET_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

app = FastAPI(title="Forma", version="0.0.1")
engine = PrecisionEngine()


class ExecuteRequest(BaseModel):
    code: str
    params: dict | None = None
    label: str | None = None
    preview: bool = False  # fast path: no STEP/GLB, no history entry


PREVIEW_DIR = RUNS_DIR / "_preview"


def _prune_previews(keep: int = 12) -> None:
    if not PREVIEW_DIR.exists():
        return
    dirs = sorted(PREVIEW_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in dirs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _persist_meta(run_id: str, result_dict: dict, label: str = "") -> dict:
    """Write meta.json for a run and return the client-facing payload."""
    meta = dict(result_dict)
    meta["run_id"] = run_id
    meta["label"] = label
    meta["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta.pop("run_dir", None)
    if meta.get("ok"):
        meta["artifact_urls"] = {
            kind: f"/runs/{run_id}/{Path(p).name}"
            for kind, p in result_dict.get("artifacts", {}).items()
        }
        meta["program_url"] = f"/runs/{run_id}/program.py"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta, default=str))
    return meta


@app.post("/api/execute")
def execute(req: ExecuteRequest):
    if req.preview:
        run_id = f"_preview/{uuid.uuid4().hex[:12]}"
        result = engine.execute(req.code, req.params, RUNS_DIR / run_id, preview=True)
        meta = result.to_dict()
        meta.pop("run_dir", None)
        meta["run_id"] = run_id
        meta["label"] = ""
        meta["preview"] = True
        if result.ok:
            meta["artifact_urls"] = _artifact_urls_for(run_id, result.artifacts)
        _prune_previews()
        return JSONResponse(meta, status_code=200 if result.ok else 422)

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
    result = engine.execute(req.code, req.params, RUNS_DIR / run_id)
    meta = _persist_meta(run_id, result.to_dict(), req.label or "")
    return JSONResponse(meta, status_code=200 if result.ok else 422)


def _artifact_urls_for(run_id: str, artifacts: dict) -> dict[str, str]:
    return {kind: f"/runs/{run_id}/{Path(p).name}" for kind, p in artifacts.items()}


@app.get("/api/runs")
def list_runs():
    runs = []
    for meta_file in sorted(RUNS_DIR.glob("*/meta.json"), reverse=True)[:100]:
        try:
            runs.append(json.loads(meta_file.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return runs


@app.get("/api/config")
def config():
    provider_keys = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
        "xai": bool(os.environ.get("XAI_API_KEY")),
    }
    return {"default_model": DEFAULT_MODEL, "provider_keys": provider_keys}


@app.get("/api/example")
def example_program():
    return {"code": (ROOT / "examples" / "simple_box.py").read_text()}


def _asset_meta(meta_file: Path) -> dict | None:
    try:
        meta = json.loads(meta_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    # url derived from the filename, not trusted from disk (and the mount can move)
    meta["url"] = f"/refs/{meta['file']}"
    return meta


@app.post("/api/assets")
async def upload_asset(file: UploadFile):
    """Store a reference image; returns {id, url, name}. Reusable in any
    later chat message via its id."""
    mime = file.content_type or ""
    if mime not in ASSET_MIMES:
        return JSONResponse({"error": f"unsupported type {mime!r} — upload an image"}, status_code=400)
    data = await file.read()
    if len(data) > ASSET_MAX_BYTES:
        return JSONResponse({"error": "image larger than 15MB"}, status_code=400)

    asset_id = uuid.uuid4().hex[:12]
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[mime]
    (ASSETS_DIR / f"{asset_id}{ext}").write_bytes(data)
    meta = {
        "id": asset_id,
        "file": f"{asset_id}{ext}",
        # /refs, NOT /assets — Vite's built bundle owns /assets/* under the
        # root mount, and shadowing it blanks the whole UI
        "url": f"/refs/{asset_id}{ext}",
        "name": Path(file.filename or "image").name[:80],
        "mime": mime,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (ASSETS_DIR / f"{asset_id}.json").write_text(json.dumps(meta))
    return meta


@app.get("/api/assets")
def list_assets():
    metas = [m for f in ASSETS_DIR.glob("*.json") if (m := _asset_meta(f))]
    return sorted(metas, key=lambda m: m.get("created_at", ""), reverse=True)


def _asset_paths(asset_ids: list[str]) -> list[Path]:
    """Resolve ids -> files, rejecting anything that isn't a known asset id
    (ids are our own hex strings — never treat them as paths)."""
    paths = []
    for asset_id in asset_ids[:8]:
        if not (isinstance(asset_id, str) and asset_id.isalnum()):
            continue
        meta = _asset_meta(ASSETS_DIR / f"{asset_id}.json")
        if meta:
            p = ASSETS_DIR / meta["file"]
            if p.exists():
                paths.append(p)
    return paths


class ProviderModelsRequest(BaseModel):
    provider: str
    api_key: str


@app.post("/api/providers/models")
def provider_models(req: ProviderModelsRequest):
    """List models the given key can access. Doubles as the key-validity test.
    The key is used for this one outbound call and discarded."""
    try:
        return {"models": list_models(req.provider, req.api_key)}
    except ProviderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket, model: str | None = None):
    await ws.accept()
    loop = asyncio.get_running_loop()
    answers_q: queue.Queue = queue.Queue()  # browser answers -> agent thread
    chat_q: asyncio.Queue = asyncio.Queue()  # user messages -> worker
    closed = False
    ask_pending = False  # an ask_user form is waiting on the browser

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
            answers = answers_q.get()  # blocks the agent thread until the form comes back
        finally:
            ask_pending = False
        return answers if answers is not None else []

    def on_event(event: dict) -> None:
        if event.get("type") == "run":
            meta = _persist_meta(event["run_id"], event["result"], event.get("label", ""))
            send_threadsafe({"type": "run", "meta": meta})
        else:
            send_threadsafe(event)

    orch: Orchestrator | None = None

    def make_orch(m: str | None, api_key: str | None) -> Orchestrator:
        return Orchestrator(
            engine, RUNS_DIR, model=m or model, api_key=api_key,
            ask_user=ask_user, on_event=on_event,
        )

    async def worker():
        nonlocal orch
        while True:
            text, images = await chat_q.get()
            if orch is None:  # no init received — query-param model + env keys
                orch = make_orch(model, None)
            await ws.send_json({"type": "status", "state": "thinking"})
            try:
                reply = await asyncio.to_thread(orch.send, text, images)
                await ws.send_json({"type": "assistant", "text": reply})
            except Exception as exc:
                # drop the failed user turn so history stays consistent
                if orch.messages and orch.messages[-1].get("role") == "user":
                    orch.messages.pop()
                try:
                    await ws.send_json(
                        {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                    )
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
                orch = make_orch(data.get("model"), data.get("api_key") or None)
                await ws.send_json({"type": "ready", "model": orch.model})
            elif kind == "chat" and data.get("text", "").strip():
                images = _asset_paths(data.get("assets") or [])
                chat_q.put_nowait((data["text"], images))
            elif kind == "answers":
                answers_q.put(data.get("answers", []))
            elif kind == "stop":
                if orch is not None:
                    orch.request_stop()
                if ask_pending:
                    answers_q.put(None)  # unblock the thread waiting on a form
    except WebSocketDisconnect:
        pass
    finally:
        closed = True
        answers_q.put(None)  # unblock an agent thread waiting on a form
        worker_task.cancel()


app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")
app.mount("/refs", StaticFiles(directory=ASSETS_DIR), name="refs")

if FRONTEND_DIST.exists():
    # production build of the React app (frontend/: `npm run build`)
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="app")
else:
    @app.get("/")
    def index():
        return JSONResponse(
            {"detail": "frontend not built — run `npm run build` in frontend/, "
                       "or use the Vite dev server (`npm run dev`)"},
            status_code=503,
        )
