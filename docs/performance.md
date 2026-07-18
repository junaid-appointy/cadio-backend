# CADIO — performance, reliability & scalability

> Consolidated reference for everything we've built to make CADIO fast, stable,
> and able to serve many users on a small box. Written 2026-07-18. Deep-dives
> live in `reliability.md` (deploy/OOM/CPU incidents) and `PROJECT.md` (dated
> build log); this doc is the cross-cutting "what's implemented and why" index.
>
> The whole system is designed around one hard constraint: the CAD kernel
> (build123d / OCCT) is **heavy** — ~360MB resident per process, ~3–8s to import,
> single-threaded, and it never returns freed pages to the OS. Almost every
> decision below follows from treating a kernel process as a scarce, expensive,
> leaky resource that must be pooled, bounded, and kept off the request path.

---

## 1. Performance

### Build path (the CAD kernel)
- **Warm worker pool** (`engines/precision/pool.py`). Resident `python -I`
  workers keep the kernel pre-imported and serve jobs over stdio, so a rebuild
  costs only the geometry work — **≈20ms warm vs ≈6s cold**. This is the
  backbone of realtime slider tweaking. A one-shot cold subprocess remains as a
  fallback when the pool can't field a worker (boot failure / saturation).
- **Persistent sandbox HOME** (`.sandbox_home/`) keeps OCCT's on-disk caches
  warm across jobs and restarts.
- **Preview builds skip STEP + GLB** (`preview=True`) and use **coarse STL
  tessellation** (`coarse=True`) — a throwaway slider preview or an affect diff
  doesn't need a fine mesh. Coarse tessellation yields **~20× fewer facets**
  (41K→2K) for affect diffs.
- **O(1) face resolution** (`_sandbox_runner.py`): the linear `IsSame` scans that
  mapped triangles → BREP faces became an O(1) `TopTools_IndexedMapOfShape`
  resolver (FORWARD-orientation normalized to match `IsSame`, verified
  byte-identical), plus a bbox pre-filter in `_classify_by_solids`.
- **Kernel kept out of the API process.** STEP inspection (`import_step`) used to
  load OCCT (~360MB, permanent) into the web process; it now runs in a
  short-lived `python -I` subprocess. The renderer reuses the already-validated
  mesh instead of re-reading the STL (one in-memory mesh copy per build).

### Rendering (agent "eyes" + thumbnails)
- **Render cost cut 4.3s → 1.74s** on a 270K-facet mesh (`render.py`): decimate
  to `CADIO_RENDER_MAX_FACES` (18K) with `fast_simplification`, 512px, the
  object-oriented matplotlib Figure API, fixed axes (no `bbox_inches="tight"`
  relayout), sequential (matplotlib is GIL-bound / not thread-safe). Section view
  slices the decimated proxy, not the full mesh.
- **Render tiers: `full | thumbnail | none`.** The full 5-view set is only
  produced when a vision model will actually critique it; manual Code-tab runs
  render just the iso thumbnail (**~1/5 the render CPU**); feature edges are
  computed once per mesh.

### Viewer / frontend
- **Incremental recolor** (`Viewer.tsx`): the color buffer is painted once per
  model; hover/selection/highlight repaint **only the changed facets** via a
  `faceToFacets` map + diff, instead of rebuilding ~2.4M floats every hover tick.
- **`frameloop="demand"`**: an idle CAD canvas costs ~0 CPU/GPU (no 60fps loop
  for identical frames). Every imperative mutation — recolor, brush ring, paint
  strokes, camera fit — calls `invalidate()` to request exactly one frame.
- **Adaptive display-mesh refinement**: already-dense sources (>250K facets) skip
  refinement entirely (facets map 1:1); sparse sources refine up to
  `min(600K, 4×src)` for brush granularity. Saves ~45MB of buffers on dense
  models.
- **Code-splitting** (`vite.config.ts`): the Workspace is lazy-loaded and
  three/@react-three are a manual chunk — the **landing route is 73KB gzip
  (was 381KB)**; the ~250KB three chunk loads only on project open and caches
  separately.

### Network / serving
- **GZip** middleware at level 6, but **off** for `/files` and `/previews`.
- **Final STLs are precompressed once at build** (`model.stl.gz`) and served with
  `Content-Encoding: gzip` + a real `Content-Length` (so the viewer progress bar
  works), zero per-request compression CPU. Decoded bytes are identical → facet
  order preserved.
- **Immutable caching**: content-addressed run artifacts under `/files` get
  `Cache-Control: immutable, max-age=1y`.

### Data layer
- **SQLite tuned for concurrent reads** (`store.py`): per-thread connections
  (`threading.local`) + WAL + `busy_timeout` so reads run concurrently; writes
  serialize on `_wlock`. `list_projects` N+1 (a COUNT per project) collapsed to
  one `LEFT JOIN … GROUP BY`.
