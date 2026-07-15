"""Part-level regression guard.

An edit scoped to one feature ("make the handle thicker") should leave every
other part alone — but the agent regenerates the whole program each turn, and
nothing structurally prevents unrelated features from drifting (rewritten
code, subtly changed dimensions, a shared variable cascading further than
intended). This module compares a freshly built run's part table (parts.json)
against the newest sibling run's and reports parts that changed. The warning
rides into the agent's validation report, where the standing "resolve
validation issues before answering" instruction makes it either restore the
parts or consciously confirm the change was requested.

Warnings only — never flips validation.ok. A legitimate global edit ("scale
everything up 2x") reads as one collapsed summary line, not a wall of noise.
Comparison is against the newest previous build, so during an agent's own
iteration loop the FIRST build that breaks a part gets flagged — exactly when
the agent is still in the loop and can fix it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..engines.base import ValidationIssue

_AREA_TOL = 0.12  # relative area change below this is tessellation noise
_MAX_LISTED = 5  # cap the per-part list so the note stays legible


def _load_parts(run_dir: Path) -> dict[str, dict]:
    """Part table keyed by display name (unique within a run). {} if absent."""
    try:
        parts = json.loads((run_dir / "parts.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(parts, list):
        return {}
    return {p["display"]: p for p in parts if isinstance(p, dict) and p.get("display")}


def _prev_run_dir(run_dir: Path) -> Path | None:
    """Newest sibling run that has a part table (by parts.json mtime)."""
    best: tuple[float, Path] | None = None
    try:
        for d in run_dir.parent.iterdir():
            if d == run_dir or not d.is_dir():
                continue
            try:
                m = (d / "parts.json").stat().st_mtime
            except OSError:
                continue  # no part table -> failed/legacy/preview run
            if best is None or m > best[0]:
                best = (m, d)
    except OSError:
        return None
    return best[1] if best else None


def part_drift_issue(run_dir: Path, model_bbox: dict | None) -> ValidationIssue | None:
    """One warning summarizing parts that changed vs the previous build, or
    None when there is nothing to compare or nothing moved."""
    new = _load_parts(run_dir)
    if not new:
        return None
    prev_dir = _prev_run_dir(run_dir)
    if prev_dir is None:
        return None
    old = _load_parts(prev_dir)
    if not old:
        return None

    # "moved" threshold scales with the model so it means the same at any size
    diag = 0.0
    size = (model_bbox or {}).get("size")
    if isinstance(size, (list, tuple)) and len(size) == 3:
        diag = (size[0] ** 2 + size[1] ** 2 + size[2] ** 2) ** 0.5
    move_tol = max(1.0, 0.02 * diag)

    changed: list[str] = []
    for disp, op in old.items():
        np_ = new.get(disp)
        if np_ is None:
            changed.append(f"`{disp}` is gone")
            continue
        bits: list[str] = []
        oa = float(op.get("area_mm2") or 0.0)
        na = float(np_.get("area_mm2") or 0.0)
        if max(oa, na) > 0 and abs(na - oa) / max(oa, na) > _AREA_TOL:
            pct = (na - oa) / oa * 100.0 if oa else 100.0
            bits.append(f"area {pct:+.0f}%")
        oc, nc = op.get("centroid_mm"), np_.get("centroid_mm")
        if isinstance(oc, list) and isinstance(nc, list) and len(oc) == 3 == len(nc):
            d = sum((a - b) ** 2 for a, b in zip(oc, nc)) ** 0.5
            if d > move_tol:
                bits.append(f"moved {d:.1f}mm")
        if bits:
            changed.append(f"`{disp}` ({', '.join(bits)})")
    if not changed:
        return None

    total = len(old)
    if len(changed) > _MAX_LISTED and len(changed) >= 0.6 * total:
        # model-wide reshape: one line, not a wall of per-part noise
        msg = (
            f"{len(changed)} of {total} pre-existing parts changed vs the previous "
            "version. Fine if the user asked for a model-wide change; otherwise "
            "unrelated parts drifted — rebuild preserving them."
        )
    else:
        # name the drifted parts — that's what lets the agent restore them
        listed = "; ".join(changed[:_MAX_LISTED])
        more = f"; …and {len(changed) - _MAX_LISTED} more" if len(changed) > _MAX_LISTED else ""
        msg = (
            f"vs the previous version, parts outside the request may have changed: "
            f"{listed}{more}. If the user didn't ask for these to change, this is "
            "unintended drift — rebuild preserving them exactly."
        )
    return ValidationIssue("warning", "part_drift", msg)
