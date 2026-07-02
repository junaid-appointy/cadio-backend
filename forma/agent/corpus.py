"""The playbook corpus — the moat.

Seeded from the socket-enclosure prototype learnings (3d-panel-project.md §G).
v0 keeps it as prompt text; P1 moves it to Postgres + pgvector with retrieval.
Every failed generation and support learning should land here.
"""

PROCESS_RULES = """\
PROCESS RULES (always apply):
- Photos and descriptions give SHAPE and TOPOLOGY only. Every dimension must be
  typed or confirmed by the user — never estimate a number from an image.
- Ask clarifying questions in small batches (max 4), with sensible defaults
  pre-filled. Never re-ask something already answered.
- Establish early: units, driving vs derived dimensions, tolerance/fit class
  (press 0.1mm / slide 0.2mm / loose 0.4mm — default 0.4mm global), material &
  process (default FDM/PLA), print orientation.
- Confirm the design on a summary of key dimensions and hole positions BEFORE
  presenting the model as done — wrong geometry is cheap on paper, expensive
  after export.
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
"""


def system_corpus() -> str:
    return PROCESS_RULES + "\n" + MODELING_RULES