- **Bounded LLM context**: history replay is windowed to
  `CADIO_HISTORY_MAX_TURNS` (40), sliced on a user boundary to keep
  tool_call/result pairing valid; the system prompt is cached. Per-stage
  `timings` / `rss_peak_mb` / `stl_facets` are stripped from LLM payloads.

---

## 2. Reliability

### Process lifecycle
- **No `--reload` in prod** (reload is opt-in as of 2026-07-10). `--reload` tore
  the server down on every edit — dropping every WebSocket (code 1012),
  recycling the worker pool, and killing in-flight affect jobs (the `202`
  storm). See `reliability.md` for the full failure-mode writeup.
- **Graceful shutdown** (lifespan + `atexit`): on SIGTERM/deploy the app drains
  in-flight builds up to `SHUTDOWN_DRAIN_S` (15s), closes sockets with code 1001
  "going away" (not the abrupt 1012), and kills the worker pool via
  `engine.shutdown()` so **no OCCT subprocess is ever orphaned**.
- **Conversation resume**: chat history persists in SQLite; on reconnect the
  client reloads scrollback and the server rebuilds the agent's LLM context from
  stored messages. A restart is transparent except for a single in-flight turn.
- **Session decoupled from the socket** (`ProjectSession`, `_sessions` registry):
  an agent turn + its build slot run in a session-owned daemon thread, not bound
  to a WebSocket. A refresh mid-build no longer orphans the turn or leaks the
  build slot (which used to cause false "a build is already running"); on
  reconnect the client re-attaches and the reply routes to the new socket.
- **Client auto-reconnect** (`useChat.ts`): exponential backoff, then a
  "reconnect via settings" prompt; shows "conversation restored" on success.
- **Stable sessions**: `CADIO_SESSION_SECRET` keeps the signed cookie valid
  across restarts (otherwise a per-boot key signs everyone out on each restart).

### Worker-pool health
- **The pool actually boots now.** `_worker_loop.py` once shadowed stdlib
  `inspect` with a same-named module in its own dir, silently breaking the
  build123d import so *every* build fell back to the cold path (a fresh
  ~360MB/3–8s kernel each time). Fixed: module renamed, worker dir appended (not
  prepended) to `sys.path`, worker stderr captured into the raised error, cold
  fallback logs a warning. *Never name a module after a stdlib one in that dir.*
- **No OOM amplifier.** A job that kills/times-out its worker returns an
  instructive "simplify your program" error to the agent; the replacement worker
  respawns **lazily on next use** (not stacked on the dying one at peak memory),
  and the same job is **never re-run cold** (that repeats the damage). The cold
  path is reserved for pool *boot* failure, where the job isn't the suspect.
- **RSS-creep recycling**: OCCT never returns freed pages, so a worker retires
  after `CADIO_WORKER_RECYCLE_RSS_MB` (700) RSS or `CADIO_WORKER_RECYCLE_JOBS`
  (80) jobs — a fresh ~360MB floor instead of unbounded creep.
  `MALLOC_ARENA_MAX=2` curbs glibc arena fragmentation in this threaded process.
- **Affect-sweep crash-loop guard** (`affect.py`, 2026-07-18): a sweep aborts
  after `CADIO_AFFECT_MAX_FAILS` (3) *consecutive* failed per-parameter builds.
  Without it, a model too heavy for the box fails every parameter and the sweep
  grinds the whole manifest re-booting a fresh worker each time — which pegged
  the 0.75-CPU pod and starved interactive previews (see `reliability.md`).

### Memory safety (the 2GB-cgroup discipline)
- **Per-worker `RLIMIT_DATA`** (`CADIO_WORKER_MAX_DATA_MB`, 1800MB ≈ 1.2–1.3GB
  RSS): a runaway build dies alone with an instructive `MemoryError` instead of
  the cgroup SIGKILL'ing the whole pod.
- **Memory-pressure admission** (`CADIO_MEM_PRESSURE_PCT`, 80): with another
  kernel job in flight and the working set high, background/preview jobs shed
  (recomputable / stale-able) while a second *interactive* build waits its turn —
  two near-rlimit workers at once is the one spike a 2GB cgroup can't survive.
- **Global shed** (`CADIO_MAX_QUEUE_DEPTH` 6, `CADIO_SHED_MEM_PCT` 90): past the
  queue depth or cgroup %, new work is rejected immediately with an honest
  message instead of everyone silently queueing into 30s-timeout territory.
- **Constrained mode**: a pod whose cgroup limit is under
  `CADIO_MIN_BUILD_MEM_MB` (1024) never boots the pool and refuses builds with
  "mid-deploy" messaging (`/healthz` shows `engine.constrained: true`) — the
  512MB deploy size-flip window used to accept builds and OOM.

### Self-tuning to the pod (2026-07-18)
Reliability shouldn't depend on remembering to hand-set env vars on every
environment. The engine now reads the pod's **granted** CPU from the cgroup
(`cpu.max`, via `cgroup_cpus()`) and right-sizes itself:
- **`CADIO_POOL_SIZE` unset** → 1 worker on a sub-`CADIO_LOW_CPU_THRESHOLD`
  (1.25) core pod, else 2. Two kernel builds can't run in parallel on <1 core;
  a second worker there only oversubscribes and starves interactive previews.
