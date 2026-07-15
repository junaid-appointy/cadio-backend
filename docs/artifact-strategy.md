# CADIO — artifact storage strategy (store source, cache display, export on demand)

> Written 2026-07-14. Companion to `storage-decision.md`. Answers "which run
> artifacts do we actually persist, and which do we regenerate?" so storage stays
> near-zero without breaking the viewer or the part-selection/affect features.
> **Decision: persist the source (`program.py` + params + chat) durably; keep a
> small decimated display mesh + facemap as a cache; generate STEP and full-res
> STL on demand at export time. Stop persisting STEP and full-res STL.**

## TL;DR

- The only irreplaceable data is **source**: `program.py` (the build123d code per
  version) + params + chat. It's kilobytes of text → lives in the metadata DB at
  **~$0**. Everything else is deterministically rebuildable from it.
- **Exports cannot be derived from a stored display mesh.** STEP is B-rep (exact
  geometry from the solids); GLB/STL are triangles. Mesh → B-rep is not
  reversible, and a full-res STL can't be reconstructed from a decimated GLB. So
  exports **must** rebuild from `program.py`. That's fine — export is a rare,
  deliberate click where a few seconds of CAD compute is acceptable.
- The viewer only needs a **decimated GLB**. Store that (small) instead of the
  full-res STL/GLB, and **generate STEP + full-res STL on demand**. Per-version
  storage drops from several MB to a few hundred KB.
- **Hard constraint:** the pick/selection + affect features are bound to the
  display mesh's exact triangle order. **Decimate first, then compute the facemap
  against the decimated mesh.** Storing a decimated display mesh whose facet order
  differs from the facemap makes clicks select the wrong part.

## What a run produces today

`engine.py` runs `program.py` + `params_in.json` in an OCCT subprocess and writes,
per run, into `~/.cadio/projects/<pid>/runs/<run_id>/`:

| File | Role | Source of truth? | Size |
|---|---|---|---|
| `program.py` | build123d code for this version | **YES** | ~KB |
| `params_in.json` | parameter values | **YES** | ~KB |
| `model.glb` | viewer mesh (today: full-res, `engine.py:262`) | no — derived | MB |
| `model.stl` | mesh export + facemap basis | no — derived | MB |
| `model.step` | B-rep export (CAD/manufacturing) | no — derived | **MB, largest** |
| `render*.png` | agent's eyes + project thumbnail | no — derived | small |
| `face_ids/faces/edges/parts.json` | pick/selection facemap | no — derived | small text |
| affect map | which params move which faces | no — derived (cache) | small text |

Chat lives in the `messages` table (DB), not on disk.

## Why exports must come from source, not from the display mesh

- **STEP is B-rep**, produced from the build123d solids — analytic/NURBS surfaces,
  exact. A GLB/STL is a **triangle soup**. There is no reliable mesh → B-rep
  inverse, so a stored mesh (any quality) cannot yield a usable STEP.
- **Full-res STL from a decimated GLB is impossible** — decimation is lossy; you
  can't add back detail you threw away.
- Therefore the durable source of truth for *any* export is `program.py`, and the
  export path re-runs it. This is the right place to spend CPU (our scarce
  resource per `storage-decision.md`): a user clicking "export STEP/STL" expects a
  brief wait, unlike a model *view*, which must be instant.

## Why decimation and the facemap are coupled

`select.py` (module header) states the invariant: a viewer click yields **one
triangle index in STL facet order — the same order `affect.py` and the browser's
STLLoader use** — and `face_ids.json` maps that index → face/part. So the facemap
is valid **only** for the exact mesh it was computed against.

Today the GLB is exported from the **full-res** mesh (`engine.py:262`). If we
simply swap in a *separately* decimated display GLB while keeping a full-res
facemap, the indices desync and clicks select the wrong part.

**Fix:** decimate once, up front; export that mesh as the GLB **and** compute the
facemap (and renders/affect) against it. One mesh, one facet order, everything
aligned. `render.py` already carries a decimator (`_decimate`,
`_RENDER_MAX_FACES = 18000`) — reuse it as the display-mesh budget, tuned by eye
so curved faces don't look faceted and picking stays accurate.

## The strategy

**Durable (metadata DB, ~KB/version, ~$0):**
- `program.py` + params + chat + light metadata (bbox, part names, one thumbnail).

**Display cache (small, regenerable):**
- One **decimated GLB** at "looks good enough" quality.
- The **facemap JSONs**, computed against that same decimated mesh.
- (Optional: don't even persist the GLB — lazy-rebuild on first open and LRU-cache
  it. Cheaper storage, costs one CAD build per cold open. Caching wins if models
  are reopened often.)

**Generated on demand, never stored:**
- **STEP** — rebuild from `program.py`, full quality.
- **Full-res STL** — rebuild from `program.py`, for download/manufacturing.

## What changes in code (contained)

1. **`engine.py` `_postprocess` / `_validate_and_convert`:** decimate the mesh
   before GLB export; compute the facemap + renders against the decimated mesh;
   stop writing `model.step` and the full-res `model.stl` on normal builds (or
   write STL to a temp path used only to derive the display mesh + maps, then drop
   it).
2. **`store.py`:** the artifacts rebuild in `_public_run` (~line 435) should stop
   advertising `step`/full `stl`; keep `glb`. Export URLs point at the new route.
3. **New `/api/projects/{pid}/runs/{run_id}/export?fmt=step|stl` route
   (`app.py`):** re-run the stored `program.py` in the engine, stream the result,
   don't persist it (or persist to a short-TTL cache).

## Decoupling win

Display quality and export quality become independent. The stored display mesh can
be as coarse as still looks/pick-clicks acceptably; exports are **always** pristine
because they rebuild from source. Lowering display fidelity costs nothing on
exports.

## The one durability caveat

Regeneration assumes the toolchain reproduces the same geometry from the same code
over time. A build123d/OCCT upgrade could change output or break an old
`program.py`. A *stored* mesh is frozen forever; a *regenerated* one is not. If a
saved/shared version must be guaranteed reproducible byte-for-byte, freeze its
artifacts (the small subset) as insurance — everything else stays regenerate-on-
demand. Pinning the sandbox toolchain version mitigates this broadly.
