"""Shared (engine-agnostic) mesh validators.

Encodes the socket-enclosure gotchas: tangent solids silently produce
non-manifold STL; verify actual geometry content, never assumptions.
"""

from __future__ import annotations

from pathlib import Path

from ..engines.base import ValidationIssue, ValidationReport


def validate_mesh(stl_path: Path, expected_bbox_size: list[float] | None = None):
    """Returns (report, trimesh_mesh_or_None)."""
    issues: list[ValidationIssue] = []
    mesh = None
    try:
        import trimesh

        mesh = trimesh.load(str(stl_path), force="mesh")
    except Exception as exc:  # unreadable STL is a hard failure
        issues.append(ValidationIssue("error", "stl_unreadable", f"could not read STL: {exc}"))
        return ValidationReport(ok=False, issues=issues), None

    if len(mesh.faces) == 0:
        issues.append(ValidationIssue("error", "empty_mesh", "STL contains no triangles"))
        return ValidationReport(ok=False, issues=issues), mesh

    if not mesh.is_watertight:
        issues.append(
            ValidationIssue(
                "error",
                "not_watertight",
                "mesh is not watertight (check for tangent solids — overlap them, never let them touch at a point)",
            )
        )
    if not mesh.is_winding_consistent:
        issues.append(ValidationIssue("error", "winding", "inconsistent triangle winding"))
    if mesh.is_watertight and mesh.volume <= 0:
        issues.append(ValidationIssue("error", "non_positive_volume", "mesh volume is not positive"))

    if expected_bbox_size is not None:
        actual = (mesh.bounds[1] - mesh.bounds[0]).tolist()
        for axis, (want, got) in enumerate(zip(expected_bbox_size, actual)):
            if abs(want - got) > max(0.01 * max(want, 1.0), 0.05):
                issues.append(
                    ValidationIssue(
                        "error",
                        "bbox_mismatch",
                        f"axis {'XYZ'[axis]}: BREP reports {want:.3f}mm but mesh measures {got:.3f}mm",
                    )
                )

    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues), mesh
