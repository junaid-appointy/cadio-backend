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


class ManifestError(ValueError):
    """PARAMS is malformed. The message is written for the AGENT to read and
    self-correct (it rides back as the tool result), so keep it a single crisp,
    instructive sentence — no traceback."""


_ALLOWED_TYPES = ("number", "integer", "string", "boolean")


def validate_manifest(declared: list) -> list:
    """Validate + normalize the program's PARAMS. Raises ManifestError on any
    problem. Numeric bounds/defaults are coerced to real numbers so the manifest
    the host stores and the frontend renders is type-clean. Returns the same
    list with each spec's values normalized in place."""
    if not isinstance(declared, list):
        raise ManifestError("PARAMS must be a list of parameter dicts.")
    seen: set[str] = set()
    for spec in declared:
        if not isinstance(spec, dict):
            raise ManifestError("Every entry in PARAMS must be a dict with at least 'name' and 'default'.")
        name = spec.get("name")
        if not isinstance(name, str) or not name.isidentifier():
            raise ManifestError(
                f"Parameter name {name!r} is invalid — each name must be a non-empty valid identifier "
                "(letters, digits, underscore; not starting with a digit)."
            )
        if name in seen:
            raise ManifestError(
                f"PARAMS declares parameter '{name}' more than once — every parameter name must be unique. "
                "For repeated features (wheels, holes, switches), declare ONE integer count parameter plus "
                "SHARED dimension parameters and place the features in a loop inside build(); never "
                "wheel_1_x, wheel_2_x."
            )
        seen.add(name)
        if "default" not in spec:
            raise ManifestError(f"Parameter '{name}' is missing a 'default'.")
        ptype = spec.get("type", "number")
        if ptype not in _ALLOWED_TYPES:
            raise ManifestError(
                f"Parameter '{name}' has unknown type {ptype!r}; use one of {', '.join(_ALLOWED_TYPES)}."
            )
        spec["type"] = ptype
        if ptype in ("number", "integer"):
            try:
                default = float(spec["default"])
                lo = None if spec.get("min") is None else float(spec["min"])
                hi = None if spec.get("max") is None else float(spec["max"])
            except (TypeError, ValueError):
                raise ManifestError(
                    f"Parameter '{name}' is numeric but its default/min/max are not numbers."
                )
            if lo is not None and hi is not None and lo >= hi:
                raise ManifestError(f"Parameter '{name}' has min ({lo}) >= max ({hi}).")
            if lo is not None and default < lo:
                raise ManifestError(f"Parameter '{name}' default {default} is below min {lo}.")
            if hi is not None and default > hi:
                raise ManifestError(f"Parameter '{name}' default {default} is above max {hi}.")
            if ptype == "integer":
                if default != int(default):
                    raise ManifestError(f"Integer parameter '{name}' has non-integer default {spec['default']!r}.")
                default = int(default)
            # write coerced numbers back so the stored manifest is type-clean
            spec["default"] = default
            if lo is not None:
                spec["min"] = int(lo) if ptype == "integer" else lo
            if hi is not None:
                spec["max"] = int(hi) if ptype == "integer" else hi
        elif ptype == "boolean":
            if not isinstance(spec["default"], bool):
                raise ManifestError(f"Boolean parameter '{name}' has a non-boolean default {spec['default']!r}.")
        elif ptype == "string":
            if not isinstance(spec["default"], str):
                raise ManifestError(f"String parameter '{name}' has a non-string default {spec['default']!r}.")
    return declared


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


def _to_topods_faces(value):
    """Flatten whatever a program's features() maps a name to — a build123d Face,
    a ShapeList, or an iterable of Faces — into raw TopoDS_Face objects."""
    items = value if isinstance(value, (list, tuple, set)) else None
    if items is None:
        items = list(value) if hasattr(value, "__iter__") and not hasattr(value, "wrapped") else [value]
    out = []
    for it in items:
        w = getattr(it, "wrapped", None)
        if w is not None:
            out.append(w)
    return out


def _feature_names(fn, part, params, id_faces: dict) -> dict[int, str]:
    """Map face-id -> agent-given name using an optional `features(part, params)`
    hook in the program. Best-effort: matching is by BREP identity (IsSame)."""
    if fn is None:
        return {}
    try:
        mapping = fn(part, params) or {}
    except Exception:
        return {}
    names: dict[int, str] = {}
    for name, value in mapping.items():
        try:
            targets = _to_topods_faces(value)
        except Exception:
            continue
        for fid, topo in id_faces.items():
            if any(topo.IsSame(t) for t in targets):
                names[fid] = str(name)
    return names


