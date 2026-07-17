# CADIO — living build doc

> Source-of-truth section first (always current); build log below; learnings at
> the end. Product spec: `../../ai-3d-product-plan.md` (v2, shell + engines).

## 0. CURRENT STATE — source of truth (updated 2026-07-02)

**Phase:** 0 (shell skeleton + Engine 1 vertical slice). **Working end to end:**

- **Engine contract** (`cadio/engines/base.py`): `execute(code, params, run_dir)
  -> ExecutionResult` (manifest, artifacts, measured bbox/volume, validation).
  Engines are plugins; the shell never imports engine internals.
- **Engine 1 — precision** (`cadio/engines/precision/`): build123d 0.11 /
  OCCT on Python 3.12 (`uv venv .venv`). **Warm worker pool** (`pool.py`):
  resident `python -I` workers with the kernel pre-imported serve jobs over
  stdio — rebuilds ≈20ms vs ≈6s cold; workers are replaced on death/timeout
  and a one-shot cold subprocess remains as fallback. Persistent sandbox HOME
  (`.sandbox_home/`) keeps OCCT caches warm. `preview=True` skips STEP + GLB.
  Stripped env, 90s wall clock, per-run output dir. Known caveat: a worker
  serves many jobs, so a hostile program could poison its own worker —
  superseded by the Docker sandbox (P1).
- **Validation gate v1** (`cadio/validation/mesh.py`): watertight, winding,
  positive volume, mesh-bbox vs BREP-bbox cross-check (catches export bugs).
- **Program contract:** `PARAMS` list (name/default/min/max/unit/group) +
  `build(params) -> Part`; requirement asserts inside `build()`. Slider
  re-execution never calls the LLM.
- **Agent** (`cadio/agent/orchestrator.py`): **provider-agnostic via LiteLLM**
  — one OpenAI-format tool loop runs on Anthropic, OpenAI, Gemini, xAI/Grok,
  etc. Model from `CADIO_MODEL` env or `chat --model provider/model-id`
  (default `anthropic/claude-opus-4-8`); keys from each provider's standard
  env var (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / XAI_API_KEY).
  Tools = `run_cad` (execute+validate, returns measured facts) and `ask_user`
  (batched Q&A, defaults). System prompt = program contract + corpus seed
  (`agent/corpus.py`). CLI REPL: `python -m cadio.cli chat`.
- **Web workspace — the primary surface** (`cadio/api/app.py` + `frontend/`,
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

**Current direction (agreed 2026-07-03): `docs/ROADMAP.md` — Track A
(projects & persistence) → Track B (Engine-1 professionalization: corpus
depth, agent render-critique "eyes", geometry import) → Track C (Blender
engine for detailed/stylized).** The frontend quality plan
(`docs/FRONTEND-PLAN.md`) continues underneath; its 2.1 sessions item is
superseded by Track A.

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

**2026-07-17 — build UX overhaul: narration, budgets, staged building.**
Response to real-user pain (castle: 3 invalid versions + a stuck viewer;
streamdeck case on a weak model: 9 invalid versions with a silent spinner):
- **Live narration** (`orchestrator.py` → WS `status` events): every build
  carries `attempt n/max`; a failed attempt emits `state:"fixing"` with a
  plain-language reason (`_FRIENDLY_FAILURES`) — the user watches "attempt 2
  needs fixes — the model has gaps · reworking…" instead of a dead spinner.
- **Build budget** (`CADIO_MAX_BUILDS_PER_TURN`, default 5): at the cap the
  agent is refused further builds and instructed to wrap up honestly; two
  ignored refusals hard-stop the turn with a canned honest reply.
- **Repeat-failure escalation**: the same validation code failing 2× injects a
  targeted corrective into the tool result; 3× forces simplify-and-stage.
- **Staged building** (corpus): complex models (>3 parts) must build massing
  first (validated), then features in 2–4-item batches — user sees the shape
  in seconds and weak models handle small deltas (the Gemini-Flash fix).
- **Viewer honesty** (`Viewer.tsx`): the silent `.catch(() => {})` that caused
  the infinite "Loading model…" is now an error card with Retry + a 45s stall
  timeout + a download % readout.
- **Size-relative STL tolerance** (`_sandbox_runner.py`): chord tolerance
  0.02–0.15mm scaled by model diagonal instead of fixed 1e-3 — castle-class
  test went to 0.6MB/11.6k tris; export/validate/render/download all shrink.
- **No affect precompute for invalid runs** (`app.py _run_valid`), and the
  `/affect` endpoint no longer re-queues rebuilds of invalid runs.
- **Chat**: consecutive invalid builds collapse into one "N attempts needed
  rework" chip (expandable) instead of a wall of red Invalid cards.

**2026-07-06 — parameter → affected-geometry highlighting.** Click a parameter
in the Params panel and the faces it controls glow cyan in the viewer.
Mechanism (`cadio/affect.py`): there's no stored param→face mapping (the number
flows through arbitrary build123d code), so we discover it empirically — nudge
the one parameter, rebuild (warm preview, ~20-50ms), and diff the meshes.
Symmetric diff via trimesh nearest-surface (needs `rtree`): base faces that
MOVED (wall/hole size) plus base faces nearest to geometry the perturbation
ADDED/REMOVED (height/count growing the model), so extend-type params are
caught too. Face indices are in STL facet order = the browser STLLoader's
triangle order, so the viewer recolours them directly (per-vertex colours).
Precomputed in a background thread on every save → `affect.json` beside the
artifacts; `GET /…/runs/{id}/affect` serves the cache or computes on demand.
Highlight applies only to the stable saved model, not live slider previews.

