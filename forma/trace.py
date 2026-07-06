"""Image → extrudable outline (logos, icons, flat graphics → 3D).

A flat logo can't be faithfully reconstructed from primitives (the agent drops
parts). Instead we TRACE the actual image: threshold foreground from
background, find contours (with holes), simplify, and emit a self-contained
build123d program that raises the traced silhouette on a backing plate — a
printable badge that reproduces the whole logo, every part included.

The generated program bakes the contour coordinates and exposes width / logo
height / base thickness as PARAMS, so it flows through the normal run pipeline
(preview, param sliders, highlighting, export) like any other model.
"""

from __future__ import annotations

import json
from pathlib import Path


def trace_polygons(image_path: Path, max_points: int = 240) -> dict:
    """Trace an image into normalized polygons with holes.
    Returns {"polys": [{"outer": [[x,y],...], "holes": [[[x,y],...],...]}, ...],
             "aspect": h/w}. Coordinates are normalized so width spans 0..1,
    y up. Raises ValueError if nothing traceable is found."""
    import cv2
    import numpy as np

    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("could not read image")

    # foreground mask
    if img.ndim == 3 and img.shape[2] == 4:
        # RGBA: prefer alpha (the logo is the opaque part)
        alpha = img[:, :, 3]
        if alpha.min() < 250:  # a real alpha channel
            mask = (alpha > 100).astype("uint8") * 255
        else:
            mask = _threshold_luma(img[:, :, :3])
    elif img.ndim == 3:
        mask = _threshold_luma(img[:, :, :3])
    else:
        mask = _threshold_luma(img)

    H, W = mask.shape
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("no shape found in the image")

    # RETR_CCOMP: hierarchy[i] = [next, prev, first_child, parent].
    # parent == -1 → an outer boundary; otherwise it's a hole.
    hierarchy = hierarchy[0]
    min_area = 0.0005 * H * W  # drop specks
    polys = []
    for i, c in enumerate(contours):
        if hierarchy[i][3] != -1:
            continue  # a hole; handled with its parent
        if cv2.contourArea(c) < min_area:
            continue
        outer = _simplify(c, W, H)
        holes = []
        child = hierarchy[i][2]
        while child != -1:
            if cv2.contourArea(contours[child]) >= min_area:
                holes.append(_simplify(contours[child], W, H))
            child = hierarchy[child][0]
        polys.append({"outer": outer, "holes": holes})

    if not polys:
        raise ValueError("no shape large enough to trace")

    # budget total points so the baked program stays small
    total = sum(len(p["outer"]) + sum(len(h) for h in p["holes"]) for p in polys)
    if total > max_points:
        polys = _decimate(polys, max_points)

    return {"polys": polys, "aspect": H / W}


def _threshold_luma(bgr):
    import cv2

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    # Otsu split; the logo is usually the brighter part on a dark tile. If the
    # "foreground" ends up being most of the image, invert (light background).
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if mask.mean() > 127:
        mask = 255 - mask
    return mask


def _simplify(contour, W, H):
    import cv2

    eps = 0.004 * cv2.arcLength(contour, True)
    pts = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
    # normalize: x in 0..1 across the width, y up
    return [[float(x) / W, float(H - y) / W] for x, y in pts]


def _decimate(polys, max_points):
    # crude uniform decimation of the largest rings first
    import cv2  # noqa: F401

    while sum(len(p["outer"]) + sum(len(h) for h in p["holes"]) for p in polys) > max_points:
        # find the longest ring and drop every other point
        longest = max(
            ([p, "outer", None] for p in polys),
            key=lambda t: len(t[0]["outer"]),
        )
        ring = longest[0]["outer"]
        if len(ring) <= 8:
            break
        longest[0]["outer"] = ring[::2]
    return polys


def generate_program(trace: dict, width_mm: float = 40.0,
                     logo_mm: float = 2.0, base_mm: float = 1.5) -> str:
    """Emit a self-contained build123d program: the traced logo raised on a
    rounded backing plate. Width / logo height / base thickness are PARAMS."""
    data = json.dumps(trace["polys"])
    return f'''
from build123d import *

# Traced from an image — the logo silhouette, raised on a backing plate.
POLYS = {data}          # normalized: x in 0..1, y up
ASPECT = {trace["aspect"]!r}

PARAMS = [
    {{"name": "width", "default": {width_mm}, "type": "number", "min": 10, "max": 200,
      "unit": "mm", "description": "Overall width", "group": "Size"}},
    {{"name": "logo_height", "default": {logo_mm}, "type": "number", "min": 0.4, "max": 10,
      "unit": "mm", "description": "Raised logo height", "group": "Relief"}},
    {{"name": "base_thickness", "default": {base_mm}, "type": "number", "min": 0, "max": 10,
      "unit": "mm", "description": "Backing plate thickness (0 = logo only)", "group": "Relief"}},
    {{"name": "base_margin", "default": 3.0, "type": "number", "min": 0, "max": 20,
      "unit": "mm", "description": "Border around the logo", "group": "Relief"}},
]


def _faces(scale, dx, dy):
    faces = []
    for poly in POLYS:
        outer = [(x * scale + dx, y * scale + dy) for x, y in poly["outer"]]
        f = make_face(Polyline(*outer, close=True))
        for hole in poly["holes"]:
            hp = [(x * scale + dx, y * scale + dy) for x, y in hole]
            f = f - make_face(Polyline(*hp, close=True))
        faces.append(f)
    return faces


def build(p):
    W = p["width"]
    scale = W  # x already spans 0..1
    # centre the logo on the origin
    dx, dy = -W / 2, -(W * ASPECT) / 2
    logo_faces = _faces(scale, dx, dy)

    part = None
    base_t = p["base_thickness"]
    if base_t > 0:
        m = p["base_margin"]
        bw, bh = W + 2 * m, W * ASPECT + 2 * m
        base = extrude(Rectangle(bw, bh), amount=base_t)
        part = base
        for f in logo_faces:
            part += extrude(Pos(0, 0, base_t) * f, amount=p["logo_height"])
    else:
        for f in logo_faces:
            solid = extrude(f, amount=p["logo_height"])
            part = solid if part is None else part + solid

    assert part is not None, "no geometry produced from the trace"
    return part
'''
