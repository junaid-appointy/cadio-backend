# Forma — living build doc

> Source-of-truth section first (always current); build log below; learnings at
> the end. Product spec: `../../ai-3d-product-plan.md` (v2, shell + engines).

## 0. CURRENT STATE — source of truth (updated 2026-07-02)

**Phase:** 0 (shell skeleton + Engine 1 vertical slice). **Working end to end:**

- **Engine contract** (`forma/engines/base.py`): `execute(code, params, run_dir)
  -> ExecutionResult` (manifest, artifacts, measured bbox/volume, validation).
  Engines are plugins; the shell never imports engine internals.
- **Engine 1 — precision** (`forma/engines/precision/`): build123d 0.11 /
  OCCT on Python 3.12 (`uv venv .venv`). Sandbox v0 = subprocess `python -I`,
  stripped env (PATH/HOME/TMPDIR → run dir), 90s wall clock, per-run output
  dir. Exports STL + STEP; GLB derived via trimesh for the viewer.
- **Validation gate v1** (`forma/validation/mesh.py`): watertight, winding,
  positive volume, mesh-bbox vs BREP-bbox cross-check (catches export bugs).
- **Program contract:** `PARAMS` list (name/default/min/max/unit/group) +
  `build(params) -> Part`; requirement asserts inside `build()`. Slider
  re-execution never calls the LLM.
- **Agent** (`forma/agent/orchestrator.py`): Claude `claude-opus-4-8`, adaptive
  thinking, manual tool loop, tools = `run_cad` (execute+validate, returns
  measured facts) and `ask_user` (batched Q&A, defaults). System prompt =
  program contract + corpus seed (`agent/corpus.py`, from the socket-enclosure
  learnings). CLI REPL: `python -m forma.cli chat`.
- **API + playground** (`forma/api/app.py`, `web/index.html`): POST
  `/api/execute`, GET `/api/runs`, artifacts served at `/runs/...`; three.js
  STL viewer with editor, param overrides, validation report, run history.

**Verified 2026-07-02:** CLI run with overrides (200×60×30 box, validation OK);
API execute → 200 with artifact URLs; viewer page + STL serving → 200.

## Next steps

- [ ] Agent exit test: recreate the socket back box from photos + Q&A alone
      (needs image input in the CLI/chat path — add `--image` attachment)
- [ ] Stub second engine to prove the contract (Phase-0 architecture exit test)
- [ ] Checkpoint artifact: dimensioned 2D drawing before "done"
- [ ] Docker sandbox (no-network, resource-limited) to replace subprocess v0
- [ ] Versions as first-class records (tree, labels) instead of flat run dirs
- [ ] Web chat panel (websocket) wiring the orchestrator into the playground
- [ ] Manifest-driven sliders in the playground (render PARAMS as controls)

## Build log

**2026-07-02 — repo bootstrapped.** uv + Python 3.12 venv; deps: build123d,
trimesh, manifold3d, fastapi, uvicorn, anthropic. Wrote engine contract,
precision engine (sandbox runner + host), mesh validation gate, corpus seed,
orchestrator, CLI, FastAPI app, three.js playground, example program. Vertical
slice verified end to end (CLI + API + viewer).

## Learnings & gotchas

- **First OCCT load can exceed 90s on macOS** — Gatekeeper verifies the ~300MB
  OCP dylib once per install. Looks like a sandbox hang; it isn't. Subsequent
  imports ~3s. Consider a post-install warmup (`python -c "import build123d"`).
- **`python -I` still resolves the venv** (isolated mode drops PYTHONPATH and
  user site, not pyvenv.cfg), so the sandbox subprocess sees build123d without
  extra plumbing.
- **OCCT wants a writable HOME** for caches — the sandbox env points HOME at
  the run dir.
- **build123d algebra mode**: shapes are origin-centred; `Pos(x,y,z) * shape`
  to place; open-top cavity = subtract a cavity translated up by floor
  thickness so it overshoots the top face.
- **Python 3.14 (system brew) is too new for the OCP wheel stack** — pin 3.12
  via uv.