- **`CADIO_AFFECT_PRECOMPUTE` unset** → background affect precompute defaults
  **off** on a low-CPU pod (the lazy `/affect` path still builds the map
  on-click), **on** otherwise.
- Both env vars still force the behavior explicitly when set. On Bifrost `heavy`
  (0.75 CPU) the pod now self-selects 1 worker + no background sweeps.

---

## 3. Scalability

### Scheduling & concurrency
- **Single hard concurrency gate.** A priority-aware gate in `PrecisionEngine`
  (width = pool size) bounds **every** kernel execution — warm, cold, preview,
  affect. It's the one place total concurrent OCCT processes are capped.
- **Priority gate** (`_PriorityGate`): interactive callers (agent build, slider
  preview) always beat background ones (affect sweeps); FIFO within each class.
  Background work acquires a slot only when no interactive caller is queued, and
  its worker is niced to 10 in containers so it yields the shared cores. An
  affect sweep can never delay a human at the *gate* (it can't preempt an
  already-running background job — hence the low-CPU self-tuning above).
- **Affect precompute is deferred and deduped**: superseded mid-sweep by newer
  runs, and collapsed to **one sweep for the final valid run** at end-of-turn
  instead of up to five during a turn; concurrency capped at
  `CADIO_AFFECT_CONCURRENCY` (pool−1). Affect never runs on a request thread —
  that once starved the pool and hung `/affect`.

### Backpressure & honesty (degrade, don't hang)
- Agent builds emit `queued (n ahead)` WS status straight from the gate's live
  queue position.
- `/api/preview` fast-fails **503 + Retry-After** at
  `CADIO_PREVIEW_GATE_TIMEOUT_S` (4s) — a stale slider value isn't worth a 30s
  park; the frontend silently retries the latest value.
- Per-user **daily build quota** + a **single in-flight build slot** per user +
  per-endpoint rate limits keep one user from monopolizing the box.

### Per-tier sizing
- `CADIO_POOL_SIZE` right-sizes resident workers (and the gate) per deployment
  tier without a rebuild; it also self-tunes off granted CPU (§2).
- Full capacity-knob table (RLIMIT, recycle thresholds, pressure/shed percents,
  timeouts) lives in `reliability.md` → "Capacity tuning knobs".

### Current single-node limits & the scale-out path
CADIO today is **one process, `workers=1`**, load-bearing because the CAD pool,
rate-limit buckets, and affect-dedupe are all in-process. Scaling *out* is not
"more uvicorn workers" — it needs:
- a **job queue** (kernel builds become queued work items, not in-process calls),
- **Postgres** for projects/runs/sessions (SQLite is single-node),
- an **object store** for artifacts (already R2-backed via `/files`
  write-through cache — the facemap/affect/select endpoints share the
  `_run_file()` R2 fallback so a fresh container serves non-empty pick maps),
- externalized rate-limit + build-slot state (Redis).

The **namespace quota is the real ceiling** on a single node: the Bifrost `dev`
env is 1.5 CPU / 3072Mi total across all services, so the `heavy` backend gets
0.75 CPU / 2048Mi. More headroom means raising that quota (dashboard) before
bumping tiers — see `reliability.md` for the tier/quota details and the
size-flip deploy workaround.

---

## 4. Observability (how we keep this data-driven)
- **`/healthz`** (unauthenticated, no DB/engine work so it stays green under
  load): `connections`, `inflight_builds`, `mem` (cgroup limit/usage/peak,
  `used_pct`), `cpu` (`cpus`, `nr_throttled`, `throttled_usec`), `engine` (gate
  depth, per-worker RSS + job counts, `constrained`), `affect_jobs`. The signals
  that reveal trouble: `nr_throttled` rising (CPU-bound), `gate.in_flight` pinned
  with `inflight_builds: 0` (background affect wedged), a worker's `jobs` stuck
  at 0 while pids cycle (a crash-loop), `affect_jobs` not draining.
- **Per-run telemetry**: every saved run carries `timings` (per stage) +
  `rss_peak_mb` + `stl_facets` in its meta, so optimization stays data-driven.
- **`scripts/loadsim.py`**: canned-program builds + preview bursts + affect
  polling (no LLM keys) that prints percentiles; container harness is
  `docker run --memory=2g --memory-swap=2g --cpus=1.5`.

---

## See also
- `reliability.md` — deploy flow, the OOM incident, the CPU-starvation incident,
  Bifrost tiers/quota, the full capacity-knob table.
- `PROJECT.md` — dated build log with the original context for each change.
- `storage-decision.md` — SQLite → Postgres/object-store scale-out reasoning.
- `SCALE-ANALYSIS.md` — 100k-user sizing/cost model (peak concurrent builds is
  the scaling alarm; BYO LLM keys → $0 inference).
- `selection-accuracy.md`, `artifact-strategy.md` — pick-map + artifact details.
