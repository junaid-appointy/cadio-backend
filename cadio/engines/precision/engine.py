"""Engine 1 — Precision (build123d on OCCT).

Executes agent- or user-written build123d programs in sandboxed processes,
exports STL/STEP, converts to GLB for the browser viewer, and runs the
validation gate (mesh + geometry sanity).

Execution paths:
- warm (default): a resident worker pool with the kernel pre-imported —
  sub-second rebuilds, which is what makes realtime param tweaks possible.
- cold (fallback): one-shot `python -I` subprocess, used when the pool is
  unavailable or a worker dies mid-job.

`preview=True` runs skip STEP export and GLB conversion (the viewer renders
STL) — validation still runs so live feedback stays honest.

Sandboxing (v0): isolated-mode subprocesses, stripped env, wall clock
timeout. TODO(P1): no-network Docker with resource limits (plan §6.3).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..base import ExecutionResult, ParamSpec, ValidationReport
from ...validation.mesh import validate_mesh
from .pool import WorkerJobError, WorkerPool, WorkerUnavailable, memory_limit_preexec

_RUNNER = Path(__file__).parent / "_sandbox_runner.py"

log = logging.getLogger("cadio.engine")

# Longest a request waits for a kernel slot before giving up with a "busy"
# error. Previews queue briefly behind an in-flight build instead of piling on.
_GATE_TIMEOUT_S = float(os.environ.get("CADIO_EXEC_GATE_TIMEOUT_S", "30"))

# Above this cgroup memory usage, a SECOND concurrent kernel job is admitted
# only if it's interactive (agent build / user action). Background (affect) and
# preview jobs yield — they are recomputable and stale-able respectively.
_MEM_PRESSURE_PCT = float(os.environ.get("CADIO_MEM_PRESSURE_PCT", "80"))

# Below this cgroup limit the pod cannot host even one kernel worker
# (boot ~220MB + worker ~360MB). Seen during the deploy size-flip window
# (a 512MB pod serving while the heavy pod terminates) — refuse builds
# honestly instead of OOM-dying mid-boot.
_MIN_BUILD_MEM_MB = int(os.environ.get("CADIO_MIN_BUILD_MEM_MB", "1024"))

# error text the affect loop keys off to skip-and-let-the-lazy-path-retry
MEM_PRESSURE_ERROR = "deferred: the server is under memory pressure"

_CGROUP = Path("/sys/fs/cgroup")


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
        return None if text == "max" else int(text)
    except Exception:
        return None


def cgroup_mem_limit_mb() -> int | None:
    """The pod's cgroup-v2 memory limit in MB (None outside a limited cgroup)."""
    limit = _read_int(_CGROUP / "memory.max")
    return None if limit is None else limit // (1024 * 1024)


def cgroup_cpus() -> float | None:
    """CPU cores the pod's cgroup actually grants (cpu.max = "quota period"), or
    None when unlimited/unreadable (a dev laptop). This is the number the pod
    gets, NOT the namespace quota — on Bifrost `heavy` it reads 0.75, and the
    self-tuning defaults below key off it so an underpowered pod right-sizes
    itself without hand-set env vars."""
    try:
        quota, period = (_CGROUP / "cpu.max").read_text().split()[:2]
        if quota == "max":
            return None
        return round(int(quota) / int(period), 2)
    except Exception:
        return None


# Below this many granted cores a second resident kernel worker only
# oversubscribes the CPU (two OCCT builds can't run in parallel on <1 core) and
# background affect sweeps can't finish before their worker times out — so the
# pool defaults to 1 worker and eager affect precompute defaults off. Both are
# still overridable via CADIO_POOL_SIZE / CADIO_AFFECT_PRECOMPUTE.
_LOW_CPU_THRESHOLD = float(os.environ.get("CADIO_LOW_CPU_THRESHOLD", "1.25"))


def low_cpu_pod() -> bool:
    """True when the pod's granted CPU is under the single-worker threshold."""
    cpus = cgroup_cpus()
    return cpus is not None and cpus < _LOW_CPU_THRESHOLD


