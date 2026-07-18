# CADIO — reliability & deployment

> Written 2026-07-10. How to run CADIO so connections stay up and restarts are
> clean. TL;DR: **never run the deployed app with `--reload`**, put it behind a
> supervisor that restarts on crash, and probe `/healthz`.

## The failure mode this doc exists to prevent

Symptom seen in local dev: WebSockets dropping every few seconds
(`ws … closed by client code=1012`), a wall of `GET …/affect → 202` that never
resolves, and repeated `MallocStackLogging` lines. **All three are one event:**
uvicorn `--reload` tearing the whole server down on every source edit.

Each reload simultaneously:
- drops every live WebSocket (**code 1012 = "Service Restart"**),
- recycles the CAD worker pool (→ the benign macOS `MallocStackLogging` lines
  are just freshly-spawned worker subprocesses; `pool_size=2` → two per spawn),
- kills in-flight background jobs — e.g. the param→face **affect map**, whose
  endpoint then answers `202 "still computing"` forever because the task that
  would finish it was killed. The client polls ~8× and gives up.

`--reload` is a **development-only** convenience. It is not a bug — but it is
incompatible with a stable, reliable service.

## Run modes

| Context | Command | Reload |
|---|---|---|
| Deployed beta / prod | `cadio serve --host 0.0.0.0 --port 8000` | **off** (default) |
| Local development | `cadio serve --reload` | on (opt-in) |

As of 2026-07-10 reload is **opt-in**: `cadio serve` runs with **no reload** by
default; add `--reload` only while actively editing source. This makes the
platform reliable by default and confines connection drops to explicit dev use.

## What makes restarts clean (built in)

- **Graceful shutdown** (`app.py` lifespan + `atexit`): on SIGTERM / reload /
  deploy the app (1) waits up to `SHUTDOWN_DRAIN_S` (15s) for in-flight builds
  to finish, (2) closes each live socket with **code 1001 "going away"** (not
  the abrupt 1012) after a status nudge, (3) kills the CAD worker pool via
  `engine.shutdown()` so **no OCCT subprocess is ever orphaned**. (Before
  2026-07-10 `WorkerPool.shutdown()` existed but was never called — every
  restart leaked two worker processes.)
- **Conversation resume**: chat history is persisted in SQLite; on (re)connect
  the client reloads scrollback (`fetchHistory`) and the server rebuilds the
  agent's LLM context from stored messages (`app.py`, `orch.set_history`). A
  restart is transparent except for a single in-flight agent turn.
- **Client auto-reconnect** (`useChat.ts`): exponential backoff up to
  `MAX_RECONNECT`, then a "reconnect via settings" prompt. On success it shows
  "conversation restored".
- **Session survival**: set `CADIO_SESSION_SECRET` so the signed session cookie
  survives restarts (otherwise a random per-boot key signs everyone out on each
  restart — see the login flow in `auth.py`). Required in any real deployment.

## Health probe

`GET /healthz` — unauthenticated, does no DB or engine work (stays green under
load), returns `{"ok": true, "connections": N, "inflight_builds": N}`. Point
your load balancer / supervisor liveness check here.

## Reliable deploy checklist

1. **No `--reload`.** Plain `cadio serve` (reload off) or `uvicorn cadio.api.app:app`.
2. **`workers=1`.** Load-bearing until the in-process CAD pool, rate-limit
   buckets, and affect-dedupe are externalized — see `storage-decision.md`.
   Scaling out means a job queue + Postgres + object store, not more workers.
3. **Supervisor that restarts on crash** — systemd (`Restart=always`), a Docker
   restart policy (`restart: unless-stopped`), or the platform's equivalent.
   Send **SIGTERM** to stop (triggers the graceful drain above); allow a
   stop-timeout ≥ `SHUTDOWN_DRAIN_S` (≥ ~20s) so builds drain before SIGKILL.
4. **Set `CADIO_SESSION_SECRET`** (stable, secret) so sessions survive restarts.
5. **Liveness probe → `/healthz`.**
6. **Backups**: Litestream sidecar for the SQLite DB + periodic `tar` of
   `~/.cadio/projects` (see `storage-decision.md`).

## Memory discipline (the 2GB-container OOM incident, 2026-07-16)

The deployed backend (Bifrost `heavy` tier = **2048MB cgroup limit** per
`/healthz` `mem.limit_mb` — not the 3GB the instance size suggests; no swap)
crash-looped under
use while the same code ran fine on an 8GB MacBook (macOS absorbs pressure with
swap + compression; a cgroup answers with SIGKILL). Root causes, all fixed:

