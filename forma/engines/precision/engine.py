"""Engine 1 — Precision (build123d on OCCT).

Executes agent- or user-written build123d programs in a subprocess sandbox,
exports STL/STEP, converts to GLB for the browser viewer, and runs the
validation gate (mesh + geometry sanity).

Sandboxing (v0): isolated-mode subprocess (`python -I`), stripped env, wall
clock timeout, dedicated output dir. TODO(P1): move to no-network Docker with
resource limits per ai-3d-product-plan.md §6.3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..base import ExecutionResult, ParamSpec, ValidationReport
from ...validation.mesh import validate_mesh

_RUNNER = Path(__file__).parent / "_sandbox_runner.py"

PROGRAM_CONTRACT = """\
A precision-engine program is a Python file using build123d (algebra mode) that defines:

1. `PARAMS` — a list of parameter dicts, one per user-tweakable value:
   {"name": "length", "default": 60.0, "type": "number", "min": 10, "max": 500,
    "unit": "mm", "description": "Outer length", "group": "Size"}
   Every dimension that came from the user's requirements MUST be a parameter,
   never a magic number inside build().

2. `build(params: dict) -> Part` — pure function from parameter values to a
   single solid (use build123d algebra mode: Box, Cylinder, Pos, Rot, fillet,
   chamfer, boolean +/-). Units are millimetres.
   Assert requirement facts inside build() with plain `assert` statements
   (e.g. `assert cavity_depth >= 32.4, "clearance under plate"`) so violated
   requirements fail loudly instead of producing wrong geometry.

The runner (not your code) handles export, measurement, and validation.
Do not import anything except build123d, math, and dataclasses.
"""


class PrecisionEngine:
    id = "precision"
    domains = ["functional_parts"]

    def __init__(self, timeout_s: float = 90.0):
        self.timeout_s = timeout_s

    def program_contract(self) -> str:
        return PROGRAM_CONTRACT

    def execute(
        self, code: str, params: dict[str, Any] | None, run_dir: Path
    ) -> ExecutionResult:
        # absolute: the subprocess runs with cwd=run_dir, so relative paths
        # passed to the runner would otherwise resolve inside themselves
        run_dir = Path(run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        program_path = run_dir / "program.py"
        program_path.write_text(code)
        params_path = run_dir / "params_in.json"
        params_path.write_text(json.dumps(params or {}))

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(run_dir),  # OCCT wants a writable HOME for caches
            "TMPDIR": str(run_dir),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(_RUNNER), str(program_path), str(params_path), str(run_dir)],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
                cwd=run_dir,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                ok=False, run_dir=run_dir,
                error=f"execution timed out after {self.timeout_s}s",
            )

        result_file = run_dir / "result.json"
        if not result_file.exists():
            return ExecutionResult(
                ok=False, run_dir=run_dir,
                error=f"runner produced no result. stderr:\n{proc.stderr[-4000:]}",
            )
        raw = json.loads(result_file.read_text())
        if not raw.get("ok"):
            return ExecutionResult(ok=False, run_dir=run_dir, error=raw.get("error", "unknown error"))

        manifest = [ParamSpec(**{k: v for k, v in spec.items() if k in ParamSpec.__dataclass_fields__})
                    for spec in raw.get("manifest", [])]
        artifacts = dict(raw["artifacts"])

        validation, glb = self._validate_and_preview(Path(artifacts["stl"]), raw, run_dir)
        if glb:
            artifacts["glb"] = str(glb)

        return ExecutionResult(
            ok=True,
            run_dir=run_dir,
            params=raw["params"],
            manifest=manifest,
            artifacts=artifacts,
            bbox=raw["bbox"],
            volume_mm3=raw["volume_mm3"],
            validation=validation,
        )

    def _validate_and_preview(
        self, stl_path: Path, raw: dict, run_dir: Path
    ) -> tuple[ValidationReport, Path | None]:
        report, mesh = validate_mesh(stl_path, expected_bbox_size=raw["bbox"]["size"])
        glb_path: Path | None = None
        if mesh is not None:
            try:
                glb_path = run_dir / "model.glb"
                mesh.export(str(glb_path))
            except Exception:
                glb_path = None
        return report, glb_path
