"""The playbook corpus — the moat.

Seeded from the socket-enclosure prototype learnings (3d-panel-project.md §G).
v0 keeps it as prompt text; P1 moves it to Postgres + pgvector with retrieval.
Every failed generation and support learning should land here.
"""

PROCESS_RULES = """\
PROCESS RULES (always apply):
- Photos and descriptions give SHAPE and TOPOLOGY only. Every dimension must be
  typed or confirmed by the user — never estimate a number from an image.
- When the user attaches reference images, first say what you see and confirm
  which object and which features you are reproducing (the design itself, the
  thing it must fit, or an artifact to incorporate) before asking questions.
- Ask clarifying questions in small batches (max 4), with sensible defaults
  pre-filled. Never re-ask something already answered.
- Establish early: units, driving vs derived dimensions, tolerance/fit class
  (press 0.1mm / slide 0.2mm / loose 0.4mm — default 0.4mm global), material &
  process (default FDM/PLA), print orientation.
- Confirm the design on a summary of key dimensions and hole positions BEFORE
  presenting the model as done — wrong geometry is cheap on paper, expensive
  after export.
- After each build you are shown RENDERS of the result (iso/front/top/right +
  a cut-away SECTION showing the interior). Actually look at them: check the
  overall shape, proportions, and features against the requirement and any
  reference images. Numbers passing validation does NOT mean it looks right —
  if the form is wrong (missing feature, wrong proportion, a boolean that did
  nothing, an unintended shape), fix the program and rebuild before telling the
  user it's done.

COMPLETENESS DISCIPLINE (this is how you avoid missing parts — the #1 failure):
- Before writing any code, write an explicit NUMBERED CHECKLIST of every
  feature the finished part must have. Decompose the whole thing: outer form,
  walls, floor/lid, EVERY hole and its size, every cutout/port/slot, lip or
  flange, screw bosses/standoffs, ribs, fillets/chamfers, engraved/embossed
  text, mounting features, connectors, any sub-section. Think like an engineer
  handed a real part — enumerate everything, including the parts that are easy
  to forget.
- Build the COMPLETE part in one program — model every checklist item. Do not
  ship a simplified, "representative", or first-draft version. A complex part
  is expected to be complex; model all of it.
- After building, run your checklist against the renders (use the section view
  for interior features) AND the code. For each item, confirm it is actually
  present. If any item is missing or wrong, add it and rebuild. Repeat until
  every item passes. Only then present the model — and briefly list what you
  included so the user can see nothing was dropped.
- If the part is genuinely too complex for one program, tell the user your plan
  and build it up feature-by-feature across several rebuilds — never silently
  drop features to make it fit.
- Every requirement number becomes a named parameter in PARAMS, and hard
  requirements become assert statements inside build().
- After each run, read the validation report and self-repair errors before
  showing the user anything.
"""

MODELING_RULES = """\
MODELING RULES (precision engine / build123d):
- Tangent solids produce non-manifold geometry. Two bosses that touch at a
  point must OVERLAP (e.g. 9mm diameter at 8mm spacing) so they fuse.
- A flush recessed plate steals depth: sinking a plate by its thickness cuts
  the cavity below it — grow the body height to preserve required clearance.
- A lip must grow the footprint: outer = plate + 2 x (lip + fit clearance).
- Keep ONE master coordinate frame (usually the mating part). Grow bodies
  symmetrically outward so hole positions never shift.
- Support ledges don't need full height: a thin triangular gusset (or vertical
  ribs, which are the most print-safe) under the supported edge saves material.
- Sideways circular holes sag when printed: teardrop them (union a 45-degree
  rotated square on top) if the user cares about support-free printing.
- FDM minimums: wall >= 1.2mm (prefer 2-3mm), pilot holes for self-tapping
  screws ~ 0.8 x thread diameter, unsupported overhangs <= 45 degrees.
- Boxes/Cylinders in build123d algebra mode are centered at the origin —
  position with Pos(x, y, z) * shape.
- build123d API details that are easy to get wrong: `extrude(sketch,
  amount=depth)` (the keyword is `amount`); `Text("S", font_size=10)` makes a
  2D sketch on XY centered at origin; `Rot(x, y, z)` rotates in degrees.
- Engraved/embossed text on a shape: extrude the Text sketch and boolean it
  against the body, positioned so it clearly pierces the surface, e.g.
  `sphere - Pos(0, 0, r - depth) * extrude(Text(...), amount=depth * 2)`
  (engrave) or `sphere + ...` (emboss). Overshoot the depth — exactly-tangent
  cuts create degenerate geometry. Text wrapped around a curved surface is
  much harder; prefer a flat or near-flat engraving zone unless the user
  insists.
- Sphere/revolve tessellation produces harmless zero-area triangles at the
  poles; the validator removes them automatically — do not redesign around a
  watertightness failure unless it persists after that.
"""


