"""Forma web API — the product surface.

REST:
  POST /api/execute      {code, params?, label?} -> run + validate (no LLM; used
                         by the params panel and code editor)
  GET  /api/runs         list past runs (newest first)
  GET  /api/config       default model + which provider keys the server has
  GET  /api/example      starter program
  GET  /runs/...         static artifacts (stl/step/glb/program.py)
  GET  /                 the workspace page

WebSocket /ws/chat?model=provider/model-id — the agent loop:
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
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent.orchestrator import DEFAULT_MODEL, Orchestrator
from ..engines.precision import PrecisionEngine

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "runs"
FRONTEND_DIST = ROOT / "frontend" / "dist"
RUNS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Forma", version="0.0.1")
engine = PrecisionEngine()


class ExecuteRequest(BaseModel):
    code: str
    params: dict | None = None
    label: str | None = None


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
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
    result = engine.execute(req.code, req.params, RUNS_DIR / run_id)
    meta = _persist_meta(run_id, result.to_dict(), req.label or "")
    return JSONResponse(meta, status_code=200 if result.ok else 422)


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


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket, model: str | None = None):
    await ws.accept()
    loop = asyncio.get_running_loop()
    answers_q: queue.Queue = queue.Queue()  # browser answers -> agent thread
    chat_q: asyncio.Queue = asyncio.Queue()  # user messages -> worker
    closed = False

    def send_threadsafe(payload: dict) -> None:
        if closed:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop).result(timeout=30)
        except Exception:
            pass

    def ask_user(questions: list[dict]) -> list[dict]:
        send_threadsafe({"type": "ask_user", "questions": questions})
        answers = answers_q.get()  # blocks the agent thread until the form comes back
        return answers if answers is not None else []

    def on_event(event: dict) -> None:
        if event.get("type") == "run":
            meta = _persist_meta(event["run_id"], event["result"], event.get("label", ""))
            send_threadsafe({"type": "run", "meta": meta})
        else:
            send_threadsafe(event)

    orch = Orchestrator(
        engine, RUNS_DIR, model=model, ask_user=ask_user, on_event=on_event
    )

    async def worker():
        while True:
            text = await chat_q.get()
            await ws.send_json({"type": "status", "state": "thinking"})
            try:
                reply = await asyncio.to_thread(orch.send, text)
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
            if kind == "chat" and data.get("text", "").strip():
                chat_q.put_nowait(data["text"])
            elif kind == "answers":
                answers_q.put(data.get("answers", []))
    except WebSocketDisconnect:
        pass
    finally:
        closed = True
        answers_q.put(None)  # unblock an agent thread waiting on a form
        worker_task.cancel()


app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")

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