def write_face_map(part, out: Path, params: dict | None = None, features_fn=None) -> None:
    """Write facet -> source-face maps beside model.stl, read on click to select
    a whole BREP face. `export_stl` (called just before) has already meshed and
    stored the triangulation on the shape; we read that SAME triangulation in the
    SAME TopExp face order, so facet i here == triangle i in model.stl / the
    viewer's STLLoader. Emits:
      face_ids.json : [face_id per facet]  (length == facet count)
      faces.json    : [{id, type, radius, n, name?}]  per-face metadata
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import (
        GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus,
    )
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    surf_type = {
        GeomAbs_Plane: "planar", GeomAbs_Cylinder: "cylindrical", GeomAbs_Cone: "conical",
        GeomAbs_Sphere: "spherical", GeomAbs_Torus: "toroidal",
    }
    face_ids: list[int] = []
    faces: list[dict] = []
    id_faces: dict[int, Any] = {}
    exp = TopExp_Explorer(part.wrapped, TopAbs_FACE)
    fid = 0
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        id_faces[fid] = face
        tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        if tri is not None:
            adaptor = BRepAdaptor_Surface(face)
            gtype = surf_type.get(adaptor.GetType(), "freeform")
            radius = None
            try:
                if adaptor.GetType() == GeomAbs_Cylinder:
                    radius = round(adaptor.Cylinder().Radius(), 2)
            except Exception:
                radius = None
            n = tri.NbTriangles()
            faces.append({"id": fid, "type": gtype, "radius": radius, "n": n})
            face_ids.extend([fid] * n)
        fid += 1
        exp.Next()

    names = _feature_names(features_fn, part, params or {}, id_faces)
    if names:
        for f in faces:
            if f["id"] in names:
                f["name"] = names[f["id"]]

    (out / "face_ids.json").write_text(json.dumps(face_ids))
    (out / "faces.json").write_text(json.dumps(faces))


def write_edge_map(part, out: Path) -> None:
    """Write per-edge geometry beside model.stl so a viewer can pick an EDGE
    (to fillet/chamfer), not just a face. Emits edges.json:
      [{id, type, radius, length, points:[[x,y,z], ...]}]  (deduped BREP edges).
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.GCPnts import GCPnts_UniformAbscissa
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Ellipse, GeomAbs_Line
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    curve_type = {GeomAbs_Line: "line", GeomAbs_Circle: "circle", GeomAbs_Ellipse: "ellipse"}
    emap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(part.wrapped, TopAbs_EDGE, emap)

    edges: list[dict] = []
    for i in range(1, emap.Extent() + 1):
        edge = TopoDS.Edge_s(emap.FindKey(i))
        try:
            adaptor = BRepAdaptor_Curve(edge)
        except Exception:
            continue
        ctype = curve_type.get(adaptor.GetType(), "curve")
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, props)
        length = float(props.Mass())
        radius = None
        if adaptor.GetType() == GeomAbs_Circle:
            radius = round(adaptor.Circle().Radius(), 2)
        n = 2 if ctype == "line" else int(min(64, max(10, length / 1.5)))
        pts: list[list[float]] = []
        try:
            sampler = GCPnts_UniformAbscissa(adaptor, n)
            if sampler.IsDone():
                for k in range(1, sampler.NbPoints() + 1):
                    p = adaptor.Value(sampler.Parameter(k))
                    pts.append([round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3)])
        except Exception:
            pts = []
        if len(pts) < 2:
            continue
        edges.append({"id": i - 1, "type": ctype, "radius": radius,
                      "length": round(length, 2), "points": pts})
    (out / "edges.json").write_text(json.dumps(edges))


def run_job(program: str, outdir: str, params: dict | None = None, preview: bool = False) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    result = {"ok": False}
    try:
        mod = load_program(Path(program))
        declared = validate_manifest(getattr(mod, "PARAMS", []))
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
            # per-facet BREP face map (facet i -> source OCCT face) + per-edge
            # geometry — lets a viewer click select a WHOLE face (a full cylinder,
            # a flat wall) or an edge, not a mesh strip. An optional features()
            # hook names faces. Best-effort: never fail a build over any of it.
            try:
                write_face_map(part, out, resolved, getattr(mod, "features", None))
            except Exception:
                pass
            try:
                write_edge_map(part, out)
            except Exception:
                pass

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
    except ManifestError as exc:
        # a single instructive sentence the agent can act on — no traceback noise
        result["error"] = f"PARAMS manifest invalid: {exc}"
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