1. **The warm worker pool never booted — ever.** `_worker_loop.py` put its own
   directory at `sys.path[0]` to import `_sandbox_runner`, and that directory
   contained `inspect.py`, which **shadowed stdlib `inspect`** and broke the
   build123d import chain. The failure was invisible: worker stderr went to
   `DEVNULL` and the engine silently fell back to the cold path. Net effect:
   *every* build (agent, preview slider, per-parameter affect rebuild) spawned
   a fresh ~360MB / 3–8s `python -I` kernel process.
   Fixes: renamed to `measure.py`; worker dir is now **appended** to sys.path;
   worker stderr is captured into the raised error; the cold fallback logs a
   warning. *Never name a module in that directory after a stdlib module.*
2. **Unbounded kernel concurrency.** `/api/preview` had only a rate limit (120/
   min) and no build slot; with the pool dead, a slider drag spawned concurrent
   cold kernels until the cgroup OOM-killed the pod. Fix: a hard semaphore in
   `PrecisionEngine` (size = pool size) gates **every** execution — warm, cold,
   preview, affect. Callers past the 30s gate timeout get a "server busy" error
   instead of a process.
3. **The OOM amplifier.** A job that killed/timed out its worker used to boot a
   replacement (+360MB) *and* re-run the same job cold (+360MB) — exactly when
   memory was tightest. Fix: worker death/timeout mid-job returns an
   instructive error to the agent (simplify the program); replacements respawn
   lazily on next use; the cold path is reserved for pool *boot* failure.
4. **RSS creep.** OCCT never returns freed pages, so workers now recycle after
   `CADIO_WORKER_RECYCLE_JOBS` (40) jobs. `MALLOC_ARENA_MAX=2` in the
   Dockerfile stops glibc arena fragmentation in this thread-heavy process.
5. **Kernel in the API process.** STEP inspection (`import_step`) used to load
   OCCT (~360MB, permanent) into the API process; it now runs in a short-lived
   `python -I` subprocess of `measure.py`. The renderer also reuses the
   validated mesh instead of re-reading the STL (one in-memory copy per build).

Measured floors (Python 3.12, this dependency set): API process ~220MB at boot
(litellm alone is ~130MB); each kernel worker ~360MB idle, spiking with
geometry. Budget for the 2GB tier: main + 2 workers ≈ 950MB steady state,
2-build concurrency cap, ~1GB headroom for geometry spikes. `/healthz` reports
cgroup limit/usage — watch `used_pct` after builds. Verified in a local
`docker run --memory=2g` of the production image: boot 8s, healthz 200,
warm pool jobs 20–40ms.

## CPU starvation: affect sweeps wedged the pod (2026-07-18)

Symptom: models built fine, but **parameter-slider changes never reflected** —
the "updating" spinner hung, or the preview completed and nothing changed. The
box was `heavy` (2048MB) but only **0.75 CPU**.

Diagnosis came straight from `/healthz` polled over ~75s:
- `engine.gate.in_flight` pinned at **1** the whole time (a kernel job always
  held a gate slot), while `inflight_builds: 0` (no user build).
- `engine.workers` cycled pids (75 → 79 …) with **`jobs: 0`** — no worker ever
  *completed* a job. A boot → run → die loop.
- `affect_jobs: 3` never drained; `mem` idle at ~47%; `cpu.nr_throttled` high.

Root cause: the **affect-map precompute** (nudge each parameter, rebuild, diff —
the "what does this knob change" highlight map) runs its kernel builds at
background priority, **niced to 10**. On 0.75 heavily-throttled cores a niced
build can't finish inside the 90s worker timeout, so it's killed and the sweep
skips to the next parameter (`continue`) — re-booting a fresh ~360MB worker each
time. Three queued sweeps ground the CPU forever. Interactive **previews** lost
because they have the shortest gate timeout (`CADIO_PREVIEW_GATE_TIMEOUT_S` = 4s)
and, once past the gate, competed for CPU with the crash-loop; agent builds still
won (30s gate wait + interactive priority), which is why "models load" but slider
tweaks didn't. The priority gate orders *queued* acquirers but can't preempt an
*in-flight* background job, so on an effectively-one-worker box a running affect
build still blocks a preview.