**2026-07-03 — Track A (projects/persistence) + Track B (Engine-1 pro).**
*Track A:* SQLite store (`cadio/store.py`: projects/messages/runs/assets, WAL,
single-writer lock); all data under `~/.cadio/projects/<pid>/`; project-scoped
API; **websocket conversation resume** — the orchestrator's LLM history is
rebuilt from stored messages via one serializer (`cadio/api/history.py`) that
also drives UI scrollback, so the agent genuinely remembers across reload and
restart. React project **home + router** (`/`, `/p/<pid>`), inline project
rename, SPA-fallback route. Legacy flat runs/assets auto-migrate into an
"Unsorted imports" project (idempotent, timestamps + files preserved).
*Track B:* **B1** corpus depth — verified recipes for revolve, shell, loft,
sweep (perpendicular-plane), fillet/chamfer, polar/grid patterns, splines
(each executed through the engine before entering the corpus); program
contract now blesses builder mode. **B2 agent eyes** — `cadio/render.py`
renders 4 views (matplotlib Agg, headless) per non-preview run; the
orchestrator feeds them to vision models as a post-build user message so the
agent critiques SHAPE before presenting (gated by `litellm.supports_vision`;
render.png doubles as the project thumbnail; render messages are transient,
not persisted). **B3** STEP/STL reference import — `inspect.py` measures
bbox/volume/bore diameters; `inspect_geometry` agent tool; uploads accept
geometry files; attachments split into vision-images vs inspectable-geometry.

**2026-07-03 — stop control + icon/alignment pass.** Agent turns are now
interruptible: ws `{"type":"stop"}` → cooperative cancel in the orchestrator
(checked between LLM calls and per tool call; pending tool calls get a
"cancelled" result so history stays valid; a blocked ask_user is unblocked
with no answers). Stop pill appears next to the thinking indicator. Emoji
chrome replaced with an inline SVG icon set (Icons.tsx); composer redesigned
as a single input card (textarea + icon bar + send); all controls normalized
to a 32px rhythm; alignment verified programmatically via Playwright
(bounding-box centers) plus the stop flow tested end to end.

**2026-07-03 — 1.3 design pass + critical /assets shadowing fix.**
Design system (layered surfaces, sans UI type + mono for technical values,
tokens, transitions, styled scrollbars); top app bar (wordmark, connection
badge, model chip); resizable panes (react-resizable-panels v3: Group/Panel/
Separator API); markdown rendering for agent messages (react-markdown);
toasts; per-pane error boundaries; welcome/empty states everywhere; animated
thinking dots. **Playwright visual check caught a real breakage:** the
reference-image mount at `/assets` shadowed Vite's built bundle (also under
`/assets/*`) → blank page. Uploads now serve from `/refs/` (urls derived from
filenames at read time, so old metas remap automatically). Screenshot pixel
check confirms the dark theme renders as designed.

**2026-07-03 — image references + disconnect fix + auto-reconnect.**
- **Root-caused the mid-conversation disconnects:** `uvicorn --reload` watches
  the whole repo, so the engine writing `runs/*/program.py` triggered a server
  restart, killing the websocket (visible in the user's log). Fix: `python -m
  cadio.cli serve --reload` watches only the `cadio/` package. Frontend also
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

- **OCCT's STL writer emits zero-area triangles at sphere/revolve poles** —
  their self-edges register as boundary edges, so a naive trimesh
  `is_watertight` fails on a *perfect* Sphere(). This false positive sent the
  agent through 18 futile redesigns of an engraved ball. The validator now
  merges vertices + drops degenerate faces before judging (reported as a
  warning), while genuinely open meshes still fail. Corpus told not to
  redesign around a watertight failure that the cleaner handles, and gained
  the engraving recipe (`extrude(Text(...), amount=...)`, overshoot the cut).

- **Runtime data must live outside the repo** (`~/.cadio`, override
  `CADIO_HOME`). The first "fix" for reload-disconnects was a safer serve
  command — but users keep typing the command they know (`uvicorn --reload`),
  which watches the repo and restarted the server whenever the engine wrote
  `runs/*/program.py`. A fix that requires changing habits isn't a fix; now
  nothing is written inside the repo at runtime, so any command is safe.
  (Legacy `runs/`, `assets/`, `.sandbox_home/` auto-migrate on first import.)
- **Keep the Vite dev proxy in sync with backend mounts.** When Track A moved
  artifacts/refs to `/files` and previews to `/previews`, the `vite.config.ts`
  proxy still only listed `/api`,`/runs`,`/ws` — so in `npm run dev` (:5173)
  every image AND the 3D model 404'd (they load fine on the built app at :8000
  where the mounts are native). Any new backend URL prefix must be added to
  the dev proxy.
- **Never mount user content at `/assets`** — Vite emits the app bundle under
  `/assets/*`; an earlier static mount silently shadows it and the page goes
  blank with only 404s in the console. Reference uploads live at `/refs/`.
- **API-level tests can't catch route-shadowing/visual breakage** — the
  Playwright screenshot check (frontend devDependency) is what found it; keep
  using it after frontend-affecting backend changes.

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
