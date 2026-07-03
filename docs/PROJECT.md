# Forma — living build doc

> Source-of-truth section first (always current); build log below; learnings at
> the end. Product spec: `../../ai-3d-product-plan.md` (v2, shell + engines).

## 0. CURRENT STATE — source of truth (updated 2026-07-02)

**Phase:** 0 (shell skeleton + Engine 1 vertical slice). **Working end to end:**

- **Engine contract** (`forma/engines/base.py`): `execute(code, params, run_dir)
  -> ExecutionResult` (manifest, artifacts, measured bbox/volume, validation).
  Engines are plugins; the shell never imports engine internals.
- **Engine 1 — precision** (`forma/engines/precision/`): build123d 0.11 /
  OCCT on Python 3.12 (`uv venv .venv`). **Warm worker pool** (`pool.py`):
  resident `python -I` workers with the kernel pre-imported serve jobs over
  stdio — rebuilds ≈20ms vs ≈6s cold; workers are replaced on death/timeout
  and a one-shot cold subprocess remains as fallback. Persistent sandbox HOME
  (`.sandbox_home/`) keeps OCCT caches warm. `preview=True` skips STEP + GLB.
  Stripped env, 90s wall clock, per-run output dir. Known caveat: a worker
  serves many jobs, so a hostile program could poison its own worker —
  superseded by the Docker sandbox (P1).
- **Validation gate v1** (`forma/validation/mesh.py`): watertight, winding,
  positive volume, mesh-bbox vs BREP-bbox cross-check (catches export bugs).
- **Program contract:** `PARAMS` list (name/default/min/max/unit/group) +
  `build(params) -> Part`; requirement asserts inside `build()`. Slider
  re-execution never calls the LLM.
- **Agent** (`forma/agent/orchestrator.py`): **provider-agnostic via LiteLLM**
  — one OpenAI-format tool loop runs on Anthropic, OpenAI, Gemini, xAI/Grok,
  etc. Model from `FORMA_MODEL` env or `chat --model provider/model-id`
  (default `anthropic/claude-opus-4-8`); keys from each provider's standard
  env var (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / XAI_API_KEY).
  Tools = `run_cad` (execute+validate, returns measured facts) and `ask_user`
  (batched Q&A, defaults). System prompt = program contract + corpus seed
  (`agent/corpus.py`). CLI REPL: `python -m forma.cli chat`.
- **Web workspace — the primary surface** (`forma/api/app.py` + `frontend/`,
  a **React 19 + TypeScript + Vite** app using @react-three/fiber + drei):
  three panes — chat / 3D viewer / tabbed side panel (Params · Code · Runs).
  Dev: `npm run dev` in `frontend/` (proxies /api, /runs, /ws to uvicorn on
  :8000). Prod: `npm run build` → FastAPI serves `frontend/dist` at `/`
  (returns a 503 hint if the build is missing).
  - **WebSocket `/ws/chat?model=`** runs the agent loop server-side; events to
    the browser: `status` (thinking / building), `ask_user` (rendered as an
    inline form; answers round-trip to the blocked agent thread via a queue),
    `run` (persisted meta with artifact URLs + manifest; viewer and params
    panel update live), `assistant`, `error`. Model picked per-connection from
    the UI header.
  - **Params tab**: manifest-driven sliders/number inputs; "Rebuild" POSTs the
    run's own `program.py` + new values to `/api/execute` — **no LLM call**.
  - **Code tab**: raw program editing + run. **Runs tab**: history, click to
    reload any version. Exports (STL/STEP/GLB) top-right of the viewer.
  - REST: `POST /api/execute`, `GET /api/runs`, `GET /api/config` (default
    model + which provider keys the server sees), `GET /api/example`.

**Verified 2026-07-02:** CLI run with overrides (200×60×30 box, validation OK);
API execute → 200 with artifact URLs; viewer page + STL serving → 200.
Websocket flow verified offline (mocked LLM + real engine): chat → ask_user
form round-trip → run event (artifact URLs + manifest) → assistant reply, with
the answered dimension (150mm) measured in the produced geometry.