Fixes:
1. **Crash-loop guard** (`affect.py`): a sweep aborts after
   `CADIO_AFFECT_MAX_FAILS` (3) *consecutive* failed per-parameter builds — a
   too-heavy model no longer grinds the whole manifest re-booting workers. One
   success resets the counter.
2. **Eager-precompute kill-switch** (`app.py`): `CADIO_AFFECT_PRECOMPUTE=0`
   disables the background "sweep every param of every saved run" work. The lazy
   `/affect` path still builds the map on demand when a user clicks a parameter
   label, so highlighting keeps working without the background grind. Set this on
   any sub-1-CPU pod.
3. **Right-size**: on 0.75 CPU, two kernel workers oversubscribe <1 core —
   `CADIO_POOL_SIZE=1` matches workers to cores. Or move to `extra_heavy` for
   more CPU (read the actual grant from `/healthz` after; see tier table above).

## Deploying on Bifrost (read before pushing cadio-backend)

Flow: this repo (`cadio.git`) is the source of truth; the backend subset is
mirrored (rsync, minus `frontend/` and uncommitted docs) into
`cadio-backend.git`, whose pushes trigger the webhook build + deploy.

**Compute tiers (2026-07-18).** The ladder is `micro` < `standard` < `heavy` <
`extra_heavy` (`bifrost service create --help`). Bifrost stores only the tier
*label* on the service (`compute_size` in `bifrost service get`); it does **not**
publish the CPU/RAM each tier grants — the CLI has no command for it and the
catalog API returns 403. The only reliable way to learn a tier's real limits is
to deploy onto it and read the pod's cgroup back from `/healthz` (`mem.limit_mb`,
`cpu.cpus`). Measured this way:

| Tier | RAM (`mem.limit_mb`) | CPU (`cpu.cpus`) | Where |
|---|---|---|---|
| `standard` | 512MB | ? | (old backend window) |
| `heavy` | 2048MB | **0.75** | backend now |
| `micro` | ? | ? | frontend (nginx, no `/healthz` cgroup probe) |
| `extra_heavy` | ? | ? | never deployed — bump + read `/healthz` to learn |

**`heavy` is 0.75 CPU, not 1.5.** The "1.5 CPU" in older notes is the *namespace
total* quota shared across services, **not** what the heavy pod gets — the pod's
cgroup reports `cpu.cpus: 0.75`. Tuning that assumed 1.5 was sized for 2× the CPU
the pod actually has. This is the root of the affect-starvation incident below.
Backend is `heavy`, frontend is `micro` (confirmed via `bifrost service list`).

**The environment quota is the real ceiling (2026-07-18).** The `dev` namespace
(`env-c800a7f2-01c5048c-dev`, type `development`) has a **total** budget of
**1.5 CPU / 3072Mi / 30Gi**, shared across *all* services — visible only in the
dashboard (the `bifrost environment get` CLI doesn't return it, and there's no
CLI flag to change it; raising it is a dashboard/support action). Per-service
tier requests are bounded by this. Current split: backend `heavy` = 0.75 CPU /
2048Mi (half the CPU, ⅔ the RAM), leaving ~0.75 CPU / ~1024Mi for the frontend
`micro` plus any surge.

**Implication for `extra_heavy`:** it does **not** fit on this env. A bigger pod
alongside the frontend would exceed 1.5 CPU / 3072Mi, and a *rolling* deploy
(old `heavy` + new `extra_heavy` live at once) blows the quota outright — the same
mechanism as the "two `heavy` pods don't fit" wedge below, worse. So on the
current namespace the only way to give the backend more CPU is to **raise the
environment quota first** (dashboard), *then* bump the tier. Until then, the
software levers (`CADIO_AFFECT_PRECOMPUTE=0`, `CADIO_POOL_SIZE=1`) are the only
lever available.

**Rollout wedge (2026-07-16/17):** the namespace quota (3072Mi — see the
environment-quota note above) cannot hold two `heavy` pods at once (2×2048Mi >
3072Mi). A rolling update surges the new 2GB-request pod while the old
one still runs → the new pod never schedules → the deploy workflow times out
and reports `failed` (build `succeeded`, old pod keeps serving; even
`bifrost deployment restart` wedges the same way). Deploys only "worked"
historically when the old pod had just OOM-died. Until the platform gives this
service `strategy: Recreate` (or a bigger node), ship with the **size-flip
workaround**:

1. `bifrost service update backend --compute-size standard` (512MB request
   fits beside the old heavy pod), push/deploy → rollout succeeds, old heavy
   pod terminates. The 512MB pod can serve but NOT build (boot 220MB + worker
   360MB > 512MB) — keep this window short.
