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
import time
import traceback
from pathlib import Path

_module_counter = itertools.count()


def load_program(path: Path):
    # unique module name per load: a warm worker serves many jobs and must
    # never hand one job a previous job's module
    name = f"cadio_program_{next(_module_counter)}"
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


def _split_value(value):
    """Sort whatever a program's features() maps a name to into (faces, solids).

    A name may point at a build123d Face / ShapeList of faces (classic form) OR
    at a construction sub-solid (Part / Solid / Compound — the preferred form,
    where the agent builds each part as its own solid). We classify each item by
    its wrapped TopoDS shape type. Sub-solids are matched geometrically (a final
    face belongs to the solid whose surface it lies on); faces are matched by
    BREP identity."""
    from OCP.TopAbs import (
        TopAbs_COMPOUND, TopAbs_COMPSOLID, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID,
    )

    solid_types = (TopAbs_SOLID, TopAbs_COMPOUND, TopAbs_COMPSOLID, TopAbs_SHELL)
    items = value if isinstance(value, (list, tuple, set)) else None
    if items is None:
        items = list(value) if hasattr(value, "__iter__") and not hasattr(value, "wrapped") else [value]
    faces, solids = [], []
    for it in items:
        w = getattr(it, "wrapped", None)
        if w is None:
            continue
        try:
            st = w.ShapeType()
        except Exception:
            continue
        if st == TopAbs_FACE:
            faces.append(w)
        elif st in solid_types:
            solids.append(w)
    return faces, solids


def _face_surface_point(face):
    """A point that lies ON a face's underlying surface — evaluated at the middle
    of the face's UV bounds. A trimmed sphere/cylinder patch keeps its ORIGINAL
    surface geometry after a boolean, so this point sits exactly on the parent
    solid's shell (distance ~0), which is what makes solid classification work.
    (The area centroid does NOT: for a curved face it sits inside the volume.)"""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps

    try:
        umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
        surf = BRep_Tool.Surface_s(face)
        return surf.Value((umin + umax) / 2.0, (vmin + vmax) / 2.0)
    except Exception:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        return props.CentreOfMass()


