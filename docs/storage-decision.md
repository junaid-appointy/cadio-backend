# CADIO — storage & scaling decision

> Written 2026-07-10. Answers "do we need Postgres + object storage, and when?"
> **Decision: no — not for the beta.** Stay on **SQLite (WAL) + local disk on a
> named volume**. This doc records *why*, the exact **move triggers**, and the
> **~1-day migration path** so the change is a known task, not a surprise.
> Source-of-truth for current state stays `PROJECT.md §0`.

## TL;DR

- Storage is **not** CADIO's scaling constraint — **CPU-bound CAD compute is**.
  Each build spins an OCCT/build123d subprocess (seconds of work), served by a
  small warm pool *inside the single web process*.
- SQLite handles the metadata volume of **1000+ users** without trouble.
  Local disk handles the artifacts until they get large (~50 GB) or need a CDN.
- Postgres + object storage become **mandatory the moment you need a second
  machine** (compute node or web instance) — because two boxes can't share a
  SQLite file or a local disk. That trigger is driven by **peak concurrent
  builds, not registered-user count.**
- Do **not** push this onto users' own devices as the primary model: the native
  OpenCASCADE/OCP/build123d install is too heavy for a frictionless product.

## What we store today

| Layer | Today | Holds |
|---|---|---|
| Metadata DB | **SQLite** (`~/.cadio/*.db`, WAL) | users, projects, conversations, runs, assets |
| Artifacts | **Local disk** (`/data/projects`) | STL/STEP/GLB files, renders |

- All DB access goes through the **`Store`** class (`cadio/store.py`): one shared
  `sqlite3` connection guarded by a `threading.Lock`, WAL so reads never block.
  The CAD worker pool never touches the DB.
- Artifacts are written by the engine and served by exactly one authenticated
  route; URLs are shaped in two functions. → S3 later = presigned URLs there.

These clean seams are the whole reason the migration is cheap.

## Why the constraint is compute, not storage

CAD builds are CPU-bound subprocesses (`cadio/engines/precision/`): ~3s just for
the OCCT import, seconds total per build, served by a warm pool of ~2 workers in
the single web process. With **BYO LLM keys**, the expensive inference is already
offloaded to users — so the server's scarce resource is **CPU for geometry**,
which caps *concurrent builds*, not total users. One box with N cores builds
~N models at once, full stop.

### Storage sizing (why SQLite/disk are fine at 100–1000 users)

- **Metadata (SQLite):** text rows for users/projects/conversations/runs. Even
  1000 users × hundreds of projects × many turns ≈ tens–low-hundreds of MB.
  SQLite handles millions of rows. **Data volume never forces Postgres.**
- **Artifacts (disk):** an STL is ~0.5–10 MB. 1000 users × ~100 models × ~2 MB
  ≈ **~200 GB** — *this* can outgrow a VPS disk, but the fix is a bigger volume
  or S3, and it grows slowly. Not urgent.

## Move triggers (the actual decision rules)

| Trigger | Action |
|---|---|
| Still one process, tens of concurrent builds | **Nothing** — bigger multi-core box, `workers=1`, SQLite + disk |
| Need a **2nd compute/web instance** (peak concurrency exceeds one box) | **Postgres + object storage become mandatory** (shared state) |
| Artifacts > ~50 GB, or CDN needed for renders | **Object storage (S3)** for artifacts |
| Serverless/Vercel target (no persistent local disk) | Postgres + S3 (no single SQLite file to keep) |

**Watch peak concurrent builds, not signups.** Registered-user count barely
matters; simultaneous CAD compute is the scaling alarm.

## Scaling by tier

1. **Now → ~100s of users, low concurrency:** SQLite + disk, one box. Add
   nothing. The seams already make the later port ~1 day.
2. **Sustained high concurrency:** move CAD builds onto a **job queue with
   dedicated, autoscaled worker machines**; keep the web tier thin. This is the
   cheapest scaling lever and it naturally pulls in Postgres (job/metadata state
   shared across boxes) + S3 (artifacts workers write and the web reads) — i.e.
   exactly the architecture 1000+ concurrent users wants.
3. **Prerequisite for any multi-box step:** externalize the in-process state
   first — the **CAD pool, rate-limit buckets, affect-dedupe** (e.g. Redis).
   Postgres is a *prerequisite* for `workers>1`, **not** by itself enough to
   unlock horizontal scaling.

## Hosted vs. "leave it to the user's device"

CADIO began single-user local-first, so self-hosting-per-user is tempting ($0
infra, infinite scale). **Rejected as the default** because the native
OpenCASCADE/OCP/build123d dependency turns "open a URL and type a prompt" into
"install a ~1 GB toolchain," which kills a consumer beta — and you lose sharing,
central updates, and analytics. **Keep the hosted web app as the front door;**
optionally ship a desktop build (Tauri/Electron) later for power users.

## Neon / hosted-Postgres migration path (~1 day, contained to `store.py`)

Recorded so it's a known task, not a surprise:

- **Portability already baked in:** `upsert_user` is conflict-upsert logic
  (SQLite `ON CONFLICT` → Postgres `INSERT … ON CONFLICT (google_sub) DO
  UPDATE`), `count_runs_today` uses a standard `created_at >= ?` range (no
  SQLite date() funcs), and new autoincrement reads avoid `lastrowid`. So the
  port is mostly type/placeholder swaps — no rework of plan-added logic.
- **The non-trivial part:** replace the single persistent connection +
  `threading.Lock` with a **`psycopg_pool` connection pool** (mandatory — Neon
  autosuspends and drops idle connections, and a global lock negates Postgres
  concurrency). Neon's pooled endpoint is PgBouncer transaction-mode;
  psycopg3 is compatible.
- **What it does NOT do:** unlock horizontal scaling by itself — `workers=1`
  stays until the in-process CAD pool + rate-limit buckets + affect-dedupe are
  also externalized.

## Backups (do this now, on SQLite + disk)

- Optional **Litestream** sidecar (commented in compose) streaming the DB.
- Periodic **`tar` of `/data/projects`** for artifacts.
- Cost today: **$0 beyond the VPS.**

## Cost posture

Nothing beyond the VPS until a move trigger fires. Postgres/S3 add cost and
operational surface for **zero benefit** on a single instance — add them when
concurrency forces a second machine, not before.