## Next steps

**Frontend track:** see `docs/FRONTEND-PLAN.md` (3 phases; next up: 1.1
provider/key settings in UI → 1.2 realtime tweaks via warm worker pool →
1.3 app shell polish).

- [ ] Agent exit test: recreate the socket back box from photos + Q&A alone
      (needs image input in the CLI/chat path — add `--image` attachment)
- [ ] Stub second engine to prove the contract (Phase-0 architecture exit test)
- [ ] Checkpoint artifact: dimensioned 2D drawing before "done"
- [ ] Docker sandbox (no-network, resource-limited) to replace subprocess v0
- [ ] Versions as first-class records (tree, labels) instead of flat run dirs
- [x] Web chat panel (websocket) wiring the orchestrator into the workspace
- [x] Manifest-driven sliders in the workspace (PARAMS rendered as controls)
- [ ] Persist chat sessions (survive reload; today a reload starts a fresh
      conversation while runs persist)
- [ ] Image upload in web chat (reference photos → vision models)

## Build log

**2026-07-03 — image references + disconnect fix + auto-reconnect.**
- **Root-caused the mid-conversation disconnects:** `uvicorn --reload` watches
  the whole repo, so the engine writing `runs/*/program.py` triggered a server
  restart, killing the websocket (visible in the user's log). Fix: `python -m
  forma.cli serve --reload` watches only the `forma/` package. Frontend also
  gained **auto-reconnect** (5 attempts, backoff, re-sends init with model+key)
  with an honest note that a server restart resets the agent's conversation.
- **Reference images:** `POST/GET /api/assets` (15MB cap, image mimes only,
  ids validated — never treated as paths), served at `/assets/`. Chat accepts
  attachments via 📎 upload, drag-drop onto the composer, or 🖼 the reference
  library (any previously uploaded image can be re-attached at any time).
  Attachments flow over the ws as asset ids → orchestrator builds multimodal
  content (base64 data URLs, OpenAI image format — LiteLLM converts per
  provider; needs a vision-capable model). Corpus rule added: describe what
  you see and confirm the object before asking questions; dimensions still
  always come from the user.

**2026-07-03 — frontend plan items 1.1 + 1.2 landed.**
(1.1) Provider & API key live in the UI: settings slide-over with provider
select, per-provider key storage (localStorage), live model list fetched from
the provider's own API (`POST /api/providers/models` — doubles as the key
test), model filter. Keys travel per-connection in the ws `init` message
(never query strings / never persisted server-side); orchestrator passes
`api_key` through to LiteLLM; env vars remain a fallback.
(1.2) Realtime tweaks: warm worker pool + `preview: true` execute path
(STL-only, `runs/_preview/` scratch slots pruned to 12, excluded from
history); frontend debounces (120ms) + coalesces slider changes to at most
one in-flight preview; viewer swaps meshes without flicker and only re-frames
the camera on saved runs. Params panel gains Save version (label), reset, and
per-param dirty dots. Measured: engine preview ≈20ms, HTTP round-trip ≈25ms.

**2026-07-02 — frontend moved to React + Vite + TypeScript.** The v0 single
vanilla-JS HTML file (chosen to keep the slice build-tool-free) is replaced by
a proper app in `frontend/`: components (Chat, Viewer, SidePanel), a `useChat`
websocket hook, typed API layer, @react-three/fiber viewer with drei Bounds
auto-fit. Next.js was considered and rejected: no SSR/SEO needs for a
canvas + websocket workspace; Vite dev-proxy + static build is the fit.
Verified via TestClient: React index + hashed assets served, agent ws flow
green.

**2026-07-02 — agent made provider-agnostic.** Orchestrator moved from the
Anthropic SDK to LiteLLM (OpenAI wire format for messages/tool calls) so any
API key works. Verified offline with a mocked LLM driving the real engine
through a full tool round-trip. Fixed a bug found by that test: relative
`run_dir` + subprocess `cwd=run_dir` made the runner write results into a
nested path — engine now resolves `run_dir` to absolute.

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
