"""Measure an uploaded reference solid (STEP / STL).

Gives the agent real numbers to build against ("make a lid for this") instead
of user-typed guesses. Parsing only — no code execution.

STEP inspection needs the OCCT kernel (~360MB resident once imported), so it
runs in a short-lived `python -I` SUBPROCESS of this same file — never in the
API process, whose footprint must stay small and which must never die. STL
inspection stays in-process (trimesh is already loaded there and cheap).

(Renamed from inspect.py: a module named `inspect` shadowed the stdlib inside
the sandbox workers and silently broke the warm pool — never reuse that name.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_STEP_TIMEOUT_S = 120.0


def inspect_geometry(path: Path) -> dict[str, Any]:
    """Return measured facts about a STEP/STL file, or {'error': ...}."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext in (".step", ".stp"):
            return _inspect_step_subprocess(path)
        if ext == ".stl":
            return _inspect_stl(path)
        return {"error": f"unsupported geometry type {ext!r} — upload STEP or STL"}
    except Exception as exc:
        return {"error": f"could not read geometry: {exc}"}


def _inspect_step_subprocess(path: Path) -> dict:
    """Run the OCCT-backed STEP measurement in an isolated child process so the
    kernel import never lands in (or crashes) the API process."""
    env = {"PATH": os.environ.get("PATH", ""),
           "HOME": os.environ.get("HOME", "/tmp"),
           "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", os.path.abspath(__file__), str(path)],
            capture_output=True, text=True, timeout=_STEP_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"STEP inspection timed out after {_STEP_TIMEOUT_S:.0f}s"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": f"could not read geometry: {proc.stderr.strip()[-500:] or 'no output'}"}


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


if __name__ == "__main__":
    # child-process entry (see _inspect_step_subprocess): measure one STEP file
    # and print JSON. Errors also come back as JSON so the parent never parses
    # a traceback.
    try:
        print(json.dumps(_inspect_step(Path(sys.argv[1]))))
    except Exception as exc:  # noqa: BLE001 — boundary: everything becomes JSON
        print(json.dumps({"error": f"could not read geometry: {exc}"}))
