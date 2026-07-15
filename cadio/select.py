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


def _load_face_ids(run_dir: Path, n_faces: int | None, strict: bool = True) -> list[int] | None:
    """Per-facet source-face ids written by the sandbox, or None if absent /
    (when strict) length-mismatched — a mismatch would highlight the wrong faces,
    so callers that align facets to the mesh fall back. Pass strict=False when you
    only need facet→face lookup and don't have the mesh's facet count handy."""
    path = Path(run_dir) / "face_ids.json"
    if not path.exists():
        return None
    try:
        ids = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(ids, list):
        return None
    if strict and n_faces is not None and len(ids) != n_faces:
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


def _load_parts(run_dir: Path) -> list[dict]:
    """The sandbox part table: one entry per perceived part with a unique, human
    display name. [] for legacy runs written before parts.json."""
    path = Path(run_dir) / "parts.json"
    if not path.exists():
        return []
    try:
        parts = json.loads(path.read_text())
        return parts if isinstance(parts, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _part_for_face(parts: list[dict], brep_face_id: int) -> dict | None:
    """The part a BREP face belongs to (the clicked face's OWN part — not the
    first-in-file-order named region-mate, which used to mislabel selections)."""
    for p in parts:
        if brep_face_id in (p.get("faces") or []):
            return p
    return None


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
    run_dir: Path, face: int, max_angle_deg: float = 20.0,
    anchor: int | None = None, precise: bool = False,
) -> dict[str, Any] | None:
    """Resolve a clicked facet into a stable part description:
    {faces, display, feature, where, governed_by, centroid_mm, size_mm, normal}.
    `anchor` is the BREP face id the viewer resolved the click to (falls back to
    face_ids[face]); `precise=True` (Alt-click) scopes the description to that
    single BREP face instead of the whole part, matching the viewer highlight.
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

    face_ids = _load_face_ids(run_dir, n_faces)
    parts = _load_parts(run_dir)
    display: str | None = None
    part_meta: dict | None = None
    on_part: str | None = None
    clicked: dict | None = None

    # the clicked BREP face: trust the viewer's anchor when it's a real id
    # (guards client-supplied data), else map the seed facet through face_ids
    target: int | None = None
    if face_ids is not None:
        target = anchor if (isinstance(anchor, int) and anchor in face_ids) else face_ids[face]

    if precise and target is not None:
        # Alt-click: the user picked ONE BREP face — describe exactly that face,
        # with its parent part as context, so the note matches the highlight.
        region = {i for i, fid in enumerate(face_ids) if fid == target}
        feature = _feature_label(_faces_meta(run_dir).get(target)) or "face"
        p = _part_for_face(parts, target)
        if p and p.get("display"):
            on_part = p["display"]
    elif face_ids is not None and parts and (part_meta := _part_for_face(parts, target)) is not None:
        # Preferred: the sandbox PART TABLE — the clicked facet resolves to the
        # whole part it belongs to (the bow, not "the torso"), with a unique
        # display name.
        brep_faces = set(part_meta.get("faces") or [])
        region = {i for i, fid in enumerate(face_ids) if fid in brep_faces}
        display = part_meta.get("display")
        feature = f"`{display}`" if display else _feature_label(_faces_meta(run_dir).get(target))
        # A part can be the entire model (single-solid builds have ONE part), so
        # "the `Body`" alone tells the agent nothing about WHERE the user
        # clicked. Anchor the specific face: its surface type + local position.
        if len(brep_faces) > 1:
            seed_centroid = mesh.triangles[face].mean(axis=0)
            clicked = {
                "feature": _feature_label(_faces_meta(run_dir).get(target)) or "face",
                "centroid_mm": [round(float(v), 1) for v in seed_centroid],
            }
    elif face_ids is not None:
        # part table absent (legacy run): fall back to smooth-region grouping.
        meta_by_id = _faces_meta(run_dir)
        target_region = meta_by_id.get(target, {}).get("region")
        if target_region is not None:
            group = {fid for fid, m in meta_by_id.items() if m.get("region") == target_region}
        else:
            group = {target}
        region = {i for i, fid in enumerate(face_ids) if fid in group}
        named = next((m for fid, m in meta_by_id.items() if fid in group and m.get("name")), None)
        feature = _feature_label(named or meta_by_id.get(target))
    else:
        # no BREP map at all: coplanar flood-fill on the raw mesh.
        mesh.merge_vertices()
        region = _grow_region(mesh, face, float(np.deg2rad(max_angle_deg)))
        feature = None

    idx = sorted(region)
    if not idx:
        return None
    centroid = mesh.triangles[idx].reshape(-1, 3).mean(axis=0)
    normal = np.asarray(mesh.face_normals)[idx].mean(axis=0)
    normal = normal / (float(np.linalg.norm(normal)) or 1.0)

    lo, hi = mesh.bounds
    span = np.where((hi - lo) > 1e-6, hi - lo, 1.0)
    frac = (centroid - lo) / span

    # geometry anchor: prefer the part table's measured centroid/size (exact),
    # else derive from the selected facets so legacy runs still get an anchor.
    if part_meta and part_meta.get("centroid_mm"):
        centroid_out = [round(float(v), 1) for v in part_meta["centroid_mm"]]
    else:
        centroid_out = [round(float(v), 1) for v in centroid]
    if part_meta and part_meta.get("bbox_mm"):
        bb = part_meta["bbox_mm"]
        size_out = [round(float(bb[a + 3] - bb[a]), 1) for a in range(3)]
    else:
        sel = mesh.triangles[idx].reshape(-1, 3)
        size_out = [round(float(sel[:, a].max() - sel[:, a].min()), 1) for a in range(3)]

    return {
        "faces": [int(i) for i in idx],
        "display": display,
        "feature": feature,
        "on_part": on_part,
        "clicked": clicked,
        "where": _location_words(frac),
        "governed_by": _governing_params(run_dir, region),
        "governed_ready": affect.affect_path(run_dir).exists(),
        "centroid_mm": centroid_out,
        "size_mm": size_out,
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
    """Resolve a picked BREP edge into {edge, feature, where, length, centroid,
    on_parts, governed_by}. `on_parts` are the display names of the faces the
    edge lies between (so it can be named 'the edge where Bow meets Torso')."""
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

    # adjacent parts, and the parameters that govern them (via those parts' facets)
    parts = _load_parts(run_dir)
    on_parts: list[str] = []
    governed: list[str] = []
    adj_fids = [f for f in (edge.get("faces") or []) if isinstance(f, int)]
    if parts and adj_fids:
        face_ids = _load_face_ids(run_dir, None, strict=False)
        brep_faces: set[int] = set()
        for fid in adj_fids:
            p = _part_for_face(parts, fid)
            if p is None:
                continue
            disp = p.get("display")
            if disp and disp not in on_parts:
                on_parts.append(disp)
            brep_faces.update(p.get("faces") or [])
        if face_ids is not None:
            region = {i for i, fid in enumerate(face_ids) if fid in brep_faces}
            governed = _governing_params(run_dir, region)

    return {
        "edge": edge_id,
        "feature": feature,
        "where": _location_words(frac),
        "length_mm": edge.get("length"),
        "centroid_mm": [round(float(v), 1) for v in centroid],
        "on_parts": on_parts,
        "governed_by": governed,
    }


def _fmt(v: float) -> str:
    """Compact number: drop a trailing .0 (12.0 -> '12', 6.5 -> '6.5').
    `+ 0.0` normalizes IEEE negative zero so a rounded -0.04 prints '0', not '-0'."""
    return f"{v + 0.0:g}"


def _geo_anchor(desc: dict[str, Any]) -> str:
    """' centered at (x, y, z) mm, ~W×D×H mm' from a descriptor's centroid/size."""
    out = ""
    cen = desc.get("centroid_mm")
    if cen:
        out += " centered at (" + ", ".join(_fmt(v) for v in cen) + ") mm"
    size = desc.get("size_mm")
    if size:
        out += ", ~" + "×".join(_fmt(v) for v in size) + " mm"
    return out