2. `bifrost service update backend --compute-size heavy`, then
   `bifrost deploy --service backend --environment dev` → the 2GB pod fits
   beside the 0.5GB pod → rollout succeeds. Verify `/healthz` shows
   `limit_mb: 2048` and a fresh (low) `rss_peak_mb`.

`CADIO_POOL_SIZE` (default 2) right-sizes kernel workers per tier without a
rebuild; it also caps total concurrent kernel processes (the exec gate).

**Size-flip automation (2026-07-17):** `scripts/deploy_backend.sh` runs the
whole flip (standard → deploy → heavy → deploy), polling `/healthz
mem.limit_mb` at each step and finishing with a smoke check. The 512MB window
is now safe: below `CADIO_MIN_BUILD_MEM_MB` (1024) the backend boots in
**constrained mode** — pool never boots, builds are refused with
"mid-deploy" messaging, `/healthz` shows `engine.constrained: true`. The
durable fix remains a platform `strategy: Recreate` for this service — ask
Bifrost; the script is the workaround until then.

## Capacity tuning knobs (2026-07-17; `heavy` = 2GB / **0.75 CPU** measured — not 1.5)

| Env | Default | What it bounds |
|---|---|---|
| `CADIO_POOL_SIZE` | 2 | resident kernel workers + exec-gate width |
| `CADIO_WORKER_MAX_DATA_MB` | 1800 | per-worker RLIMIT_DATA — runaway build dies alone (Linux); ≈1.2–1.3GB RSS (VmData ≈ RSS+550MB) |
| `CADIO_WORKER_RECYCLE_RSS_MB` | 700 | recycle a worker whose RSS crept past this |
| `CADIO_WORKER_RECYCLE_JOBS` | 80 | job-count recycle backstop (RSS is the real bound) |
| `CADIO_MEM_PRESSURE_PCT` | 80 | above this (working set, cache excluded) with another job in flight: background/preview jobs shed; a second INTERACTIVE build waits — two near-rlimit workers at once is the one spike the pod can't survive |
| `CADIO_AFFECT_MEM_PCT` | 70 | affect sweeps don't even start above this |
| `CADIO_AFFECT_PRECOMPUTE` | 1 | `0` disables background affect sweeps (lazy `/affect` still serves on click) — set on sub-1-CPU pods |
| `CADIO_AFFECT_MAX_FAILS` | 3 | consecutive failed per-param builds before a sweep aborts (crash-loop guard) |
| `CADIO_RENDER_MAX_FACES` | 18000 | render-proxy decimation (A/B 8000 before lowering fleet-wide) |
| `CADIO_PREVIEW_GATE_TIMEOUT_S` | 4 | preview fast-fail (503 + Retry-After, client retries latest value) |
| `CADIO_MAX_QUEUE_DEPTH` | 6 | queued interactive builds before global shed |
| `CADIO_SHED_MEM_PCT` | 90 | cgroup % that triggers global shed |
| `CADIO_MIN_BUILD_MEM_MB` | 1024 | below this cgroup limit the pod runs constrained (no builds) |

Watch `/healthz`: `mem.peak_mb` (worst spike vs limit), `cpu.nr_throttled`
(rising = the pod's 0.75-CPU share is the bottleneck), `engine.gate`
(waiting_interactive > 0 sustained = at capacity; `in_flight` pinned with
`inflight_builds: 0` = background affect wedged), `engine.workers[].rss_mb`
(recycling health; `jobs` stuck at 0 while pids cycle = a crash-loop),
`affect_jobs` (not draining = sweeps grinding). Every saved run carries `timings` (per
stage) + `rss_peak_mb` + `stl_facets` in its meta — optimization stays
data-driven; `scripts/loadsim.py` drives a synthetic load and prints the
percentiles.

Also: a deploy `failed` status is about the rollout, not the code — a
succeeded build + healthy local `docker run --memory=2g` means ship again,
don't hunt phantom bugs.

## Known gaps (future hardening)

- An agent turn in progress at the moment of restart is **interrupted**, not
  resumed — only completed turns are persisted. Resumable turns would need the
  orchestrator to checkpoint mid-turn.
- The **affect map** is recomputed per run in memory; a restart loses an
  in-progress computation (self-heals on the next request). Persisting or
  making it cheap-to-recompute would remove the transient `202` storm.