def _classify_by_solids(id_faces: dict, solid_targets: dict, diag: float) -> dict[int, str]:
    """Assign each final BREP face to the named construction solid whose surface
    it lies on. We sample a point ON the face's surface and take the nearest named
    solid's shell (distance ~0 for the parent solid, since a union trims surfaces
    but never moves them). Faces beyond tolerance from every named solid are left
    unnamed (they belong to an unnamed part)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    tol = max(1e-3, 5e-4 * diag)
    # precompute each named solid's bbox (grown by tol). A face's surface point can
    # only be within tol of a solid whose (grown) box contains it, so the box test
    # skips the exact distance for every far-away solid — turning O(faces x solids)
    # into O(faces x nearby-solids). Correctness is unchanged: a skipped solid is
    # provably farther than tol.
    boxed: list[tuple[str, object, Bnd_Box]] = []
    for name, solids in solid_targets.items():
        for s in solids:
            box = Bnd_Box()
            try:
                BRepBndLib.Add_s(s, box)
                box.Enlarge(tol)
            except Exception:
                continue
            boxed.append((name, s, box))

    names: dict[int, str] = {}
    for fid, face in id_faces.items():
        pnt = _face_surface_point(face)
        vtx = BRepBuilderAPI_MakeVertex(pnt).Vertex()
        best, best_d = None, None
        for name, s, box in boxed:
            if box.IsOut(pnt):
                continue
            try:
                d = BRepExtrema_DistShapeShape(vtx, s).Value()
            except Exception:
                continue
            if best_d is None or d < best_d:
                best_d, best = d, name
        if best is not None and best_d is not None and best_d <= tol:
            names[fid] = best
    return names


def _resolve_features(fn, part, params, id_faces: dict, diag: float) -> dict[int, str]:
    """Map face-id -> agent-given name via the optional `features(part, params)`
    hook. Sub-solid values are classified geometrically; face values by BREP
    identity (IsSame), which overrides a geometric assignment for the same face."""
    if fn is None:
        return {}
    try:
        mapping = fn(part, params) or {}
    except Exception:
        return {}
    face_targets: dict[str, list] = {}
    solid_targets: dict[str, list] = {}
    for name, value in mapping.items():
        try:
            faces, solids = _split_value(value)
        except Exception:
            continue
        if faces:
            face_targets[str(name)] = faces
        if solids:
            solid_targets[str(name)] = solids
    names: dict[int, str] = {}
    if solid_targets:
        names.update(_classify_by_solids(id_faces, solid_targets, diag))
    for name, targets in face_targets.items():
        for fid, topo in id_faces.items():
            if any(topo.IsSame(t) for t in targets):
                names[fid] = name
    return names


def _fid_resolver(id_faces: dict):
    """An O(1) face -> face-id lookup, replacing a linear IsSame scan. Backed by an
    OCCT hashed shape map; we normalize every face to FORWARD orientation on both
    insert and query so the match is orientation-independent — i.e. equivalent to
    TopoDS IsSame (the linear scan's semantics), not the orientation-sensitive
    IsEqual the map hashes by default."""
    from OCP.TopAbs import TopAbs_FORWARD
    from OCP.TopTools import TopTools_IndexedMapOfShape

    fmap = TopTools_IndexedMapOfShape()
    # id_faces keys are the contiguous 0..N-1 fids assigned in write_face_map's
    # explorer order, so map index i+1 corresponds to fid i.
    for fid in range(len(id_faces)):
        fmap.Add(id_faces[fid].Oriented(TopAbs_FORWARD))

    def resolve(face):
        idx = fmap.FindIndex(face.Oriented(TopAbs_FORWARD))
        return idx - 1 if idx > 0 else None

    return resolve


def _face_adjacency(part, id_faces: dict) -> dict[int, set]:
    """face-id -> set of edge-adjacent face-ids (two faces are adjacent iff they
    share a BREP edge). Used to split a name's faces into connected components —
    two disjoint 'ear' islands become two part instances."""
    from collections import defaultdict

    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    fid_of = _fid_resolver(id_faces)

    adj: dict[int, set] = defaultdict(set)
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(part.wrapped, TopAbs_EDGE, TopAbs_FACE, emap)
    for i in range(1, emap.Extent() + 1):
        flist = emap.FindFromIndex(i)
        if flist.Extent() < 2:
            continue
        a, b = fid_of(TopoDS.Face_s(flist.First())), fid_of(TopoDS.Face_s(flist.Last()))
        if a is not None and b is not None and a != b:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def _components(face_set: set, adj: dict[int, set]) -> list[list[int]]:
    """Connected components of `face_set` under edge adjacency `adj`."""
    seen: set = set()
    comps: list[list[int]] = []
    for start in face_set:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj.get(x, ()):
                if y in face_set and y not in seen:
                    seen.add(y)
                    stack.append(y)
        comps.append(comp)
    return comps


def _face_geometry(id_faces: dict) -> dict[int, tuple]:
    """face-id -> (centroid[3], area, bbox[6]). Drives part centroids/sizes for
    the agent note and the position qualifiers that disambiguate repeated parts."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    out: dict[int, tuple] = {}
    for fid, face in id_faces.items():
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        c = props.CentreOfMass()
        area = float(props.Mass())
        box = Bnd_Box()
        try:
            BRepBndLib.Add_s(face, box)
            bb = box.Get()  # (xmin, ymin, zmin, xmax, ymax, zmax)
        except Exception:
            bb = (c.X(), c.Y(), c.Z(), c.X(), c.Y(), c.Z())
        out[fid] = ((c.X(), c.Y(), c.Z()), area, tuple(bb))
    return out


def _title(name: str) -> str:
    """Agent identifier -> readable display base ('left_ear' -> 'Left ear')."""
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else name


_AXIS_WORDS = (("left", "right"), ("front", "back"), ("bottom", "top"))


def _qualify(comp_centroids: list[tuple]) -> dict:
    """Given [(comp_key, centroid[3]), ...] for the instances that share one name,
    return {comp_key: qualifier_words} that make each instance unique and human
    ('left', 'front left', ...). Falls back to deterministic ordinals when a
    position split can't separate them."""
    n = len(comp_centroids)
    if n == 1:
        return {comp_centroids[0][0]: ""}
    cents = [c for _, c in comp_centroids]
    spreads = [max(c[a] for c in cents) - min(c[a] for c in cents) for a in range(3)]
    means = [sum(c[a] for c in cents) / n for a in range(3)]
    axis_by_spread = sorted(range(3), key=lambda a: -spreads[a])
    # try 1, then 2, then 3 axes (widest-spread first) until every label is unique
    for k in range(1, 4):
        use = {a for a in axis_by_spread[:k] if spreads[a] > 1e-6}
        labels = {}
        for key, c in comp_centroids:
            # compose words in a natural reading order: vertical, depth, side
            words = [_AXIS_WORDS[a][0] if c[a] < means[a] else _AXIS_WORDS[a][1]
                     for a in (2, 1, 0) if a in use]
            labels[key] = " ".join(words)
        if len(set(labels.values())) == n:
            return labels
    ordered = sorted(comp_centroids, key=lambda kc: (kc[1][2], kc[1][1], kc[1][0]))
    return {key: str(i + 1) for i, (key, _) in enumerate(ordered)}


def _region_label(faces: list[int], face_kind: dict, face_geom: dict) -> str:
    """Readable base label for an UNNAMED part, from its dominant face's surface
    type ('Cylindrical face Ø16', 'Flat face', 'Curved face')."""
    dom = max(faces, key=lambda f: face_geom.get(f, ((0, 0, 0), 0, ()))[1])
    kind, radius = face_kind.get(dom, ("freeform", None))
    if kind == "cylindrical" and radius:
        return f"Cylindrical face Ø{radius * 2:g}"
    if kind == "planar":
        return "Flat face"
    if kind == "freeform":
        return "Curved face"
    return f"{kind.capitalize()} face"


def _make_part(name, faces: list[int], face_geom: dict) -> dict:
    """A part record: area-weighted centroid + union bbox over its faces."""
    faces = sorted(faces)
    tot = sum(face_geom.get(f, ((0, 0, 0), 0, ()))[1] for f in faces) or 1.0
    cen = [sum(face_geom[f][0][a] * face_geom[f][1] for f in faces) / tot for a in range(3)]
    bxs = [face_geom[f][2] for f in faces if face_geom.get(f, (None, None, ()))[2]]
    if bxs:
        bbox = [min(b[a] for b in bxs) for a in range(3)] + [max(b[a + 3] for b in bxs) for a in range(3)]
    else:
        bbox = cen + cen
    return {"name": name, "faces": faces, "centroid": cen, "bbox": bbox, "area": tot}


def _build_parts(names: dict, adj: dict, regions: dict, face_geom: dict,
                 face_kind: dict, all_faces: list[int]) -> tuple[list[dict], dict]:
    """Partition every face into parts: each named group is split into connected
    components (one instance per island); leftover unnamed faces are grouped by
    their smooth region. Then assign globally-unique, human display names.
    Returns (parts, name_stats) where name_stats[name] = (n_faces, n_components) —
    the naming-quality backstop reads it to spot positional bands."""
    from collections import defaultdict

    parts: list[dict] = []
    name_stats: dict[str, tuple] = {}
    by_name: dict[str, set] = defaultdict(set)
    for fid, name in names.items():
        by_name[name].add(fid)
    for name, fset in by_name.items():
        comps = _components(fset, adj)
        name_stats[name] = (len(fset), len(comps))
        for comp in comps:
            parts.append(_make_part(name, comp, face_geom))

    unnamed = [fid for fid in all_faces if fid not in names]
    region_groups: dict = defaultdict(list)
    for fid in unnamed:
        region_groups[regions.get(fid, ("solo", fid))].append(fid)
    for comp in region_groups.values():
        p = _make_part(None, comp, face_geom)
        p["_base"] = _region_label(comp, face_kind, face_geom)
        parts.append(p)

    _assign_displays(parts)
    for i, p in enumerate(parts):
        p["id"] = i
        p.pop("_base", None)
    return parts, name_stats


def _assign_displays(parts: list[dict]) -> None:
    """Fill each part's `display` so it is readable AND unique across the model:
    a lone name is Title-cased; repeated names get position qualifiers; any
    residual collision gets a numeric suffix."""
    from collections import defaultdict

    groups: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(parts):
        base = _title(p["name"]) if p["name"] else p.get("_base", "Part")
        p["_disp_base"] = base
        groups[base].append(i)

    used: set = set()
    for base, idxs in groups.items():
        if len(idxs) == 1:
            disp = base
        else:
            quals = _qualify([(i, parts[i]["centroid"]) for i in idxs])
            for i in idxs:
                q = quals.get(i, "")
                parts[i]["_disp"] = f"{base} ({q})" if q else base
        if len(idxs) == 1:
            parts[idxs[0]]["_disp"] = disp
    # final global de-dup (rare: two different bases collide after qualifying)
    for p in parts:
        disp = p.get("_disp", p["_disp_base"])
        if disp in used:
            k = 2
            while f"{disp} ({k})" in used:
                k += 1
            disp = f"{disp} ({k})"
        used.add(disp)
        p["display"] = disp
        p.pop("_disp", None)
        p.pop("_disp_base", None)


def _naming_warnings(name_stats: dict, n_faces: int, n_regions: int) -> list[list[str]]:
    """Backstop that catches lazy/under-naming so the agent self-corrects from the
    validation report (proactive guidance lives in the corpus). Warnings only —
    never fails the build.

    A positional band (the anti-pattern: 'every face above z=50 is the head')
    grabs several unrelated blobs, so when its faces are split by connectivity it
    fragments into MANY disconnected pieces — whereas a real repeated part is a
    small, expected set (2 ears, 4 wheels). So we flag a name that fragments into
    5+ pieces, which catches the band (head→5 blobs here) while keeping legitimate
    pairs/quads and well-named single parts (a bow of 8 box faces) quiet."""
    out: list[list[str]] = []
    if n_faces < 6:
        return out
    for name, (fcount, comps) in name_stats.items():
        if comps >= 5 and fcount >= 10:
            out.append(["lazy_naming",
                        f"`{name}` spans {fcount} faces across {comps} disconnected pieces — that is a "
                        "positional band, not a part. Build each distinct part as its own named sub-solid "
                        "and return it from features() so users can select parts individually."])
            break
    if n_regions >= 6 and len(name_stats) <= 1:
        named = len(name_stats)
        out.append(["under_named",
                    f"the model has {n_regions} visually distinct regions but only {named} named part(s); "
                    "build the salient parts as their own named sub-solids in features() so users can "
                    "point at them."])
    return out


def _smooth_regions(part, id_faces: dict, face_tris: dict[int, int],
                    face_kind: dict[int, tuple]) -> dict[int, int]:
    """Group BREP faces into smooth regions — the 'part' a human perceives.

    Booleans + fillets shatter a perceived part into many BREP faces: a hole
    becomes two half-cylinders, and a fillet inserts sliver transition strips.
    Faces of the same smooth surface meet along TANGENT edges (OCCT marks them
    G1+; genuine corners are C0). But naively unioning every tangent edge fuses
    the whole shell into one blob, because a fillet is tangent to BOTH the faces
    it bridges. So we're careful:
      - LARGE ↔ LARGE tangent: merge only if it's the SAME split surface
        (co-planar walls, a cylinder cut in halves) — never two freeform faces.
      - small ↔ small tangent: cluster the slivers together.
      - a sliver cluster then ATTACHES to its single LARGEST tangent neighbour —
        so a fillet joins one part, never glues two large parts into one.
    Returns {face_id: region_id}. Best-effort; any face left alone is its own
    region.
    """
    from collections import defaultdict

    from OCP.BRep import BRep_Tool
    from OCP.GeomAbs import GeomAbs_C0
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    fid_of = _fid_resolver(id_faces)  # O(1) hashed lookup (was a linear IsSame scan)

    # tangent adjacency (non-C0 shared edges only)
    adj: dict[int, set[int]] = defaultdict(set)
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(part.wrapped, TopAbs_EDGE, TopAbs_FACE, emap)
    for i in range(1, emap.Extent() + 1):
        flist = emap.FindFromIndex(i)
        if flist.Extent() != 2:
            continue
        edge = TopoDS.Edge_s(emap.FindKey(i))
        f1, f2 = TopoDS.Face_s(flist.First()), TopoDS.Face_s(flist.Last())
        try:
            if BRep_Tool.Continuity_s(edge, f1, f2) == GeomAbs_C0:
                continue
        except Exception:
            continue
        a, b = fid_of(f1), fid_of(f2)
        if a is not None and b is not None and a != b:
            adj[a].add(b)
            adj[b].add(a)

    total = sum(face_tris.values()) or 1
    small_max = max(24, 0.01 * total)  # slivers/fillets vs. real faces
    small = {fid for fid in id_faces if face_tris.get(fid, 0) <= small_max}

    parent = {fid: fid for fid in id_faces}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def same_surface(a: int, b: int) -> bool:
        ta, ra = face_kind.get(a, ("freeform", None))
        tb, rb = face_kind.get(b, ("freeform", None))
        if ta != tb or ta == "freeform":
            return False  # never merge two freeform faces (that's the blob risk)
        if ta in ("cylindrical", "conical"):
            return ra is not None and rb is not None and abs(ra - rb) <= 0.01
        return True  # co-planar / co-spherical / co-toroidal split faces

    # pass 1: merge same-surface large faces, and cluster small faces together
    for a in id_faces:
        for b in adj[a]:
            if a >= b:
                continue
            a_small, b_small = a in small, b in small
            if a_small and b_small:
                union(a, b)
            elif not a_small and not b_small and same_surface(a, b):
                union(a, b)

    # pass 2: attach each small cluster to its single largest tangent-large neighbour
    clusters: dict[int, list[int]] = defaultdict(list)
    for fid in small:
        clusters[find(fid)].append(fid)
    for members in clusters.values():
        neigh_large = {nb for m in members for nb in adj[m] if nb not in small}
        if neigh_large:
            best = max(neigh_large, key=lambda L: face_tris.get(L, 0))
            union(best, members[0])

    labels: dict[int, int] = {}
    regions: dict[int, int] = {}
    for fid in id_faces:
        regions[fid] = labels.setdefault(find(fid), len(labels))
    return regions


def write_face_map(part, out: Path, params: dict | None = None, features_fn=None):
    """Write facet -> source-face maps beside model.stl, read on click to select
    a whole BREP face. `export_stl` (called just before) has already meshed and
    stored the triangulation on the shape; we read that SAME triangulation in the
    SAME TopExp face order, so facet i here == triangle i in model.stl / the
    viewer's STLLoader. Emits:
      face_ids.json : [face_id per facet]  (length == facet count)
      faces.json    : [{id, type, radius, n, name?, region?, part?}]  per-face meta
      parts.json    : [{id, name, display, faces, centroid_mm, bbox_mm, area_mm2}]
    Returns (id_faces, warnings): the face map (so write_edge_map can attach
    adjacent-part ids) and any naming-quality warnings for the validation report.
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
    face_tris: dict[int, int] = {}          # fid -> triangle count (0 if untriangulated)
    face_kind: dict[int, tuple] = {}        # fid -> (surface type, radius) for region merging
    exp = TopExp_Explorer(part.wrapped, TopAbs_FACE)
    fid = 0
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        id_faces[fid] = face
        gtype, radius = "freeform", None
        try:
            adaptor = BRepAdaptor_Surface(face)
            gtype = surf_type.get(adaptor.GetType(), "freeform")
            if adaptor.GetType() == GeomAbs_Cylinder:
                radius = round(adaptor.Cylinder().Radius(), 2)
        except Exception:
            pass
        tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        n = tri.NbTriangles() if tri is not None else 0
        face_tris[fid] = n
        face_kind[fid] = (gtype, radius)
        if tri is not None:
            faces.append({"id": fid, "type": gtype, "radius": radius, "n": n})
            face_ids.extend([fid] * n)
        fid += 1
        exp.Next()

    # model diagonal for the sub-solid classification tolerance
    face_geom = _face_geometry(id_faces)
    bxs = [g[2] for g in face_geom.values() if g[2]]
    if bxs:
        span = [max(b[a + 3] for b in bxs) - min(b[a] for b in bxs) for a in range(3)]
        diag = (span[0] ** 2 + span[1] ** 2 + span[2] ** 2) ** 0.5
    else:
        diag = 1.0

    names = _resolve_features(features_fn, part, params or {}, id_faces, diag)
    if names:
        for f in faces:
            if f["id"] in names:
                f["name"] = names[f["id"]]

    # smooth-region grouping so a click selects the perceived part, not a raw
    # BREP face (a fillet sliver or a boolean-split half-cylinder). Best-effort.
    regions: dict[int, int] = {}
    try:
        regions = _smooth_regions(part, id_faces, face_tris, face_kind)
        for f in faces:
            f["region"] = regions.get(f["id"], f["id"])
    except Exception:
        pass  # frontend falls back to per-face selection when region is absent

    # part table: the single source of truth for identity + display name. Named
    # groups split by connectivity; unnamed faces grouped by smooth region.
    warnings: list[list[str]] = []
    try:
        adj = _face_adjacency(part, id_faces)
        parts, name_stats = _build_parts(names, adj, regions, face_geom, face_kind, list(id_faces))
        face_to_part = {fid: p["id"] for p in parts for fid in p["faces"]}
        for f in faces:
            if f["id"] in face_to_part:
                f["part"] = face_to_part[f["id"]]
        parts_out = [
            {
                "id": p["id"], "name": p["name"], "display": p["display"],
                "faces": p["faces"],
                "centroid_mm": [round(v, 2) for v in p["centroid"]],
                "bbox_mm": [round(v, 2) for v in p["bbox"]],
                "area_mm2": round(p["area"], 1),
            }
            for p in parts
        ]
        (out / "parts.json").write_text(json.dumps(parts_out))
        warnings = _naming_warnings(name_stats, len(faces), len(set(regions.values())) or len(parts))
    except Exception:
        pass  # parts are a nicety; face/region selection still works without them

    (out / "face_ids.json").write_text(json.dumps(face_ids))
    (out / "faces.json").write_text(json.dumps(faces))
    return id_faces, warnings


def write_edge_map(part, out: Path, id_faces: dict | None = None) -> None:
    """Write per-edge geometry beside model.stl so a viewer can pick an EDGE
    (to fillet/chamfer), not just a face. Emits edges.json:
      [{id, type, radius, length, faces:[fid,fid], points:[[x,y,z], ...]}]
    `faces` are the ids of the two BREP faces meeting at the edge (so an edge can
    be named by its adjacent parts); requires the face map from write_face_map.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.GCPnts import GCPnts_UniformAbscissa
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Ellipse, GeomAbs_Line
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
    )

    curve_type = {GeomAbs_Line: "line", GeomAbs_Circle: "circle", GeomAbs_Ellipse: "ellipse"}
    emap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(part.wrapped, TopAbs_EDGE, emap)

    # edge -> adjacent BREP faces, resolved to the same face-ids write_face_map
    # assigned, so an edge can be tied to its parts. O(1) hashed lookup.
    fid_of = _fid_resolver(id_faces) if id_faces else (lambda _f: None)
    face_items = list((id_faces or {}).items())

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    if face_items:
        TopExp.MapShapesAndAncestors_s(part.wrapped, TopAbs_EDGE, TopAbs_FACE, ancestors)

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
        # denser polylines on curved edges so screen-space picking stays accurate
        # on large models (a line only needs its 2 endpoints)
        n = 2 if ctype == "line" else int(min(256, max(24, length)))
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
        adj_faces: list[int] = []
        if face_items and ancestors.Contains(edge):
            flist = ancestors.FindFromKey(edge)
            seen: set = set()
            # a manifold edge borders exactly 2 faces; First/Last covers them
            # (matches _smooth_regions; non-manifold extras are rare and dropped)
            for face in (flist.First(), flist.Last()):
                fid = fid_of(TopoDS.Face_s(face))
                if fid is not None and fid not in seen:
                    seen.add(fid)
                    adj_faces.append(fid)
        edges.append({"id": i - 1, "type": ctype, "radius": radius,
                      "length": round(length, 2), "faces": adj_faces, "points": pts})
    (out / "edges.json").write_text(json.dumps(edges))