def _gov_tail(desc: dict[str, Any], noun: str) -> str:
    """'(controlled by `p`)' — or, when the affect map is ready and nothing drives
    the part, an instruction to add a parameter so the user can tweak it."""
    govs = desc.get("governed_by") or []
    if govs:
        return " (controlled by " + ", ".join(f"`{g}`" for g in govs) + ")"
    if desc.get("governed_ready"):
        return f" — no parameter currently controls this {noun}; add one if the user wants to tweak it"
    return ""


def _face_phrase(desc: dict[str, Any]) -> str:
    display = desc.get("display")
    feature = desc.get("feature")
    where = ", ".join(desc.get("where") or ["center"])
    if display:
        what = f"the `{display}`"
    elif desc.get("on_part"):
        # precise (Alt-click) pick: one BREP face, named with its parent part
        what = f"a {feature} on `{desc['on_part']}` at the {where}"
    elif feature:
        what = f"a {feature} at the {where}"
    else:
        what = f"the {where} region"
    out = what + _geo_anchor(desc) + _gov_tail(desc, "part")
    # part-level picks carry the specific clicked face so "the `Body`" (which
    # may be the whole model) still tells the agent WHERE the user pointed
    clicked = desc.get("clicked")
    if clicked and clicked.get("centroid_mm"):
        at = ", ".join(_fmt(float(v)) for v in clicked["centroid_mm"])
        out += f" — the user clicked its {clicked.get('feature') or 'face'} near ({at}) mm"
    return out


