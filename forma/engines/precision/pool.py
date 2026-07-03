"""Warm worker pool for the precision engine.

Cold subprocess execution pays ~3s of OCCT import per run; the pool keeps N
resident `python -I` workers with the kernel pre-loaded, so a rebuild costs
only the actual geometry work (sub-second for simple parts) — the backbone of
realtime parameter tweaking.

Same isolation level as the cold path (isolated mode, stripped env). Caveat
(documented): a worker serves many jobs, so a hostile program could poison its
own worker for later jobs. Acceptable for the local single-user phase; the
Docker sandbox (P1 of the product plan) supersedes this.
"""

from __future__ import annotations

import json
import os
import queue
import select
import subprocess
import sys
from pathlib import Path

_WORKER_SCRIPT = Path(__file__).parent / "_worker_loop.py"


class WorkerError(Exception):
    """Worker died or timed out — the job outcome is unknown."""


class _Worker:
    def __init__(self, scratch: Path, boot_timeout_s: float):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(scratch),  # persistent -> OCCT caches stay warm
            "TMPDIR": str(scratch),
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(_WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=scratch,
        )
        ready = self._read_line(boot_timeout_s)
        if not ready or not json.loads(ready).get("ready"):
            self.kill()
            raise WorkerError("worker failed to boot")

    def _read_line(self, timeout_s: float) -> str | None:
        assert self.proc.stdout is not None
        r, _, _ = select.select([self.proc.stdout], [], [], timeout_s)
        if not r:
            return None
        return self.proc.stdout.readline() or None

    def run(self, request: dict, timeout_s: float) -> dict:
        if self.proc.poll() is not None:
            raise WorkerError("worker is dead")
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerError(f"worker pipe broken: {exc}") from exc
        line = self._read_line(timeout_s)
        if line is None:
            raise WorkerError(f"worker timed out after {timeout_s}s")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"worker returned garbage: {line[:200]}") from exc

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass


class WorkerPool:
    def __init__(self, size: int = 2, timeout_s: float = 90.0, boot_timeout_s: float = 180.0):
        from ...config import SANDBOX_HOME  # outside the repo — see config.py

        self.timeout_s = timeout_s
        self.boot_timeout_s = boot_timeout_s
        self.scratch = SANDBOX_HOME
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._idle: queue.Queue[_Worker] = queue.Queue()
        for _ in range(size):
            self._idle.put(_Worker(self.scratch, boot_timeout_s))

    def run(self, request: dict) -> dict:
        """Execute one job on an idle worker. Raises WorkerError on
        timeout/death (worker is replaced); the caller decides on fallback."""
        worker = self._idle.get()
        try:
            return worker.run(request, self.timeout_s)
        except WorkerError:
            worker.kill()
            worker = _Worker(self.scratch, self.boot_timeout_s)  # replace before re-raising
            raise
        finally:
            self._idle.put(worker)

    def shutdown(self) -> None:
        while not self._idle.empty():
            try:
                self._idle.get_nowait().kill()
            except queue.Empty:
                break