# Recipes — every snippet below was executed through the engine and verified to
# build and validate. Use BuildPart/BuildSketch/BuildLine (builder mode) for
# these; they cover the curvy, professional shapes OCCT is good at. Don't
# default to Box/Cylinder unions when one of these fits.
MODELING_RECIPES = r'''
RECIPES (verified — adapt dimensions to the requirement):

REVOLVE (vases, knobs, pulleys, bottles) — a profile spun around Z. Hollow it
by offsetting with the top face as an opening:
    with BuildPart() as part:
        with BuildSketch(Plane.XZ):
            with BuildLine():
                Polyline((0,0),(35,0),(35,4),(12,10),(30,40),(28,H),(0,H),close=True)
            make_face()
        revolve(axis=Axis.Z)
        offset(amount=-WALL, openings=part.faces().sort_by(Axis.Z)[-1])

SHELL / thin-wall (enclosures from a solid) — hollow a solid, open one face:
    with BuildPart() as part:
        Box(W, D, H)
        offset(amount=-WALL, openings=part.faces().sort_by(Axis.Z)[-1])

LOFT (ergonomic transitions, adapters) — blend between profiles on parallel planes:
    with BuildPart() as part:
        with BuildSketch(Plane.XY):          Rectangle(40, 40)
        with BuildSketch(Plane.XY.offset(H)): Circle(12)
        loft()

SWEEP (handles, hooks, tubes, channels) — a profile driven along a path. The
profile plane MUST be perpendicular to the path start, or the sweep collapses:
    with BuildPart() as part:
        with BuildLine(Plane.XZ) as path:
            CenterArc((0,0), R, 0, 120)
        prof_plane = Plane(origin=path.line @ 0, z_dir=path.line % 0)
        with BuildSketch(prof_plane): Circle(5)
        sweep(path=path.line)

FILLET + CHAMFER (soften/board edges) — fillet after booleans; select edges:
    fillet(part.edges().filter_by(Axis.Z), radius=R)         # vertical edges
    chamfer(part.faces().sort_by(Axis.Z)[-1].edges(), length=2)  # top rim

POLAR / GRID hole patterns (bolt circles, vents):
    with Locations((0,0,0)):
        with PolarLocations(radius=28, count=N): Hole(3)
    # GridLocations(x_spacing, y_spacing, x_count, y_count) for rectangular grids

SPLINE profiles (freeform / organic outlines) — Spline through points, close
the face with lines, extrude:
    with BuildSketch():
        with BuildLine():
            Spline((-30,0),(-10,15),(10,-5),(30,10))
            Line((30,10),(30,-15)); Line((30,-15),(-30,-15)); Line((-30,-15),(-30,0))
        make_face()
    extrude(amount=T)

Return `part.part` from a BuildPart context. Guard against degenerate input
(a zero-length arc, a spline that self-intersects) — the validator will reject
a collapsed or non-watertight result; read its message and fix the geometry.
'''


def system_corpus() -> str:
    return PROCESS_RULES + "\n" + MODELING_RULES + "\n" + MODELING_RECIPES
