"""Warm worker pool for the precision engine.

Cold subprocess execution pays ~3s of OCCT import per run; the pool keeps N
resident `python -I` workers with the kernel pre-loaded, so a rebuild costs
only the actual geometry work (sub-second for simple parts) — the backbone of
realtime parameter tweaking.

Memory discipline (the pool runs inside a ~3GB container):
- each worker is ~360MB resident just from the kernel import, and OCCT's
  allocator never returns freed pages to the OS, so a long-lived worker's RSS
  creeps upward across jobs. Workers are therefore RECYCLED after
  `recycle_after` jobs (fresh 360MB floor) instead of living forever.
- a worker that dies mid-job is NOT replaced synchronously in the failure
  path (that stacked a booting replacement on top of the dying worker at the
  exact moment memory was tightest); the pool re-spawns lazily on next use.

Same isolation level as the cold path (isolated mode, stripped env). Caveat
(documented): a worker serves many jobs, so a hostile program could poison its
own worker for later jobs. Acceptable for the local single-user phase; the
Docker sandbox (P1 of the product plan) supersedes this.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import select
import subprocess
import sys
from pathlib import Path

_WORKER_SCRIPT = Path(__file__).parent / "_worker_loop.py"

log = logging.getLogger("cadio.pool")

# Jobs a worker serves before it is retired and respawned. Bounds OCCT/glibc
# RSS creep: the cost is one ~3s re-import per N jobs.
RECYCLE_AFTER_JOBS = int(os.environ.get("CADIO_WORKER_RECYCLE_JOBS", "40"))


class WorkerError(Exception):
    """Base: this job did not produce a result."""


class WorkerUnavailable(WorkerError):
    """The pool can't field workers at all (boot failure / saturation).
    The job itself is not suspect — a cold fallback is reasonable."""


class WorkerJobError(WorkerError):
    """The worker died or timed out WHILE RUNNING this job. The job is the
    prime suspect (kernel crash, runaway geometry, OOM) — do NOT blindly
    re-run it on the cold path; that repeats the damage."""


class _Worker:
    def __init__(self, scratch: Path, boot_timeout_s: float):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(scratch),  # persistent -> OCCT caches stay warm
            "TMPDIR": str(scratch),
        }
        self.jobs_served = 0
        # stderr is captured to a pipe so a boot/crash failure carries its
        # traceback into the raised error — a silent DEVNULL here once hid a
        # worker that NEVER booted (see _worker_loop.py) for weeks.
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(_WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=scratch,
        )
        ready = self._read_line(boot_timeout_s)
        if not ready or not json.loads(ready).get("ready"):
            err = self._drain_stderr()
            self.kill()
            raise WorkerUnavailable(f"worker failed to boot: {err or 'no output'}")

    def _read_line(self, timeout_s: float) -> str | None:
        assert self.proc.stdout is not None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout_s)
        if not r:
            return None
        return self.proc.stdout.readline() or None

    def _drain_stderr(self, limit: int = 2000) -> str:
        """Best-effort tail of the worker's stderr (non-blocking) for diagnostics."""
        try:
            assert self.proc.stderr is not None
            chunks: list[str] = []
            while True:
                r, _, _ = select.select([self.proc.stderr], [], [], 0.2)
                if not r:
                    break
                data = os.read(self.proc.stderr.fileno(), 65536).decode(errors="replace")
                if not data:
                    break
                chunks.append(data)
            return "".join(chunks)[-limit:].strip()
        except Exception:
            return ""

    def run(self, request: dict, timeout_s: float) -> dict:
        if self.proc.poll() is not None:
            raise WorkerJobError(f"worker is dead: {self._drain_stderr()}")
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerJobError(f"worker pipe broken: {exc}") from exc
        line = self._read_line(timeout_s)
        if line is None:
            if self.proc.poll() is not None:  # died mid-job (crash / OOM-kill)
                raise WorkerJobError(
                    f"worker process died running the job: {self._drain_stderr()}")
            raise WorkerJobError(f"worker timed out after {timeout_s}s")
        try:
            result = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerJobError(f"worker returned garbage: {line[:200]}") from exc
        self.jobs_served += 1
        return result

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass


class WorkerPool:
    def __init__(self, size: int = 2, timeout_s: float = 90.0, boot_timeout_s: float = 180.0,
                 recycle_after: int = RECYCLE_AFTER_JOBS):
        from ...config import SANDBOX_HOME  # outside the repo — see config.py

        self.timeout_s = timeout_s
        self.boot_timeout_s = boot_timeout_s
        self.recycle_after = max(1, recycle_after)
        self.scratch = SANDBOX_HOME
        self.scratch.mkdir(parents=True, exist_ok=True)
        # slots, not workers: a slot may hold None, meaning "boot lazily on
        # next use". Killing a worker never spawns its replacement eagerly —
        # replacement memory is paid when the next job needs it, not while the
        # previous worker is still dying.
        self._idle: queue.Queue[_Worker | None] = queue.Queue()
        # boot ONE worker eagerly (fail fast + absorb the import cost up
        # front); the rest boot lazily so idle footprint starts at one kernel.
        self._idle.put(_Worker(self.scratch, boot_timeout_s))
        for _ in range(size - 1):
            self._idle.put(None)

    def run(self, request: dict) -> dict:
        """Execute one job on an idle worker. Raises WorkerUnavailable when no
        slot frees up (saturation) and WorkerJobError when the job itself
        killed/timed out its worker; the caller decides on fallback."""
        # bounded wait for a free slot: workers are always returned in `finally`
        # and each job self-times-out, so this only waits out genuinely in-flight
        # jobs. The ceiling is a safety net against a slot leaking un-returned
        # (a caller thread dying mid-job) so we fail instead of hanging forever.
        try:
            worker = self._idle.get(timeout=self.timeout_s * 2 + 10)
        except queue.Empty as exc:
            raise WorkerUnavailable("no idle worker available (pool saturated or stalled)") from exc
        try:
            if worker is None:  # lazy slot — boot on demand
                worker = _Worker(self.scratch, self.boot_timeout_s)
            return worker.run(request, self.timeout_s)
        except WorkerError:
            if worker is not None:
                worker.kill()
            worker = None  # lazy respawn on the slot's next use
            raise
        finally:
            if worker is not None and worker.jobs_served >= self.recycle_after:
                log.info("recycling worker after %d jobs", worker.jobs_served)
                worker.kill()
                worker = None
            self._idle.put(worker)

    def shutdown(self) -> None:
        while not self._idle.empty():
            try:
                w = self._idle.get_nowait()
                if w is not None:
                    w.kill()
            except queue.Empty:
                break