_mem_pct_cache: tuple[float, float | None] = (0.0, None)
_mem_pct_lock = threading.Lock()


def cgroup_mem_pct() -> float | None:
    """Current cgroup memory usage as % of the limit, cached 250ms (it's read
    on every kernel-job admission). None when not in a limited cgroup (dev)."""
    global _mem_pct_cache
    now = time.monotonic()
    with _mem_pct_lock:
        ts, val = _mem_pct_cache
        if now - ts < 0.25:
            return val
        current = _read_int(_CGROUP / "memory.current")
        limit = _read_int(_CGROUP / "memory.max")
        if current and limit:
            # memory.current counts reclaimable page cache (every artifact
            # write inflates it); subtract inactive_file so the gate reacts to
            # the working set — the number the OOM killer actually acts on —
            # not to cache the kernel would happily drop.
            try:
                for line in (_CGROUP / "memory.stat").read_text().splitlines():
                    if line.startswith("inactive_file "):
                        current = max(0, current - int(line.split(" ", 1)[1]))
                        break
            except Exception:
                pass
        val = None if not current or not limit else round(100.0 * current / limit, 1)
        _mem_pct_cache = (now, val)
        return val


class _PriorityGate:
    """Counted gate over kernel slots where interactive callers always beat
    background ones. Replaces the plain BoundedSemaphore so an affect-map
    sweep can never delay a human: background waiters acquire only when no
    interactive caller is queued. FIFO within each class; `on_wait` reports
    the caller's live queue position (number of jobs ahead) for honest UI."""

    def __init__(self, size: int):
        self._size = size
        self._cond = threading.Condition()
        self._in_flight = 0
        self._seq = itertools.count()
        self._queues: dict[int, list[int]] = {0: [], 1: []}  # 0=interactive, 1=background

    def acquire(self, priority: int = 0, timeout: float | None = None,
                on_wait: Callable[[int], None] | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        ticket = next(self._seq)
        q = self._queues[priority]
        with self._cond:
            q.append(ticket)
            last_reported = None
            try:
                while True:
                    ahead = q.index(ticket) + (len(self._queues[0]) if priority else 0)
                    if ahead == 0 and self._in_flight < self._size:
                        q.remove(ticket)
                        self._in_flight += 1
                        return True
                    if on_wait is not None and ahead != last_reported:
                        last_reported = ahead
                        try:
                            on_wait(ahead)  # must be fast/non-blocking (called under the gate lock)
                        except Exception:
                            pass
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        q.remove(ticket)
                        self._cond.notify_all()  # our departure may unblock a background waiter
                        return False
                    self._cond.wait(remaining)
            except BaseException:
                if ticket in q:
                    q.remove(ticket)
                self._cond.notify_all()
                raise

    def release(self) -> None:
        with self._cond:
            if self._in_flight <= 0:
                raise ValueError("gate released more times than acquired")
            self._in_flight -= 1
            self._cond.notify_all()

    def depth(self) -> dict[str, int]:
        with self._cond:
            return {
                "in_flight": self._in_flight,
                "waiting_interactive": len(self._queues[0]),
                "waiting_background": len(self._queues[1]),
            }

PROGRAM_CONTRACT = """\
A precision-engine program is a Python file using build123d (algebra mode) that defines:

1. `PARAMS` — a list of parameter dicts, one per user-tweakable value:
   {"name": "length", "default": 60.0, "type": "number", "min": 10, "max": 500,
    "unit": "mm", "description": "Outer length", "group": "Size"}
   Every dimension that came from the user's requirements MUST be a parameter,
   never a magic number inside build().
   Parameter names MUST be unique (a duplicate name fails the build). For a
   REPEATED feature (4 wheels, 6 holes, N switches) declare ONE integer count
   parameter plus SHARED dimension parameters (e.g. `wheel_count`, `wheel_diameter`)
   and place the instances in a loop inside build() — never `wheel_1_*`,
   `wheel_2_*`. Give numeric params a real `min`/`max` so the UI shows a slider,
   and a per-feature `group` so related knobs cluster.

2. `build(params: dict) -> Part` — pure function from parameter values to a
   single solid. Units are millimetres. Two styles, both fine (return a Part):
   - algebra mode: `Box(...)`, `Cylinder(...)`, `Pos(x,y,z)*shape`, `+`/`-`,
     `fillet(...)`, `chamfer(...)` — good for boxy assemblies.
   - builder mode: `with BuildPart() as part: ...; return part.part` — needed
     for revolve, sweep, loft, shell/offset, patterns, splines (see RECIPES).
   Prefer the operation that matches the SHAPE; don't force everything into
   box/cylinder unions.
   Assert requirement facts inside build() with plain `assert` statements
   (e.g. `assert cavity_depth >= 32.4, "clearance under plate"`) so violated
   requirements fail loudly instead of producing wrong geometry.

3. `features(part, params) -> dict[str, shape]` — STRONGLY RECOMMENDED for any
   model with more than one visually distinct part. Names the parts a user points
   at, so a click in the viewer resolves to a stable name ("make `bow` bigger")
   that survives rebuilds and scopes the edit to THAT part.
   PREFERRED FORM — name each CONSTRUCTION SUB-SOLID. Build every part as its own
   named solid, union them in build(), and return those same solids here. The
   runner assigns each final face to the solid whose surface it lies on:
     def build(params):
         head  = Pos(0, 0, 70) * Sphere(30)
         torso = Pos(0, 0, 35) * Sphere(38)
         bow   = Pos(0, -34, 52) * Box(16, 6, 10)
         return head + torso + bow           # + ears, arms, legs...
     def features(part, params):
         # rebuild (or factor out) the same named solids and return them
         return {"head": head, "torso": torso, "bow": bow, ...}
   Values may also be a Face, a ShapeList, or a list of Faces (classic form).
   NAME BY WHAT THE PART IS, NEVER BY POSITION. Do NOT map a name to "every face
   between z=20 and z=50" — a positional band swallows whatever else sits there
   (this mislabeled a teddy bear's bow as its torso and smoothed the wrong part).
   Symmetric/repeated parts may share ONE name (the viewer auto-qualifies them
   "left"/"right"/"front"/… by position) or be named individually. Name every
   salient part generously; leave only incidental faces unnamed.

The runner (not your code) handles export, measurement, and validation.
Do not import anything except build123d, math, and dataclasses.
"""


class PrecisionEngine:
    id = "precision"
    domains = ["functional_parts"]

    def __init__(self, timeout_s: float = 90.0, pool_size: int | None = None,
                 use_pool: bool = True):
        # CADIO_POOL_SIZE tunes resident kernel workers (~360MB each) per
        # deployment tier without a rebuild: 1 for a 1GB container, 2 for 2GB+.
        # It also sizes the exec gate (total concurrent kernel processes). When
        # unset, the pod self-tunes off its GRANTED cpu (cpu.max): a sub-1.25-core
        # pod (e.g. Bifrost heavy = 0.75 CPU) defaults to 1 worker so two builds
        # can't oversubscribe less than a core and starve interactive previews.
        if pool_size is None:
            env = os.environ.get("CADIO_POOL_SIZE")
            if env is not None:
                pool_size = max(1, int(env))
            else:
                pool_size = 1 if low_cpu_pod() else 2
        self.timeout_s = timeout_s
        self._pool: WorkerPool | None = None
        self._pool_size = pool_size
        self._use_pool = use_pool
        self._pool_lock = threading.Lock()
        # Constrained mode: the deploy size-flip parks a 512MB pod in front of
        # users for a minute; it can serve chat/metadata but a single kernel
        # worker would OOM it. Refuse builds honestly and never boot the pool.
        limit = cgroup_mem_limit_mb()
        self.constrained = limit is not None and limit < _MIN_BUILD_MEM_MB
        if self.constrained:
            log.warning("cgroup limit %sMB < %sMB — builds disabled (constrained mode)",
                        limit, _MIN_BUILD_MEM_MB)
        # HARD ceiling on concurrent kernel processes — warm, cold, previews,
        # affect builds, everything. Each OCCT process is ~360MB idle and can
        # spike past 1GB on real geometry, so in a ~3GB container unbounded
        # concurrency (e.g. a burst of slider previews on the cold path) is an
        # OOM. The pool bounds its own workers, but the cold path had no limit.
        # Interactive callers always outrank background (affect) ones.
        self._exec_gate = _PriorityGate(pool_size)

    def program_contract(self) -> str:
        return PROGRAM_CONTRACT

    # ---- execution --------------------------------------------------------

    def execute(
        self,
        code: str,
        params: dict[str, Any] | None,
        run_dir: Path,
        preview: bool = False,
        coarse: bool = False,
        renders: str = "full",  # "full" | "thumbnail" | "none" — see _postprocess
        priority: int = 0,  # 0 = interactive (human waiting), 1 = background (affect)
        gate_timeout_s: float | None = None,
        on_wait: Callable[[int], None] | None = None,  # live queue position (jobs ahead)
    ) -> ExecutionResult:
        # absolute: subprocesses run with cwd inside the sandbox, so relative
        # paths would resolve inside themselves
        t0 = time.perf_counter()
        run_dir = Path(run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)

        if self.constrained:
            return ExecutionResult(
                ok=False, run_dir=run_dir,
                error="the server is mid-deploy and can't build right now — builds resume in a minute",
            )

        program_path = run_dir / "program.py"
        program_path.write_text(code)

        request = {
            "program": str(program_path),
            "outdir": str(run_dir),
            "params": params or {},
            "preview": preview,
            # coarse STL tessellation — only for throwaway diff builds (affect
            # maps), where a fine mesh is wasted: far fewer triangles to export
            # and to run proximity queries against.
            "coarse": coarse,
            # niced by the pool so a background kernel job yields CPU to a
            # concurrent interactive one (matters at 1.5 shared cores)
            "_background": priority > 0,
        }

        timeout = _GATE_TIMEOUT_S if gate_timeout_s is None else gate_timeout_s
        if not self._exec_gate.acquire(priority=priority, timeout=timeout, on_wait=on_wait):
            return ExecutionResult(
                ok=False, run_dir=run_dir,
                error="the server is busy building other models — try again in a moment",
                duration_s=round(time.perf_counter() - t0, 2),
            )
        gate_wait_s = round(time.perf_counter() - t0, 3)
        try:
            # Pressure admission: with another kernel job already in flight and
            # cgroup memory high, don't stack a second geometry spike on it.
            # Background/preview jobs shed immediately (recomputable / stale
            # anyway). An INTERACTIVE build instead WAITS its turn: the one
            # thing the per-worker rlimit cannot save the pod from is two
            # near-cap workers at once (2 × ~1.3GB + the API process exceeds a
            # 2GB cgroup) — under pressure the box degrades to single-build
            # throughput instead of gambling the pod.
            pct = cgroup_mem_pct()
            if (pct is not None and pct > _MEM_PRESSURE_PCT
                    and self._exec_gate.depth()["in_flight"] > 1):
                if priority > 0 or preview:
                    return ExecutionResult(
                        ok=False, run_dir=run_dir,
                        error=MEM_PRESSURE_ERROR,
                        duration_s=round(time.perf_counter() - t0, 2),
                    )
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    pct = cgroup_mem_pct()
                    if pct is None or pct <= _MEM_PRESSURE_PCT:
                        break
                    if self._exec_gate.depth()["in_flight"] <= 1:
                        break  # the other job finished; its memory is freed/freeing
                else:
                    return ExecutionResult(
                        ok=False, run_dir=run_dir,
                        error="the server is busy building other models — try again in a moment",
                        duration_s=round(time.perf_counter() - t0, 2),
                    )
            raw = self._execute_gated(program_path, params, run_dir, request)
        finally:
            self._exec_gate.release()
        if raw is None:
            return ExecutionResult(
                ok=False, run_dir=run_dir,
                error=f"execution failed or timed out after {self.timeout_s}s",
                duration_s=round(time.perf_counter() - t0, 2),
            )

        result = self._postprocess(raw, run_dir, preview, renders)
        result.timings["gate_wait_s"] = gate_wait_s
        result.duration_s = round(time.perf_counter() - t0, 2)  # honest wall clock the user waited
        return result

    def _execute_gated(self, program_path: Path, params: dict[str, Any] | None,
                       run_dir: Path, request: dict) -> dict | None:
        """Run one job while holding an exec-gate slot. Fallback policy:
        - pool unavailable (boot failure / saturation): cold path — the job is
          not suspect, only the pool is.
        - worker died or timed out RUNNING the job: NO cold re-run. The job is
          the prime suspect (kernel crash / runaway geometry / OOM); repeating
          it cold doubles the damage at the worst possible moment. The agent
          gets an instructive error and writes a simpler program instead."""
        if self._use_pool:
            try:
                return self._get_pool().run(request)
            except WorkerJobError as exc:
                log.warning("job killed/timed out its worker (no cold re-run): %s", exc)
                return {"ok": False, "error": (
                    "the program crashed or overwhelmed the geometry kernel "
                    f"({exc}). Simplify it: fewer/cheaper boolean operations, "
                    "fillet fewer edges at once, and reduce feature counts.")}
            except WorkerUnavailable as exc:
                log.warning("worker pool unavailable, falling back to cold path: %s", exc)
        return self._run_cold(program_path, params, run_dir)

    def _get_pool(self) -> WorkerPool:
        with self._pool_lock:
            if self._pool is None:
                self._pool = WorkerPool(size=self._pool_size, timeout_s=self.timeout_s)
            return self._pool

    def shutdown(self) -> None:
        """Kill the warm worker subprocesses (idempotent). Wire this to app
        shutdown / process exit so reloads, restarts, and crashes don't orphan
        the OCCT workers — otherwise every reload leaks two python subprocesses."""
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown()
                self._pool = None

    def gate_depth(self) -> dict[str, int]:
        """Cheap, lock-light queue snapshot for admission control."""
        return self._exec_gate.depth()

    def stats(self) -> dict[str, Any]:
        """Live engine numbers for /healthz — what you watch to run this box
        near capacity: gate depth (queueing), per-worker RSS (recycle health).
        Never blocks: _pool_lock is held for the WHOLE first-worker boot in
        _get_pool, and a liveness probe must not hang behind it."""
        if not self._pool_lock.acquire(timeout=0.2):
            return {"constrained": self.constrained, "pool_size": self._pool_size,
                    "gate": self._exec_gate.depth(), "workers": "booting"}
        try:
            pool = self._pool
        finally:
            self._pool_lock.release()
        return {
            "constrained": self.constrained,
            "pool_size": self._pool_size,
            "gate": self._exec_gate.depth(),
            "workers": pool.stats() if pool is not None else [],
        }

    def _run_cold(
        self, program_path: Path, params: dict[str, Any] | None, run_dir: Path
    ) -> dict | None:
        params_path = run_dir / "params_in.json"
        params_path.write_text(json.dumps(params or {}))
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(run_dir),
            "TMPDIR": str(run_dir),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(_RUNNER), str(program_path), str(params_path), str(run_dir)],
                capture_output=True, text=True, timeout=self.timeout_s, env=env, cwd=run_dir,
                preexec_fn=memory_limit_preexec(),  # same rlimit as pool workers
            )
        except subprocess.TimeoutExpired:
            return None
        result_file = run_dir / "result.json"
        if not result_file.exists():
            return {"ok": False, "error": f"runner produced no result. stderr:\n{proc.stderr[-4000:]}"}
        return json.loads(result_file.read_text())

    # ---- post-processing ---------------------------------------------------

    def _postprocess(self, raw: dict, run_dir: Path, preview: bool,
                     renders_mode: str = "full") -> ExecutionResult:
        timings: dict[str, float] = dict(raw.get("timings") or {})
        rss_peak = raw.get("rss_peak_mb")
        if not raw.get("ok"):
            return ExecutionResult(ok=False, run_dir=run_dir,
                                   error=raw.get("error", "unknown error"),
                                   timings=timings, rss_peak_mb=rss_peak)

        manifest = [
            ParamSpec(**{k: v for k, v in spec.items() if k in ParamSpec.__dataclass_fields__})
            for spec in raw.get("manifest", [])
        ]
        artifacts = dict(raw["artifacts"])

        t = time.perf_counter()
        validation, glb, mesh = self._validate_and_convert(
            Path(artifacts["stl"]), raw, run_dir, make_glb=not preview
        )
        timings["validate_glb_s"] = round(time.perf_counter() - t, 3)
        if glb:
            artifacts["glb"] = str(glb)

        if not preview:
            # precompress the STL once (final runs only) so downloads serve the
            # .gz with zero request CPU and an honest Content-Length — replaces
            # per-download middleware gzip of megabytes of mesh. Best-effort.
            try:
                import gzip

                stl = Path(artifacts["stl"])
                t = time.perf_counter()
                (stl.parent / (stl.name + ".gz")).write_bytes(
                    gzip.compress(stl.read_bytes(), compresslevel=6))
                timings["stl_gz_s"] = round(time.perf_counter() - t, 3)
            except Exception:
                pass

        # naming-quality warnings from the sandbox part table (lazy/under-naming).
        # They're warnings, not errors — they don't flip validation.ok, but they
        # ride into the agent's tool result so it names parts individually next.
        from ..base import ValidationIssue

        for entry in raw.get("warnings", []) or []:
            try:
                code, msg = entry
            except (ValueError, TypeError):
                continue
            validation.issues.append(ValidationIssue("warning", str(code), str(msg)))

        # part-drift regression guard: a scoped edit ("thicker handle") that
        # silently moved or shrank UNRELATED parts shows up as a warning in the
        # agent's tool result, and the standing fix-validation-issues rule makes
        # it restore them (or confirm the change was requested). Never blocks.
        if not preview:
            try:
                from ...validation.drift import part_drift_issue

                drift = part_drift_issue(run_dir, raw.get("bbox"))
                if drift:
                    validation.issues.append(drift)
            except Exception:
                pass  # the guard must never break a build

        # renders are the agent's eyes + the project thumbnail — non-preview only.
        # Reuse the validated mesh instead of re-reading the STL: one in-memory
        # copy per build, not two (a detailed model's mesh is tens of MB).
        # renders_mode picks how many eyes: "full" (all views + section, for a
        # vision model that will actually look), "thumbnail" (iso only — keeps
        # the project thumbnail without paying ~5 matplotlib renders when no
        # vision model is attached / on manual Code-tab runs), "none".
        renders: dict[str, str] = {}
        if not preview and renders_mode != "none":
            from ...render import render_views

            t = time.perf_counter()
            only = ["iso"] if renders_mode == "thumbnail" else None
            renders = render_views(Path(artifacts["stl"]), run_dir, mesh=mesh, only=only)
            timings["render_s"] = round(time.perf_counter() - t, 3)

        return ExecutionResult(
            ok=True,
            run_dir=run_dir,
            params=raw["params"],
            manifest=manifest,
            artifacts=artifacts,
            renders=renders,
            bbox=raw["bbox"],
            volume_mm3=raw["volume_mm3"],
            validation=validation,
            timings=timings,
            rss_peak_mb=rss_peak,
            stl_facets=raw.get("stl_facets"),
        )

    def _validate_and_convert(
        self, stl_path: Path, raw: dict, run_dir: Path, make_glb: bool
    ) -> tuple[ValidationReport, Path | None, Any]:
        report, mesh = validate_mesh(stl_path, expected_bbox_size=raw["bbox"]["size"])
        glb_path: Path | None = None
        if make_glb and mesh is not None:
            try:
                glb_path = run_dir / "model.glb"
                mesh.export(str(glb_path))
            except Exception:
                glb_path = None
        return report, glb_path, mesh
