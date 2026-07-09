# CADIO roadmap — from vertical slice to professional tool

> Written 2026-07-03, agreed direction: **A → B → C.** This is the execution
> plan for the next phase; the product-level spec (shell + pluggable engines,
> domain map, business phases) remains `../../ai-3d-product-plan.md`. Check
> items off here as they land; keep `PROJECT.md` §0 as the source of truth for
> what is currently true.

## Vision (what this product must become)

A professional tool for creating **any type of 3D model with an AI agent** —
described in conversation, refined with reference images and questions, built
by pluggable geometry engines, tweaked in realtime, and exported in standard
formats. "Professional" concretely means:

1. **Nothing is ever lost.** All work lives in named projects: conversations,
   model versions, reference images, exports. Close the laptop, come back
   tomorrow, continue the same conversation.
2. **The agent works like a junior engineer, not a slot machine.** It gathers
   requirements, *sees* what it builds (renders → self-critique), verifies
   measured facts against the spec, and only then presents results.
3. **Breadth comes from engines, depth per engine.** Precision CAD (today),
   Blender for organic/stylized (Track C), generative meshes later — one
   vertical at a time, each professional-grade before the next.

### Capability map (honest, keep updated)

| Kind of model | Status | What unlocks it |
|---|---|---|
| Boxy functional parts (enclosures, brackets, trays) | ✅ works today | — |
| Curvy professional CAD (lofted shells, revolved vases, swept handles, fillet-heavy housings) | ✅ Track B done — corpus recipes + agent render-critique | (threads/knurls still thin; extend corpus as needed) |
| Detailed/stylized/organic (sculpted, textured, decorative, character-like) | ❌ not intentional for Engine 1 | **Track C** (Blender engine) |
| Artistic from-scratch generation (text→mesh) | ❌ deferred by design | Engine 3 (after C) |

---

## Track A — Projects & persistence  ✅ done 2026-07-03

**Problem.** Runs land in one flat global history; conversations live in
server RAM (reload/restart = amnesia); references are global. The user cannot
tell what belongs together, resume work, or share "a project."

### A1. Data layer — SQLite at `~/.cadio/cadio.db`

```sql
projects(id TEXT PK, name TEXT, created_at, updated_at, archived_at NULL)
messages(id INTEGER PK, project_id FK, role TEXT,            -- user|assistant|tool|system-event
         content JSON, created_at)                            -- content = our ChatItem-shaped payloads
runs(id TEXT PK, project_id FK, parent_run_id NULL,          -- tweak lineage -> version tree
     label TEXT, ok BOOL, meta JSON, created_at)             -- meta = today's meta.json payload
assets(id TEXT PK, project_id FK, file TEXT, name TEXT, mime TEXT, created_at)
```

- WAL mode; single writer via a small `store.py` module (stdlib `sqlite3`).
- Run/asset **files** stay on disk (`~/.cadio/projects/<id>/runs/...`,
  `.../refs/...`); DB holds metadata. meta.json files retired.
- **Migration:** existing flat `~/.cadio/runs/*` and `assets/*` move into an
  auto-created project named "Unsorted imports"; nothing is deleted.

### A2. Backend — project-scoped everything
- REST: `GET/POST /api/projects`, `PATCH /api/projects/{id}` (rename/archive),
  `GET /api/projects/{id}` (summary: counts, last activity, thumbnail run).
- Existing endpoints gain project scope: `/api/projects/{id}/runs`, `/assets`,
  `/execute`; artifact URLs become `/files/<project>/runs/...`.
- **WS session resume:** connect with `project_id`; server loads message
  history from DB, rebuilds the orchestrator's LLM history (text + image
  parts) so the agent genuinely remembers. Every user/assistant/tool event is
  written to `messages` as it happens. `stop`/reconnect/restart all survive.
- Orchestrator history and DB messages share one serialization (the sole
  source for both replay-to-LLM and replay-to-UI).
- Thumbnails: first valid run's GLB rendered small (or reuse viewer screenshot
  later); store path on project row.

### A3. Frontend — project flow
- **Home screen** (route `/`): project cards (name, thumbnail, last activity,
  model count) + "New project" + rename/archive. Workspace moves to
  `/p/<project-id>` (add `react-router` or a tiny hash router).
- Workspace header shows project name (editable inline); switcher menu.
- Chat loads history on open (scrollback = whole project conversation);
  Runs tab and reference library show only this project's items.
- Version tree v1: Runs tab groups tweaks under their parent run
  (`parent_run_id` set when Save-version derives from an agent run).

**Done when:** create two projects; build a model in each; reload the page and
restart the server; both conversations continue with full memory; each
project's Runs/references show only its own; old flat history appears under
"Unsorted imports".

---

## Track B — Engine 1 professionalization  ✅ done 2026-07-03 (B1 corpus, B2 eyes, B3 import)

**Problem.** Output looks "simple polygon" not because OCCT is weak but
because (a) the corpus only teaches box-level recipes, (b) the agent never
sees its geometry — numbers only, (c) no reference geometry can be imported.