def _rss_peak_mb() -> float | None:
    """Peak RSS of THIS worker process in MB (ru_maxrss is KB on Linux, bytes
    on macOS). The scarce-memory tier tunes worker rlimits off this number."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)
    except Exception:
        return None


def _stl_facet_count(stl_path: Path) -> int | None:
    """Facet count from the binary STL header (bytes 80:84) — every downstream
    cost (validation, render, viewer decode) keys off this number."""
    try:
        with open(stl_path, "rb") as fh:
            fh.seek(80)
            return int.from_bytes(fh.read(4), "little")
    except Exception:
        return None


def run_job(program: str, outdir: str, params: dict | None = None, preview: bool = False,
            coarse: bool = False) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    result = {"ok": False}
    timings: dict[str, float] = {}

    def _staged(name: str, t0: float) -> float:
        now = time.perf_counter()
        timings[name] = round(now - t0, 3)
        return now

    try:
        t = time.perf_counter()
        mod = load_program(Path(program))
        declared = validate_manifest(getattr(mod, "PARAMS", []))
        if not hasattr(mod, "build"):
            raise AttributeError("program must define build(params) -> Part")
        resolved = resolve_params(declared, params or {})

        part = mod.build(resolved)
        t = _staged("build_s", t)

        from build123d import export_step, export_stl

        artifacts = {}
        stl_path = out / "model.stl"
        bb0 = part.bounding_box()
        diag = ((bb0.size.X ** 2 + bb0.size.Y ** 2 + bb0.size.Z ** 2) ** 0.5) or 1.0
        if coarse:
            # throwaway diff build (affect map): tessellate coarsely — a fraction
            # of the triangles, so both STL export and the downstream proximity
            # diff are far cheaper. Tolerance scales with size and stays well below
            # the affect threshold (max(0.15, 0.0025*diag)) so it adds no false
            # "moved" faces.
            tol = max(0.05, 0.0008 * diag)
            export_stl(part, str(stl_path), tolerance=tol, angular_tolerance=0.5)
        else:
            # size-relative chord tolerance instead of build123d's fixed 1e-3mm.
            # 1e-3 on a large model (a castle) tessellates to hundreds of
            # thousands of triangles — tens of MB of STL that the browser must
            # download and decode (the "model stuck loading" complaint), plus
            # slower export/validation/render on every attempt. 0.02–0.15mm
            # chord deviation is invisible on screen and beyond print
            # resolution; every downstream stage keys off THIS mesh (pick maps,
            # affect maps, viewer indices), so consistency is preserved.
            tol = min(0.15, max(0.02, 0.00025 * diag))
            export_stl(part, str(stl_path), tolerance=tol)
        artifacts["stl"] = str(stl_path)
        t = _staged("stl_s", t)
        if not preview:  # STEP is for final exports; skip on live previews
            step_path = out / "model.step"
            export_step(part, str(step_path))
            artifacts["step"] = str(step_path)
            t = _staged("step_s", t)
            # per-facet BREP face map (facet i -> source OCCT face) + per-edge
            # geometry — lets a viewer click select a WHOLE part (a named sub-solid,
            # a full cylinder) or an edge, not a mesh strip. An optional features()
            # hook names parts. Best-effort: never fail a build over any of it.
            id_faces = None
            try:
                id_faces, naming_warnings = write_face_map(
                    part, out, resolved, getattr(mod, "features", None))
                result["warnings"] = naming_warnings
            except Exception:
                pass
            t = _staged("facemap_s", t)
            try:
                write_edge_map(part, out, id_faces)
            except Exception:
                pass
            t = _staged("edgemap_s", t)

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
                "stl_facets": _stl_facet_count(stl_path),
            }
        )
    except MemoryError:
        # the worker's rlimit tripped — tell the AGENT what to do, crisply
        result["error"] = (
            "the model ran out of memory while building — simplify it: "
            "reduce feature counts, fillet fewer edges at once, lower detail, "
            "or build it in smaller staged steps.")
    except ManifestError as exc:
        # a single instructive sentence the agent can act on — no traceback noise
        result["error"] = f"PARAMS manifest invalid: {exc}"
    except Exception:
        result["error"] = traceback.format_exc()
    result["timings"] = timings
    result["rss_peak_mb"] = _rss_peak_mb()
    return result


def main() -> int:
    program_path, params_json, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    overrides = json.loads(Path(params_json).read_text()) if params_json != "-" else {}
    result = run_job(program_path, outdir, overrides)
    (Path(outdir) / "result.json").write_text(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
