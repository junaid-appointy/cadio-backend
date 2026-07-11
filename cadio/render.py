"""Headless multi-view rendering — the agent's eyes.

Renders a mesh to labelled PNGs (iso / front / top / right) so a vision model
can SEE what it built and critique shape/proportion before presenting it, and
so projects get a thumbnail. Uses matplotlib's Agg backend: no display, no
GPU, no OpenGL — works headless on macOS/Linux every time. Quality is modest
but more than enough to judge form (which the numeric validation can't).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # offscreen; must precede any figure work
import numpy as np  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402  (OO API — no global pyplot state, thread-safe)
from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection  # noqa: E402

# (elev, azim) per canonical view
_VIEWS = {
    "iso": (26, -60),
    "front": (0, -90),
    "top": (89.9, -90),
    "right": (0, 0),
}
_LIGHT = np.array([0.4, -0.5, 0.8])
_LIGHT = _LIGHT / np.linalg.norm(_LIGHT)

# The agent judges FORM (shape, proportion, missing/misplaced features), not
# tessellation, so we render a decimated proxy — matplotlib's 3D backend is
# Python-per-polygon, so a 270K-facet mesh takes seconds. ~18K facets looks the
# same at 512px but renders ~10x faster.
_RENDER_MAX_FACES = 18000
_RENDER_SIZE = 512


def _decimate(mesh, max_faces: int):
    """Reduce the mesh to at most `max_faces` triangles for rendering. Returns the
    original when it's already small enough or if simplification is unavailable."""
    try:
        n = len(mesh.faces)
    except Exception:
        return mesh
    if n <= max_faces:
        return mesh
    try:
        import fast_simplification

        v, f = fast_simplification.simplify(
            np.asarray(mesh.vertices), np.asarray(mesh.faces),
            target_count=max_faces,
        )
        import trimesh

        return trimesh.Trimesh(vertices=v, faces=f, process=False)
    except Exception:
        return mesh  # best-effort: fall back to rendering the full mesh


def _shaded_faces(mesh) -> np.ndarray:
    """Per-face gold colour shaded by normal·light (Lambert + ambient)."""
    normals = np.asarray(mesh.face_normals)
    lit = np.clip(normals @ _LIGHT, 0.0, 1.0)
    shade = 0.35 + 0.65 * lit  # ambient floor so back faces aren't black
    base = np.array([0.91, 0.70, 0.29])  # cadio gold
    return np.clip(shade[:, None] * base[None, :], 0, 1)


def _feature_edges(mesh):
    """Line segments for edges where adjacent faces meet at a sharp angle
    (> ~22°) — the outlines of holes, cutouts, grooves and corners."""
    try:
        angles = mesh.face_adjacency_angles
        edges = mesh.face_adjacency_edges[angles > 0.38]  # ~22 degrees
        return mesh.vertices[edges]
    except Exception:
        return None


def _render_mesh(mesh, elev, azim, path: Path, size: int, center, reach) -> None:
    verts = np.asarray(mesh.vertices)
    tris = verts[np.asarray(mesh.faces)]
    colors = _shaded_faces(mesh)
    # OO Figure (not pyplot): no shared global state, so several of these can run
    # on separate threads at once (see render_views).
    fig = Figure(figsize=(size / 100, size / 100), dpi=100)
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(tris, facecolors=colors, edgecolors="none"))
    # outline sharp feature edges (holes, cuts, corners) so misplaced/stray
    # features are visible — without the noise of every triangle edge.
    segs = _feature_edges(mesh)
    if segs is not None and len(segs):
        ax.add_collection3d(Line3DCollection(segs, colors="#2a1e0a", linewidths=0.6))
    for axis, c in zip("xyz", center):
        getattr(ax, f"set_{axis}lim")(c - reach, c + reach)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=elev, azim=azim)
    ax.set_axis_off()
    fig.patch.set_facecolor("#14171c")
    fig.savefig(str(path), facecolor="#14171c", bbox_inches="tight", pad_inches=0)


def render_views(stl_path: Path, out_dir: Path, size: int = _RENDER_SIZE) -> dict[str, str]:
    """Render the canonical views PLUS a cut-away section, so the agent can see
    interior features (walls, floors, bosses, cavities) it might have omitted.
    Returns {view_name: png_path}. Best-effort — returns {} on any failure.

    The mesh is decimated to a proxy first, because this sits on the build's
    critical path (it blocks the agent's turn) and matplotlib's 3D backend costs
    seconds on a full detailed mesh. Rendering is sequential: matplotlib is
    GIL-bound and not thread-safe, so a pool doesn't parallelize it."""
    out_dir = Path(out_dir)
    try:
        import trimesh

        mesh = trimesh.load(str(stl_path), force="mesh", process=False)
        if mesh.faces is None or len(mesh.faces) == 0:
            return {}

        center = np.asarray(mesh.vertices).mean(axis=0)
        reach = float(np.abs(np.asarray(mesh.vertices) - center).max()) * 1.1 or 1.0

        # decimate ONCE, then reuse the proxy for every view and for the section
        # slice — slicing 18K faces is far cheaper than slicing the full mesh, and
        # the cut-away is only there for the agent to eyeball interior form.
        proxy = _decimate(mesh, _RENDER_MAX_FACES)

        section_mesh = None
        try:
            sizes = mesh.bounds[1] - mesh.bounds[0]
            axis = int(np.argmax(sizes[:2]))  # X or Y, whichever is longer
            normal = np.zeros(3)
            normal[axis] = 1.0
            half = proxy.slice_plane(plane_origin=center, plane_normal=normal, cap=True)
            if half is not None and len(half.faces) > 0:
                section_mesh = (half, 18, -60 if axis == 0 else -30)
        except Exception:
            pass

        jobs: list[tuple[str, object, float, float]] = [
            (name, proxy, elev, azim) for name, (elev, azim) in _VIEWS.items()
        ]
        if section_mesh is not None:
            jobs.append(("section", section_mesh[0], section_mesh[1], section_mesh[2]))

        outputs: dict[str, str] = {}
        for name, m, elev, azim in jobs:
            path = out_dir / f"view_{name}.png"
            _render_mesh(m, elev, azim, path, size, center, reach)
            outputs[name] = str(path)

        iso = out_dir / "view_iso.png"
        if iso.exists():
            import shutil

            shutil.copyfile(iso, out_dir / "render.png")
        return outputs
    except Exception:
        return {}