def _edge_phrase(desc: dict[str, Any]) -> str:
    on = desc.get("on_parts") or []
    if len(on) >= 2:
        what = f"the {desc['feature']} where `{on[0]}` meets `{on[1]}`"
    elif len(on) == 1:
        what = f"a {desc['feature']} on `{on[0]}`"
    else:
        where = ", ".join(desc.get("where") or ["center"])
        what = f"a {desc['feature']} at the {where}"
    length = desc.get("length_mm")
    tail = f", length {length} mm" if length is not None else ""
    # the centroid disambiguates between same-length edges on the same part(s)
    return what + tail + _geo_anchor(desc) + _gov_tail(desc, "edge")


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


_MAX_PHRASES = 8  # cap so 50 picks don't produce 50 phrases in the note


def _region_phrase(run_dir: Path, region: dict) -> str | None:
    """Phrase for a brush-painted region. The viewer sends a summary — per-BREP-
    face coverage percentages plus total area and centroid — never raw facet
    indices (those don't survive regeneration). Name the covered parts so the
    agent can scope its change the same way it does for clicked faces."""
    cov = region.get("face_coverage") or []
    pairs: list[tuple[int, float]] = []
    for item in cov[:24]:  # defensive cap on client-supplied data
        try:
            fid, pct = item
            pairs.append((int(fid), float(pct)))
        except (TypeError, ValueError):
            continue
    parts = _load_parts(run_dir)
    by_part: dict[str, float] = {}
    loose: list[str] = []
    for fid, pct in pairs:
        p = _part_for_face(parts, fid)
        disp = (p or {}).get("display")
        if disp:
            by_part[disp] = max(by_part.get(disp, 0.0), pct)
        else:
            loose.append(f"face F{fid} ({pct:.0f}% covered)")
    bits = [f"{pct:.0f}% of `{d}`" for d, pct in sorted(by_part.items(), key=lambda kv: -kv[1])[:5]]
    bits += loose[:3]
    area = region.get("area_mm2")
    size = ""
    if isinstance(area, (int, float)) and area > 0:
        size = f" of ≈{area / 100:.1f} cm²" if area >= 100 else f" of ≈{area:.0f} mm²"
    c = region.get("centroid")
    where = ""
    if isinstance(c, (list, tuple)) and len(c) == 3:
        try:
            where = f", centred near ({_fmt(float(c[0]))}, {_fmt(float(c[1]))}, {_fmt(float(c[2]))}) mm"
        except (TypeError, ValueError):
            pass
    if not bits:
        # a small patch can cover <5% of every (large) face it touches — the
        # area + centroid still locate it precisely, so never drop it silently
        if not (size or where):
            return None
        return f"a hand-painted surface region{size}{where}"
    return f"a hand-painted surface region{size}{where}, covering " + ", ".join(bits)


def build_note(run_dir: Path, faces: list[int | dict] | None = None,
               edges: list[int] | None = None,
               region: dict | None = None) -> str | None:
    """Compose the agent-facing note for a whole selection — any mix of picked
    faces (a bare seed facet index, or {seed, face: BREP id, precise} from the
    part-aware viewer), edges (by id), and a brush-painted region summary.
    Picks resolving to the same part are merged, and the list is capped with a
    '…and N more' summary so a large multi-select stays legible.
    None if nothing resolves."""
    # faces: dedupe by part (multiple facets of one part -> one phrase)
    face_by_key: dict[str, dict] = {}
    for item in faces or []:
        if isinstance(item, int):
            item = {"seed": item}
        if not isinstance(item, dict) or not isinstance(item.get("seed"), int):
            continue
        anchor = item.get("face")
        d = describe_selection(
            run_dir, item["seed"],
            anchor=anchor if isinstance(anchor, int) else None,
            precise=bool(item.get("precise")),
        )
        if not d:
            continue
        key = d.get("display") or f"F{tuple(d.get('faces') or [])}"
        face_by_key.setdefault(key, d)

    # edges: group by the parts they touch ("2 edges on `Bow`")
    edge_groups: dict[tuple, dict] = {}
    for eid in edges or []:
        d = describe_edge(run_dir, eid)
        if not d:
            continue
        key = tuple(d.get("on_parts") or []) or ("edge", eid)
        g = edge_groups.setdefault(key, {"desc": d, "count": 0})
        g["count"] += 1

    phrases: list[str] = []
    for d in face_by_key.values():
        phrases.append(_face_phrase(d))
    for g in edge_groups.values():
        p = _edge_phrase(g["desc"])
        if g["count"] > 1:
            # '2 edges on `Bow`' — pluralize the leading 'a <feature>'
            p = f"{g['count']} of: {p}"
        phrases.append(p)

    if len(phrases) > _MAX_PHRASES:
        head = phrases[:_MAX_PHRASES]
        extra = len(phrases) - _MAX_PHRASES
        head.append(f"…and {extra} more selected part(s)")
        phrases = head

    # the painted region rides along after the cap — it is one phrase for what
    # may be the user's most deliberate selection, so it never gets truncated
    if region:
        rp = _region_phrase(run_dir, region)
        if rp:
            phrases.append(rp)
    return _wrap_note(phrases)