### B1. Corpus depth — teach the operations OCCT is actually good at
Add recipe chapters (each: when-to-use + a tested build123d snippet):
- `revolve` (vases, knobs, pulleys), `sweep` along paths (handles, hooks,
  cable channels), `loft` between profiles (ergonomic shells, transitions),
  `shell`/`offset` (thin-wall enclosures from solids), fillet/chamfer
  strategy (order matters; fillet-after-boolean pitfalls), hole patterns
  (PolarLocations/GridLocations), helix + thread (bd_warehouse threads if
  available, else documented approximation), embossed/engraved text (already
  seeded), splines (`Spline`, `Bezier`) for freeform profiles.
- Each recipe validated through the engine once before it enters the corpus
  (no untested snippets — the corpus must never lie).
- Program contract: mention the richer toolbox explicitly so the agent stops
  defaulting to Box/Cylinder unions.

### B2. Agent eyes — render → self-critique loop (the big unlock)
- Engine post-step: render 4 canonical views (iso/front/top/side) of the
  mesh to PNGs per run. Implementation choice, in order of preference:
  1. `trimesh` scene + `pyrender`/pyglet offscreen (pure python),
  2. fallback: headless three.js via the existing Playwright install.
- Orchestrator: after a successful `run_cad`, attach the renders as image
  parts to the tool result (vision models only; capability-gate by model) so
  the agent *sees* the part, compares against the user's intent/references,
  and fixes visual problems before replying.
- New corpus process rule: "look at the renders; if the shape does not match
  the requirement or reference, fix it before presenting."
- Cost control: renders only on non-preview runs; downscale to ~512px.

### B3. Reference geometry import
- Upload STEP/STL as a project asset (extend asset types beyond images).
- New agent tool `inspect_geometry(asset_id)` → bbox, volume, key face
  planes/diameters (OCCT introspection) so "make a lid for this" works with
  measured truth instead of user-typed numbers alone.
- Viewer: show imported reference as a ghost overlay (later).

**Done when (benchmark set, each ≤3 agent attempts):** a revolved vase with a
lofted neck; a swept-handle mug; a shelled two-part enclosure with filleted
edges; a threaded cap fitting a threaded neck; and the agent visibly corrects
at least one flaw found via its own renders.

---

## Track C — Engine 2: Blender (detailed & stylized becomes real)

**Problem.** Organic/sculpted/stylized modeling is outside BREP CAD by
nature. The plan's answer is the generalist engine: headless Blender (bpy).

### C1. Engine plumbing (the contract earns its keep)
- `cadio/engines/blender/`: implements the same `Engine` interface —
  `execute(code, params, run_dir, preview)` runs a **bpy program**
  (`PARAMS` + `build(params)` building a scene/object), exports STL + GLB.
- Worker: persistent `blender --background --python worker_loop.py` process
  pool (same stdio-JSON protocol as the precision pool; imports cost ~2-4s
  once). Blender binary: autodetect (`/Applications/Blender.app/...`,
  `$CADIO_BLENDER`), clear error if missing.
- Validation: manifold check via existing mesh gate (print path) OR poly/
  material budget (asset path) — engine picks profile by declared intent.
- Checkpoint artifact: **turntable renders** (Blender EEVEE, 4–8 frames) fed
  through the same B2 self-critique loop.

### C2. Routing & UX
- Intent router v1: the orchestrator gains a second tool `run_blender`
  (system prompt describes both engines' domains: "precision engine for
  dimensional/functional; blender engine for organic/stylized/decorative");
  the model routes per request. Runs record which engine produced them;
  params panel works identically (same manifest format).
- Corpus: new bpy chapter (modifiers: subdivision, boolean, displace, bevel,
  arrays/curves; text-on-curve/wrap; sculpt-like displacement via textures;
  low-poly styles; export scale = mm).

### C3. Done when
- "A stylized vase with a twisted, ribbed surface" — precision engine would
  struggle; Blender run passes validation and looks right in turntable.
- "Text wrapped around a sphere" (the MONAWWAR case, curved for real).
- A low-poly decorative animal exports printable STL.
- The same project mixes engine-1 and engine-2 versions with the shell
  unchanged — proving the pluggable-engine bet.

---

## Later (unchanged from product plan)
Engine 3 (hosted text→3D as starting meshes, refined in Blender); cross-engine
composition; share links; print-service integration; multi-user/auth.

## Sequencing & rough effort
| Track | Size | Depends on |
|---|---|---|
| A projects/persistence | ~3–5 sessions | — |
| B1 corpus depth | ~1–2 sessions | — (parallel-safe with A) |
| B2 agent eyes | ~2 sessions | renderer choice |
| B3 geometry import | ~1–2 sessions | A (assets per project) |
| C blender engine | ~4–6 sessions | A (homes), B2 (critique loop) |

## Risks
- **Render loop cost/latency** (B2): mitigate with view count, resolution,
  non-preview-only; measure token impact per turn.
- **Blender install friction** (C): autodetect + settings override + crisp
  error; document brew cask install.
- **SQLite concurrency** (A): single-writer discipline in store.py; WAL; the
  worker pool never touches the DB.
- **History replay drift** (A): one serializer for LLM-history and UI-history,
  round-trip tested.
