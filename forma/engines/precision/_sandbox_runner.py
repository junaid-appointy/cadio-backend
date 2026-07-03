"""Executed INSIDE sandbox processes — not imported by the host.

Two entry points share `run_job`:
- CLI (cold path):   python -I _sandbox_runner.py <program.py> <params.json> <outdir>
- Worker (warm path): _worker_loop.py imports run_job and serves jobs over stdio.

`run_job` loads the program, merges param overrides onto PARAMS defaults,
calls build(params), exports STL (+ STEP unless preview), and returns measured
geometry facts (bbox, volume) so the host validates real numbers, not hopes.
"""

import importlib.util
import itertools
import json
import sys
import traceback
from pathlib import Path

_module_counter = itertools.count()


def load_program(path: Path):
    # unique module name per load: a warm worker serves many jobs and must
    # never hand one job a previous job's module
    name = f"forma_program_{next(_module_counter)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_params(declared: list, overrides: dict) -> dict:
    params = {}
    by_name = {}
    for spec in declared:
        name = spec["name"]
        by_name[name] = spec
        params[name] = spec["default"]
    for name, value in (overrides or {}).items():
        if name not in by_name:
            raise ValueError(f"unknown parameter {name!r}; declared: {sorted(by_name)}")
        spec = by_name[name]
        if spec.get("type", "number") in ("number", "integer"):
            value = float(value)
            if spec.get("type") == "integer":
                value = int(value)
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and value < lo:
                raise ValueError(f"{name}={value} below min {lo}")
            if hi is not None and value > hi:
                raise ValueError(f"{name}={value} above max {hi}")
        params[name] = value
    return params


def run_job(program: str, outdir: str, params: dict | None = None, preview: bool = False) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    result = {"ok": False}
    try:
        mod = load_program(Path(program))
        declared = getattr(mod, "PARAMS", [])
        if not isinstance(declared, list):
            raise TypeError("PARAMS must be a list of parameter dicts")
        if not hasattr(mod, "build"):
            raise AttributeError("program must define build(params) -> Part")
        resolved = resolve_params(declared, params or {})

        part = mod.build(resolved)

        from build123d import export_step, export_stl

        artifacts = {}
        stl_path = out / "model.stl"
        export_stl(part, str(stl_path))
        artifacts["stl"] = str(stl_path)
        if not preview:  # STEP is for final exports; skip on live previews
            step_path = out / "model.step"
            export_step(part, str(step_path))
            artifacts["step"] = str(step_path)

        bb = part.bounding_box()
        result.update(
            {
                "ok": True,
                "params": resolved,
                "manifest": declared,
                "bbox": {
                    "min": [bb.min.X, bb.min.Y, bb.min.Z],
                    "max": [bb.max.X, bb.max.Y, bb.max.Z],
                    "size": [bb.size.X, bb.size.Y, bb.size.Z],
                },
                "volume_mm3": float(part.volume),
                "artifacts": artifacts,
            }
        )
    except Exception:
        result["error"] = traceback.format_exc()
    return result


def main() -> int:
    program_path, params_json, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    overrides = json.loads(Path(params_json).read_text()) if params_json != "-" else {}
    result = run_job(program_path, outdir, overrides)
    (Path(outdir) / "result.json").write_text(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
