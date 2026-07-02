"""Executed INSIDE the sandbox subprocess — not imported by the host.

Usage: python -I _sandbox_runner.py <program.py> <params.json> <outdir>

Loads the program, merges param overrides onto PARAMS defaults, calls
build(params), exports STL + STEP, and writes result.json with measured
geometry facts (bbox, volume) so the host validates real numbers, not hopes.
"""

import importlib.util
import json
import sys
import traceback
from pathlib import Path


def load_program(path: Path):
    spec = importlib.util.spec_from_file_location("forma_program", path)
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
    for name, value in overrides.items():
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


def main() -> int:
    program_path, params_json, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    result = {"ok": False}
    try:
        overrides = json.loads(Path(params_json).read_text()) if params_json != "-" else {}
        mod = load_program(Path(program_path))
        declared = getattr(mod, "PARAMS", [])
        if not isinstance(declared, list):
            raise TypeError("PARAMS must be a list of parameter dicts")
        if not hasattr(mod, "build"):
            raise AttributeError("program must define build(params) -> Part")
        params = resolve_params(declared, overrides)

        part = mod.build(params)

        from build123d import export_step, export_stl

        stl_path = out / "model.stl"
        step_path = out / "model.step"
        export_stl(part, str(stl_path))
        export_step(part, str(step_path))

        bb = part.bounding_box()
        result.update(
            {
                "ok": True,
                "params": params,
                "manifest": declared,
                "bbox": {
                    "min": [bb.min.X, bb.min.Y, bb.min.Z],
                    "max": [bb.max.X, bb.max.Y, bb.max.Z],
                    "size": [bb.size.X, bb.size.Y, bb.size.Z],
                },
                "volume_mm3": float(part.volume),
                "artifacts": {"stl": str(stl_path), "step": str(step_path)},
            }
        )
    except Exception:
        result["error"] = traceback.format_exc()
    (out / "result.json").write_text(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
