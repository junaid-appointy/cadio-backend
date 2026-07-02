"""Example precision-engine program: parametric open-top enclosure tray
with rounded corners. Direct descendant of the socket back box."""

from build123d import Axis, Box, Pos, fillet

PARAMS = [
    {"name": "length", "default": 120.0, "type": "number", "min": 10, "max": 500,
     "unit": "mm", "description": "Outer length (X)", "group": "Size"},
    {"name": "width", "default": 60.0, "type": "number", "min": 10, "max": 500,
     "unit": "mm", "description": "Outer width (Y)", "group": "Size"},
    {"name": "height", "default": 30.0, "type": "number", "min": 5, "max": 300,
     "unit": "mm", "description": "Outer height (Z)", "group": "Size"},
    {"name": "wall", "default": 2.0, "type": "number", "min": 1.2, "max": 10,
     "unit": "mm", "description": "Wall thickness", "group": "Walls"},
    {"name": "floor", "default": 3.0, "type": "number", "min": 1.2, "max": 10,
     "unit": "mm", "description": "Floor thickness", "group": "Walls"},
    {"name": "corner_radius", "default": 6.0, "type": "number", "min": 0.5, "max": 30,
     "unit": "mm", "description": "Vertical corner fillet radius", "group": "Style"},
]


def build(p: dict):
    L, W, H = p["length"], p["width"], p["height"]
    t, fl, r = p["wall"], p["floor"], p["corner_radius"]

    assert L - 2 * t > 1 and W - 2 * t > 1, "walls too thick for footprint"
    assert fl < H, "floor thicker than the box"
    assert r < min(L, W) / 2 - t, "corner radius too large"

    outer = Box(L, W, H)
    # cavity is open at the top: extends past the top face, floor stays solid
    cavity = Pos(0, 0, fl) * Box(L - 2 * t, W - 2 * t, H)
    tray = outer - cavity
    tray = fillet(tray.edges().filter_by(Axis.Z), radius=r)
    return tray
