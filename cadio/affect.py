"""Parameter → affected-geometry mapping (the "what does this knob change" map).

For each parameter we can't look up which faces it controls — the number flows
through arbitrary build123d code. So we discover it empirically: nudge the one
parameter, rebuild, and diff the two meshes. Faces of the current model whose
surface moved are the ones that parameter affects.

The affected-face indices are in STL facet order, which matches the order the
browser's STLLoader produces — so face i on the backend is triangle i in the
viewer, and the frontend can recolour them directly.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def _perturbed_value(spec: dict, value: Any):
    """A meaningfully-different value for this parameter, or None if it can't be
    perturbed (strings). Stays within min/max, nudging down if at the top."""
    t = spec.get("type", "number")
    if t == "string":
        return None
    if t == "boolean":
        return not bool(value)
    v = float(value)
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and hi is not None:
        d = 0.08 * (hi - lo)
    else:
        d = max(1.0, 0.08 * abs(v)) if v else 1.0
    if t == "integer":
        d = max(1, round(d))
    up = v + d
    if hi is not None and up > hi:
        down = v - d
        if lo is not None and down < lo:
            return None  # no room to perturb
        return int(down) if t == "integer" else down
    return int(up) if t == "integer" else up


def _affected_faces(base_mesh, pert_mesh, threshold: float) -> list[int]:
    """Symmetric diff mapped onto base faces. Catches both faces that MOVED
    (wall thickness, hole size) and regions where the perturbation ADDED or
    REMOVED geometry (height/depth/count growing the model)."""
    import numpy as np

    faces = np.asarray(base_mesh.faces)
    affected: set[int] = set()

    # (1) base faces whose surface moved away from the perturbed surface
    _, d_base, _ = pert_mesh.nearest.on_surface(base_mesh.vertices)
    moved = (np.asarray(d_base) > threshold)[faces].any(axis=1)
    affected.update(int(i) for i in np.nonzero(moved)[0])

    # (2) base faces nearest to geometry the perturbation added/removed
    _, d_pert, tri = base_mesh.nearest.on_surface(pert_mesh.vertices)
    added = np.asarray(tri)[np.asarray(d_pert) > threshold]
    affected.update(int(i) for i in np.unique(added))

    return sorted(affected)


def compute_affect_map(engine, code: str, params: dict, manifest: list[dict],
                       base_stl: Path) -> dict[str, list[int]]:
    """{param_name: [affected face index, ...]} for every perturbable parameter.
    Best-effort: a parameter whose perturbed build fails is simply omitted."""
    import trimesh

    base = trimesh.load(str(base_stl), process=False)
    if base.faces is None or len(base.faces) == 0:
        return {}
    diag = float(((base.bounds[1] - base.bounds[0]) ** 2).sum() ** 0.5) or 1.0
    threshold = max(0.15, 0.0025 * diag)

    out: dict[str, list[int]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for spec in manifest:
            name = spec["name"]
            if name not in params:
                continue
            newv = _perturbed_value(spec, params[name])
            if newv is None:
                continue
            try:
                result = engine.execute(code, {**params, name: newv}, Path(tmp) / name, preview=True)
                if not result.ok or "stl" not in result.artifacts:
                    continue
                pert = trimesh.load(result.artifacts["stl"], process=False)
                out[name] = _affected_faces(base, pert, threshold)
            except Exception:
                continue
    return out


def affect_path(run_dir: Path) -> Path:
    return Path(run_dir) / "affect.json"


def build_and_cache(engine, code: str, params: dict, manifest: list[dict],
                    run_dir: Path) -> dict[str, list[int]]:
    """Compute the affect map for a saved run and cache it beside the artifacts."""
    run_dir = Path(run_dir)
    base_stl = run_dir / "model.stl"
    if not base_stl.exists():
        return {}
    amap = compute_affect_map(engine, code, params, manifest, base_stl)
    try:
        affect_path(run_dir).write_text(json.dumps(amap))
    except OSError:
        pass
    return amap
