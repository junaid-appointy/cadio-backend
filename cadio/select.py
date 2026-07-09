"""Viewer selection → a regeneration-stable reference the agent can act on.

The viewer loads a run's STL; a click gives us ONE triangle index, in STL facet
order — the same order affect.py and the browser's STLLoader use (face i on the
backend == triangle i in the viewer). From that seed we do two things:

1. Grow the facet across coplanar neighbours (trimesh face adjacency + dihedral
   angle) into the whole face/region the user actually pointed at, so clicking
   "the wall" selects the wall, not one stray triangle. Those indices go back to
   the viewer to highlight.
2. DESCRIBE that region in terms that survive a rebuild — where it sits in the
   part (bbox-relative) and which parameters govern it (from the affect map) —
   and hand the agent that description, never the raw indices. Indices don't
   survive regeneration; "the +X-end wall, governed by `wall`" does. This is the
   whole trick behind stable part references (see ai-3d-product-plan.md §6.7).

Pure geometry on existing deps (trimesh + numpy) — no new packages, no LLM.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from . import affect


def _grow_region(mesh, seed: int, max_angle: float) -> set[int]:
    """Flood-fill from `seed` over edge-adjacent faces that are (a) locally smooth
    — dihedral below `max_angle` — AND (b) still facing roughly the way the seed
    facet faces. Condition (b) is what stops the fill from creeping around a
    rounded corner/fillet and swallowing the whole shell: once the surface has
    turned more than the cone away from the seed normal, growth halts, so a click
    on the +X wall selects that wall, not every wall it curves into."""
    import numpy as np

    normals = np.asarray(mesh.face_normals)
    seed_normal = normals[seed]
    cone = max(max_angle, np.deg2rad(30.0))  # seed-cone: a bit wider than the edge test

    adjacency = mesh.face_adjacency  # (m, 2) pairs of edge-sharing faces
    angles = mesh.face_adjacency_angles  # (m,) dihedral angle per pair
    graph: dict[int, list[int]] = defaultdict(list)
    for (a, b), ang in zip(adjacency, angles):
        if ang <= max_angle:
            graph[int(a)].append(int(b))
            graph[int(b)].append(int(a))

    seen = {seed}
    dq = deque([seed])
    while dq:
        f = dq.popleft()
        for g in graph[f]:
            if g in seen:
                continue
            if float(np.arccos(np.clip(normals[g] @ seed_normal, -1.0, 1.0))) <= cone:
                seen.add(g)
                dq.append(g)
    return seen


def _location_words(frac) -> list[str]:
    """Human, bbox-relative position of the region's centroid. Z is up (mm world
    matches build123d). Empty axes are omitted; a dead-centre region → 'central'."""
    fx, fy, fz = (float(v) for v in frac)
    words: list[str] = []
    if fz > 0.66:
        words.append("top")
    elif fz < 0.34:
        words.append("bottom")
    if fx > 0.66:
        words.append("+X end")
    elif fx < 0.34:
        words.append("-X end")
    if fy > 0.66:
        words.append("+Y side")
    elif fy < 0.34:
        words.append("-Y side")
    return words or ["center"]


def _load_face_ids(run_dir: Path, n_faces: int) -> list[int] | None:
    """Per-facet source-face ids written by the sandbox, or None if absent /
    length-mismatched (a mismatch would highlight the wrong faces — fall back)."""
    path = Path(run_dir) / "face_ids.json"
    if not path.exists():
        return None
    try:
        ids = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(ids, list) or len(ids) != n_faces:
        return None
    return ids


def _faces_meta(run_dir: Path) -> dict[int, dict]:
    path = Path(run_dir) / "faces.json"
    if not path.exists():
        return {}
    try:
        return {f["id"]: f for f in json.loads(path.read_text())}
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return {}


def _feature_label(meta: dict | None) -> str | None:
    """Human name for a BREP face from its metadata: an agent-given name wins
    ('spout'); otherwise the surface type ('cylindrical face (Ø16)')."""
    if not meta:
        return None
    if meta.get("name"):
        return f"`{meta['name']}`"
    kind = meta.get("type", "freeform")
    if kind == "cylindrical" and meta.get("radius"):
        return f"cylindrical face (Ø{meta['radius'] * 2:g})"
    return f"{kind} face"


def _governing_params(run_dir: Path, region: set[int]) -> list[str]:
    """Parameters whose affected-face set meaningfully overlaps the selection —
    i.e. the knobs that control this region. Empty if no affect map is cached."""
    path = affect.affect_path(run_dir)
    if not path.exists():
        return []
    try:
        amap: dict[str, list[int]] = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    hits: list[tuple[str, int]] = []
    for name, faces in amap.items():
        overlap = len(region & set(faces))
        # ignore incidental single-facet grazes; require a real share of the region
        if overlap and overlap >= max(1, 0.15 * len(region)):
            hits.append((name, overlap))
    hits.sort(key=lambda h: -h[1])
    return [name for name, _ in hits]


def describe_selection(
    run_dir: Path, face: int, max_angle_deg: float = 20.0
) -> dict[str, Any] | None:
    """Resolve a clicked facet into {faces, where, governed_by, centroid, normal}.
    Returns None if the run has no mesh or the index is out of range."""
    import numpy as np
    import trimesh

    stl = Path(run_dir) / "model.stl"
    if not stl.exists():
        return None
    # process=False keeps facet ORDER/COUNT aligned with the browser's STLLoader
    # (face i here == triangle i there); merge_vertices then welds the duplicated
    # STL vertices so faces share edges and adjacency is computable — it remaps
    # vertex indices only, leaving the face order/count untouched.
    mesh = trimesh.load(str(stl), process=False)
    n_faces = len(mesh.faces)
    if not isinstance(face, int) or face < 0 or face >= n_faces:
        return None

    # Preferred: the sandbox's BREP face map — clicking selects the WHOLE source
    # face (a full cylinder, a flat wall), exactly and threshold-free. Fallback:
    # coplanar flood-fill on the mesh when the map is missing (e.g. legacy runs).
    face_ids = _load_face_ids(run_dir, n_faces)
    if face_ids is not None:
        target = face_ids[face]
        region = {i for i, fid in enumerate(face_ids) if fid == target}
        feature = _feature_label(_faces_meta(run_dir).get(target))
    else:
        mesh.merge_vertices()  # weld duplicated STL verts so adjacency works
        region = _grow_region(mesh, face, float(np.deg2rad(max_angle_deg)))
        feature = None

    idx = sorted(region)
    centroid = mesh.triangles[idx].reshape(-1, 3).mean(axis=0)
    normal = np.asarray(mesh.face_normals)[idx].mean(axis=0)
    normal = normal / (float(np.linalg.norm(normal)) or 1.0)

    lo, hi = mesh.bounds
    span = np.where((hi - lo) > 1e-6, hi - lo, 1.0)
    frac = (centroid - lo) / span

    return {
        "faces": [int(i) for i in idx],
        "feature": feature,
        "where": _location_words(frac),
        "governed_by": _governing_params(run_dir, region),
        "centroid_mm": [round(float(v), 1) for v in centroid],
        "normal": [round(float(v), 2) for v in normal],
    }


def _load_edges(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "edges.json"
    if not path.exists():
        return []
    try:
        edges = json.loads(path.read_text())
        return edges if isinstance(edges, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def describe_edge(run_dir: Path, edge_id: int) -> dict[str, Any] | None:
    """Resolve a picked BREP edge into {edge, feature, where, length, centroid}."""
    import numpy as np
    import trimesh

    edge = next((e for e in _load_edges(run_dir) if e.get("id") == edge_id), None)
    if edge is None or not edge.get("points"):
        return None
    pts = np.asarray(edge["points"], dtype=float)
    centroid = pts.mean(axis=0)

    stl = Path(run_dir) / "model.stl"
    if stl.exists():
        lo, hi = trimesh.load(str(stl), process=False).bounds
    else:
        lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = np.where((hi - lo) > 1e-6, hi - lo, 1.0)
    frac = (centroid - lo) / span

    if edge.get("type") == "circle" and edge.get("radius"):
        feature = f"circular edge (Ø{edge['radius'] * 2:g})"
    else:
        feature = f"{edge.get('type', 'curve')} edge"
    return {
        "edge": edge_id,
        "feature": feature,
        "where": _location_words(frac),
        "length_mm": edge.get("length"),
        "centroid_mm": [round(float(v), 1) for v in centroid],
    }


def _face_phrase(desc: dict[str, Any]) -> str:
    where = ", ".join(desc.get("where") or ["center"])
    feature = desc.get("feature")
    what = f"a {feature} at the {where}" if feature else f"the {where} region"
    govs = desc.get("governed_by") or []
    gov = " (controlled by " + ", ".join(f"`{g}`" for g in govs) + ")" if govs else ""
    return what + gov


def _edge_phrase(desc: dict[str, Any]) -> str:
    where = ", ".join(desc.get("where") or ["center"])
    length = desc.get("length_mm")
    tail = f", length {length} mm" if length is not None else ""
    return f"{desc['feature']} at the {where}{tail}"


def _wrap_note(phrases: list[str]) -> str | None:
    """Wrap one or more selected-part phrases into the agent-facing note."""
    if not phrases:
        return None
    if len(phrases) == 1:
        body = "this specific part of the CURRENT model: " + phrases[0]
        which = "THIS feature"
    else:
        body = "these specific parts of the CURRENT model: " + "; ".join(phrases)
        which = "THESE features"
    return (
        f"[The user SELECTED {body}. Scope your change to {which} and leave "
        "everything else unchanged. If it's ambiguous which feature(s) they mean, "
        "ask before rebuilding.]"
    )


def selection_note(desc: dict[str, Any]) -> str:
    """Agent-facing note for a single selected face (kept for the /select
    endpoint and callers with a ready descriptor)."""
    return _wrap_note([_face_phrase(desc)]) or ""


def build_note(run_dir: Path, faces: list[int] | None = None,
               edges: list[int] | None = None) -> str | None:
    """Compose the agent-facing note for a whole selection — any mix of picked
    faces (by seed facet index) and edges (by id). None if nothing resolves."""
    phrases: list[str] = []
    for seed in faces or []:
        d = describe_selection(run_dir, seed)
        if d:
            phrases.append(_face_phrase(d))
    for eid in edges or []:
        d = describe_edge(run_dir, eid)
        if d:
            phrases.append(_edge_phrase(d))
    return _wrap_note(phrases)
