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

## Known gaps (future hardening)

- An agent turn in progress at the moment of restart is **interrupted**, not
  resumed — only completed turns are persisted. Resumable turns would need the
  orchestrator to checkpoint mid-turn.
- The **affect map** is recomputed per run in memory; a restart loses an
  in-progress computation (self-heals on the next request). Persisting or
  making it cheap-to-recompute would remove the transient `202` storm.
