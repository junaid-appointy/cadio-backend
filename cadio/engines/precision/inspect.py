"""Measure an uploaded reference solid (STEP / STL).

Gives the agent real numbers to build against ("make a lid for this") instead
of user-typed guesses. Parsing only — no code execution — so it runs in-process
on the backend, not in the sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def inspect_geometry(path: Path) -> dict[str, Any]:
    """Return measured facts about a STEP/STL file, or {'error': ...}."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext in (".step", ".stp"):
            return _inspect_step(path)
        if ext == ".stl":
            return _inspect_stl(path)
        return {"error": f"unsupported geometry type {ext!r} — upload STEP or STL"}
    except Exception as exc:
        return {"error": f"could not read geometry: {exc}"}


def _bbox_facts(size, mn, mx, extra: dict) -> dict:
    return {
        "bbox_mm": {"size": [round(v, 3) for v in size],
                    "min": [round(v, 3) for v in mn],
                    "max": [round(v, 3) for v in mx]},
        **extra,
    }


def _inspect_step(path: Path) -> dict:
    from build123d import import_step

    part = import_step(str(path))
    bb = part.bounding_box()
    facts = _bbox_facts(
        [bb.size.X, bb.size.Y, bb.size.Z],
        [bb.min.X, bb.min.Y, bb.min.Z],
        [bb.max.X, bb.max.Y, bb.max.Z],
        {"volume_mm3": round(float(part.volume), 1),
         "note": "STEP (BREP) — dimensions are exact."},
    )
    # cylindrical faces -> candidate hole/boss diameters (rounded, deduped)
    diameters = set()
    try:
        for face in part.faces():
            geo = getattr(face, "geom_type", None)
            if str(geo).upper().find("CYLIN") >= 0:
                r = getattr(getattr(face, "radius", None), "real", None) or getattr(face, "radius", None)
                if r:
                    diameters.add(round(float(r) * 2, 2))
    except Exception:
        pass
    if diameters:
        facts["cylindrical_diameters_mm"] = sorted(diameters)
    return facts


def _inspect_stl(path: Path) -> dict:
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    size = (mesh.bounds[1] - mesh.bounds[0]).tolist()
    facts = _bbox_facts(size, mesh.bounds[0].tolist(), mesh.bounds[1].tolist(),
                        {"triangles": int(len(mesh.faces)),
                         "watertight": bool(mesh.is_watertight),
                         "note": "STL (mesh) — dimensions from the tessellation; "
                                 "treat as approximate, confirm critical fits with the user."})
    if mesh.is_watertight and mesh.volume > 0:
        facts["volume_mm3"] = round(float(mesh.volume), 1)
    return facts
