"""Forma web API — v0 vertical slice.

Endpoints:
  POST /api/execute   {code, params?}  -> run program, return validation + artifact URLs
  GET  /api/runs                        -> list past runs (newest first)
  GET  /runs/...                        -> static artifacts (stl/step/glb)
  GET  /                                -> the viewer/playground page

Run:  uvicorn forma.api.app:app --reload
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..engines.precision import PrecisionEngine

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "runs"
WEB_DIR = ROOT / "web"
RUNS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Forma", version="0.0.1")
engine = PrecisionEngine()


class ExecuteRequest(BaseModel):
    code: str
    params: dict | None = None
    label: str | None = None


def _artifact_urls(run_id: str, artifacts: dict[str, str]) -> dict[str, str]:
    return {kind: f"/runs/{run_id}/{Path(p).name}" for kind, p in artifacts.items()}


@app.post("/api/execute")
def execute(req: ExecuteRequest):
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
    run_dir = RUNS_DIR / run_id
    result = engine.execute(req.code, req.params, run_dir)

    meta = result.to_dict()
    meta["run_id"] = run_id
    meta["label"] = req.label or ""
    meta["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if result.ok:
        meta["artifact_urls"] = _artifact_urls(run_id, result.artifacts)
    (run_dir / "meta.json").write_text(json.dumps(meta, default=str))

    status = 200 if result.ok else 422
    return JSONResponse(meta, status_code=status)


@app.get("/api/runs")
def list_runs():
    runs = []
    for meta_file in sorted(RUNS_DIR.glob("*/meta.json"), reverse=True)[:100]:
        try:
            runs.append(json.loads(meta_file.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return runs


@app.get("/api/example")
def example_program():
    example = (ROOT / "examples" / "simple_box.py").read_text()
    return {"code": example}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")
