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

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..base import ExecutionResult, ParamSpec, ValidationReport
from ...validation.mesh import validate_mesh
from .pool import WorkerError, WorkerPool

_RUNNER = Path(__file__).parent / "_sandbox_runner.py"

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

    def __init__(self, timeout_s: float = 90.0, pool_size: int = 2, use_pool: bool = True):
        self.timeout_s = timeout_s
        self._pool: WorkerPool | None = None
        self._pool_size = pool_size
        self._use_pool = use_pool
        self._pool_lock = threading.Lock()

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
    ) -> ExecutionResult:
        # absolute: subprocesses run with cwd inside the sandbox, so relative
        # paths would resolve inside themselves
        t0 = time.perf_counter()
        run_dir = Path(run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
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
        }

        raw: dict | None = None
        if self._use_pool:
            try:
                raw = self._get_pool().run(request)
            except WorkerError:
                raw = None  # fall through to the cold path
        if raw is None:
            raw = self._run_cold(program_path, params, run_dir)
            if raw is None:
                return ExecutionResult(
                    ok=False, run_dir=run_dir,
                    error=f"execution failed or timed out after {self.timeout_s}s",
                    duration_s=round(time.perf_counter() - t0, 2),
                )

        result = self._postprocess(raw, run_dir, preview)
        result.duration_s = round(time.perf_counter() - t0, 2)  # honest wall clock the user waited
        return result

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
            )
        except subprocess.TimeoutExpired:
            return None
        result_file = run_dir / "result.json"
        if not result_file.exists():
            return {"ok": False, "error": f"runner produced no result. stderr:\n{proc.stderr[-4000:]}"}
        return json.loads(result_file.read_text())

    # ---- post-processing ---------------------------------------------------

    def _postprocess(self, raw: dict, run_dir: Path, preview: bool) -> ExecutionResult:
        if not raw.get("ok"):
            return ExecutionResult(ok=False, run_dir=run_dir, error=raw.get("error", "unknown error"))

        manifest = [
            ParamSpec(**{k: v for k, v in spec.items() if k in ParamSpec.__dataclass_fields__})
            for spec in raw.get("manifest", [])
        ]
        artifacts = dict(raw["artifacts"])

        validation, glb = self._validate_and_convert(
            Path(artifacts["stl"]), raw, run_dir, make_glb=not preview
        )
        if glb:
            artifacts["glb"] = str(glb)

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

        # renders are the agent's eyes + the project thumbnail — non-preview only
        renders: dict[str, str] = {}
        if not preview:
            from ...render import render_views

            renders = render_views(Path(artifacts["stl"]), run_dir)

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
        )

    def _validate_and_convert(
        self, stl_path: Path, raw: dict, run_dir: Path, make_glb: bool
    ) -> tuple[ValidationReport, Path | None]:
        report, mesh = validate_mesh(stl_path, expected_bbox_size=raw["bbox"]["size"])
        glb_path: Path | None = None
        if make_glb and mesh is not None:
            try:
                glb_path = run_dir / "model.glb"
                mesh.export(str(glb_path))
            except Exception:
                glb_path = None
        return report, glb_path
