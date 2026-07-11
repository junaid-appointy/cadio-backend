# Face/Edge Selection Accuracy — Diagnosis & Fix Plan

Status: diagnosed 2026-07-08, IMPLEMENTED 2026-07-09 (regions + region-aware
select.py + region/precise frontend selection + screen-space edge picking +
denser edges + corpus nudge). The A-plan's naive tangent-union over-merged
(one 74% blob) — shipped version merges same-surface large faces + attaches
sliver clusters to their single largest neighbour instead.

## Symptom (user report)
Clicking a part of the model selects **more** area than intended, or **less**
(a tiny strip / a sliver), instead of the part the user perceives.

## What was ruled out (verified — do not re-investigate)
The facet↔face index mapping is **correct**. On the 12 most recent real runs,
`model.stl` triangle count == `face_ids.json` length exactly (e.g. pistol run
`afc189e599f8/20260708-155220-680`: 19,866 == 19,866). `model.stl` is never
rewritten after `write_face_map` (validation cleans a copy in memory only for
the GLB), and the viewer's STLLoader preserves facet order. Picking selects
exactly the BREP face the click hit — the problem is that a raw BREP face
often isn't what a human means by "this part".

## Root causes (grounded in the pistol run: 176 faces, 19,866 triangles)

### 1. Selection unit = raw OCCT BREP face ≠ perceived "part"
Boolean unions + fillets reshape the face topology in two opposite ways:

- **"Extra area"** — OCCT fuses tangent surfaces into single mega-faces.
  The pistol's 6 largest faces are `freeform` faces covering **7.5–19.3% of
  the whole model EACH** (id=120: 3,832 tris = 19.3%). One click on "the grip's
  side" selects a face that flows over grip + body + trigger guard.
- **"Less area"** — fillets/chamfers generate transition strips: **144 of 176
  faces are slivers of ≤8 triangles**. A click landing on one selects a barely
  visible 2–8-triangle ribbon, reading as "almost nothing got selected".
- Related: booleans also split what reads as one feature (e.g. a cylindrical
  hole) into multiple faces (half-cylinders), so "the hole" is 2+ faces.

### 2. Edge picking: world-space tolerance too coarse + no occlusion/screen test
`Viewer.tsx` picks the nearest edge polyline to the clicked *surface point* in
**3D world distance** with tolerance `max(1, boundingSphereRadius * 0.08)` —
about **8–10mm** on a pistol-sized model. But fillet edges come in tightly
packed pairs (two boundary edges 1–3mm apart; this model has **494 edges**),
so the wrong twin frequently wins. There is also no occlusion awareness: an
edge behind a thin wall (near in 3D, invisible on screen) can beat the edge
under the cursor.

## Fix plan

### A. Sandbox: emit smooth-region groups (the "part" the user means)
In `cadio/engines/precision/_sandbox_runner.py`, extend `write_face_map`:
1. Build edge→faces adjacency with `TopExp.MapShapesAndAncestors_s(part.wrapped,
   TopAbs_EDGE, TopAbs_FACE, ...)`.
2. For each shared edge, measure the dihedral angle between the two faces
   (sample normals near the shared edge midpoint via `BRepAdaptor_Surface` /
   `GeomLProp_SLProps`). If the surfaces meet **tangentially / smoothly**
   (angle < ~25–30°), the faces belong to the same *smooth region*.
3. Union-find faces into regions; write a `region` int per face into
   `faces.json` (additive, backward compatible — old runs simply lack it).
   Cylinder halves split by booleans share smooth seams → rejoined. Fillet
   slivers are tangent to both neighbours → absorbed into the big region.
   (Optional refinement to stop fillets bridging two otherwise-distinct parts:
   treat a sliver as glue only when BOTH its long-side neighbours are smooth.)

### B. Frontend: select by region by default, keep face precision available
In `Viewer.tsx` / `Workspace.tsx`:
- Default click in Faces mode selects the whole **smooth region** (all faces
  with the same `region`) — matches the perceived part; fixes both "sliver"
  and "half-cylinder" complaints. Fused mega-faces stay one face (BREP limit),
  but region selection at least makes behaviour consistent and predictable.
- **Alt-click** (or a small "precise" toggle in the pick bar) selects the
  single BREP face — the current behaviour, still needed for advanced edits.
- Hover highlight must use the SAME region logic, so the preview shows exactly
  what a click will select. Selection payloads to the agent (`select.py`
  `build_note`) should mention the region's face set, not just the seed facet.
- Runs without `region` data (old runs): fall back to current per-face picking.

### C. Frontend: screen-space edge picking
Replace the pure world-distance `nearestEdge`:
1. Project candidate edge polyline segments to screen (NDC→pixels) and pick
   the edge minimising **pixel distance to the cursor**, threshold ~8px.
2. Pre-filter candidates to those within a small world distance of the clicked
   surface point (reuse the raycast hit), which mostly handles occlusion; a
   depth tie-break (prefer the segment nearest the camera) resolves the rest.
3. Raise polyline sampling density in `write_edge_map` for long curved edges —
   the current cap (`min(64, length/1.5)` points) is fine under ~100mm but
   coarsens on big models; scale the cap with edge length (e.g. up to 256).
4. Hover uses the identical function, so hover = pick.

### D. Corpus nudge (small)
`features()` naming exists but is barely used (this model named only 2 faces).
Add one line to the corpus discipline: name every part a user is likely to
click (grip, barrel, trigger_guard, muzzle...) — named features give clean
semantic selection and better agent-facing notes.

## Verification
- Pistol model: click the grip side → whole grip region highlights (not the
  fused mega-face spilling into the body — unless BREP fused them into one
  face, in which case behaviour is unchanged but hover shows it honestly);
  click a fillet strip → the strip's parent region highlights, not a sliver.
- Hole split into half-cylinders → one click selects the full hole barrel.
- Edge mode: zoom out, click near a fillet edge pair → the edge under the
  cursor (screen-nearest) is chosen; an edge behind a wall is never chosen.
- Old runs (no `region` field) still pick per-face without errors.
- `cd frontend && npx tsc --noEmit && npm run build`; rebuild a model end-to-end
  and check `faces.json` gained `region` while `face_ids.json` length still
  equals the STL triangle count.
