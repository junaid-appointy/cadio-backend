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

matplotlib.use("Agg")  # offscreen; must precede pyplot import
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
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


def _shaded_faces(mesh) -> np.ndarray:
    """Per-face gold colour shaded by normal·light (Lambert + ambient)."""
    normals = np.asarray(mesh.face_normals)
    lit = np.clip(normals @ _LIGHT, 0.0, 1.0)
    shade = 0.35 + 0.65 * lit  # ambient floor so back faces aren't black
    base = np.array([0.91, 0.70, 0.29])  # forma gold
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
    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
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
    plt.close(fig)


def render_views(stl_path: Path, out_dir: Path, size: int = 768) -> dict[str, str]:
    """Render the canonical views PLUS a cut-away section, so the agent can see
    interior features (walls, floors, bosses, cavities) it might have omitted.
    Returns {view_name: png_path}. Best-effort — returns {} on any failure."""
    out_dir = Path(out_dir)
    try:
        import trimesh

        mesh = trimesh.load(str(stl_path), force="mesh", process=False)
        if mesh.faces is None or len(mesh.faces) == 0:
            return {}
        verts = np.asarray(mesh.vertices)
        center = verts.mean(axis=0)
        reach = float(np.abs(verts - center).max()) * 1.1 or 1.0

        outputs: dict[str, str] = {}
        for name, (elev, azim) in _VIEWS.items():
            path = out_dir / f"view_{name}.png"
            _render_mesh(mesh, elev, azim, path, size, center, reach)
            outputs[name] = str(path)

        # cross-section: slice through the centre along the longest horizontal
        # axis and render the cut half, exposing the interior.
        try:
            sizes = mesh.bounds[1] - mesh.bounds[0]
            axis = int(np.argmax(sizes[:2]))  # X or Y, whichever is longer
            normal = np.zeros(3)
            normal[axis] = 1.0
            half = mesh.slice_plane(plane_origin=center, plane_normal=normal, cap=True)
            if half is not None and len(half.faces) > 0:
                sec = out_dir / "view_section.png"
                # look into the cut face
                _render_mesh(half, 18, -60 if axis == 0 else -30, sec, size, center, reach)
                outputs["section"] = str(sec)
        except Exception:
            pass

        iso = out_dir / "view_iso.png"
        if iso.exists():
            import shutil

            shutil.copyfile(iso, out_dir / "render.png")
        return outputs
    except Exception:
        return {}
