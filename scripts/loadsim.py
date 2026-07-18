#!/usr/bin/env python3
"""Load simulation for the 2GB / 1.5-CPU tier (run against the production image):

    docker build -t cadio-backend:local .
    docker run --rm --memory=2g --memory-swap=2g --cpus=1.5 -p 8000:8000 \
        -e CADIO_HOME=/data cadio-backend:local
    python scripts/loadsim.py --users 4 --rounds 3

Drives the engine exactly like the workspace does — canned-program builds via
POST /api/projects/{pid}/execute (no LLM keys needed), slider-style preview
bursts via POST /api/preview, and /affect polling — then prints latency
percentiles plus the server's own /healthz verdict (mem peak, CPU throttling,
gate depth). Pass criteria per the plan:

    - zero container OOM (docker inspect: OOMKilled=false)
    - /healthz mem.peak_mb comfortably below the cgroup limit
    - interactive gate_wait_s p95 small; preview p50 fast; honest 503s only

Caveat: with OAuth disabled every simulated user shares the single dev user, so
per-user limits (1 build slot, 120 previews/min) apply to the WHOLE sim. That
still saturates the exec gate (previews + affect sweeps all pass through it);
429/"already running" responses are counted as `slot_wait`, not failures.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request

BRACKET = """\
PARAMS = [
    {"name": "length", "default": 60.0, "type": "number", "min": 20, "max": 200, "unit": "mm"},
    {"name": "width", "default": 25.0, "type": "number", "min": 10, "max": 80, "unit": "mm"},
    {"name": "thickness", "default": 4.0, "type": "number", "min": 2, "max": 12, "unit": "mm"},
    {"name": "hole_d", "default": 5.0, "type": "number", "min": 2, "max": 12, "unit": "mm"},
]

from build123d import *

def build(params):
    L, W, T, D = params["length"], params["width"], params["thickness"], params["hole_d"]
    base = Box(L, W, T)
    upright = Pos(-L / 2 + T / 2, 0, W / 2 + T / 2) * Box(T, W, W)
    part = base + upright
    for x in (-L * 0.3, L * 0.3):
        part -= Pos(x, 0, 0) * Cylinder(D / 2, T * 4)
    return part
"""

ENCLOSURE = """\
PARAMS = [
    {"name": "length", "default": 90.0, "type": "number", "min": 40, "max": 200, "unit": "mm"},
    {"name": "width", "default": 60.0, "type": "number", "min": 30, "max": 150, "unit": "mm"},
    {"name": "height", "default": 30.0, "type": "number", "min": 15, "max": 80, "unit": "mm"},
    {"name": "wall", "default": 2.4, "type": "number", "min": 1.2, "max": 6, "unit": "mm"},
    {"name": "corner_r", "default": 6.0, "type": "number", "min": 1, "max": 15, "unit": "mm"},
    {"name": "boss_d", "default": 8.0, "type": "number", "min": 4, "max": 14, "unit": "mm"},
]

from build123d import *

def build(params):
    L, W, H = params["length"], params["width"], params["height"]
    t, r, bd = params["wall"], params["corner_r"], params["boss_d"]
    with BuildPart() as p:
        with BuildSketch():
            RectangleRounded(L, W, r)
        extrude(amount=H)
        with BuildSketch(Plane.XY.offset(t)):
            RectangleRounded(L - 2 * t, W - 2 * t, max(r - t, 0.5))
        extrude(amount=H, mode=Mode.SUBTRACT)
        with Locations(*[(x, y, t) for x in (-L / 2 + bd, L / 2 - bd) for y in (-W / 2 + bd, W / 2 - bd)]):
            Cylinder(bd / 2, H - t, align=(Align.CENTER, Align.CENTER, Align.MIN))
        with Locations(*[(x, y, t) for x in (-L / 2 + bd, L / 2 - bd) for y in (-W / 2 + bd, W / 2 - bd)]):
            Cylinder(bd / 6, H - t, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
    return p.part
"""

# heavy-ish organic-style stand-in: patterned + filleted lattice plate (drives
# tessellation, validation, facemap, and render cost well past the trivial case)
ORGANIC = """\
PARAMS = [
    {"name": "size", "default": 120.0, "type": "number", "min": 60, "max": 240, "unit": "mm"},
    {"name": "thickness", "default": 6.0, "type": "number", "min": 3, "max": 15, "unit": "mm"},
    {"name": "holes", "default": 6, "type": "integer", "min": 3, "max": 9, "unit": ""},
]

import math
from build123d import *

def build(params):
    S, T, N = params["size"], params["thickness"], int(params["holes"])
    part = Cylinder(S / 2, T)
    for i in range(N):
        a = 2 * math.pi * i / N
        r = S / (2.6 + (i % 3) * 0.7)
        part -= Pos(math.cos(a) * S / 3.2, math.sin(a) * S / 3.2, 0) * Cylinder(r / N * 1.6, T * 3)
    part -= Cylinder(S / 10, T * 3)
    return part
"""

PROGRAMS = {"bracket": BRACKET, "enclosure": ENCLOSURE, "organic": ORGANIC}


def req(base: str, path: str, payload: dict | None = None, timeout: float = 120.0):
    """(status, json_body, seconds). Never raises on HTTP errors."""
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return e.code, body, time.perf_counter() - t0
    except Exception as e:
        return 0, {"error": str(e)}, time.perf_counter() - t0


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat: dict[str, list[float]] = {}
        self.count: dict[str, int] = {}

    def add(self, kind: str, seconds: float | None = None):
        with self.lock:
            self.count[kind] = self.count.get(kind, 0) + 1
            if seconds is not None:
                self.lat.setdefault(kind, []).append(seconds)

    def report(self) -> str:
        lines = []
        for kind in sorted(set(self.count) | set(self.lat)):
            xs = sorted(self.lat.get(kind, []))
            n = self.count.get(kind, len(xs))
            if xs:
                p50 = statistics.median(xs)
                p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
                lines.append(f"  {kind:<22} n={n:<5} p50={p50 * 1000:7.0f}ms  p95={p95 * 1000:7.0f}ms  max={xs[-1] * 1000:7.0f}ms")
            else:
                lines.append(f"  {kind:<22} n={n}")
        return "\n".join(lines)


def user_loop(base: str, uidx: int, rounds: int, stats: Stats):
    names = list(PROGRAMS)
    status, proj, _ = req(base, "/api/projects", {"name": f"loadsim-{uidx}-{int(time.time())}"})
    if status != 200 or "id" not in proj:
        stats.add("project_create_fail")
        return
    pid = proj["id"]
    for rnd in range(rounds):
        prog = PROGRAMS[names[(uidx + rnd) % len(names)]]
        # 1) a "turn": one saved build (engine + validate + facemap + render + affect schedule)
        status, body, dt = req(base, f"/api/projects/{pid}/execute",
                               {"code": prog, "params": None, "label": f"r{rnd}"})
        if status == 200:
            stats.add("execute_ok", dt)
            gw = ((body.get("timings") or {}).get("gate_wait_s"))
            if gw is not None:
                stats.add("execute_gate_wait", float(gw))
        elif status == 429:
            stats.add("slot_wait")  # shared dev user: expected under concurrency
            time.sleep(1.5)
            continue
        elif status == 503:
            stats.add("shed_503", dt)
            time.sleep(2)
            continue
        else:
            stats.add(f"execute_err_{status}", dt)
            continue

        # 2) slider burst: 10 previews, values marching like a drag
        for i in range(10):
            vals = {"length": 60 + 4 * i} if "length" in prog else {"size": 100 + 5 * i}
            s, _, dt = req(base, "/api/preview", {"code": prog, "params": vals}, timeout=30)
            stats.add("preview_ok" if s == 200 else ("preview_503" if s == 503 else f"preview_{s}"),
                      dt if s == 200 else None)
            time.sleep(0.12)  # the frontend debounce cadence

        # 3) affect polling, like the params panel does
        run_id = body.get("run_id")
        if run_id:
            for _ in range(8):
                s, _, dt = req(base, f"/api/projects/{pid}/runs/{run_id}/affect", timeout=30)
                if s == 200:
                    stats.add("affect_ready", dt)
                    break
                stats.add("affect_202")
                time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    s0, health0, _ = req(args.base, "/healthz")
    if s0 != 200:
        raise SystemExit(f"server not healthy at {args.base}: {health0}")
    print("start healthz:", json.dumps(health0, indent=None))

    stats = Stats()
    t0 = time.perf_counter()
    threads = [threading.Thread(target=user_loop, args=(args.base, i, args.rounds, stats))
               for i in range(args.users)]
    for t in threads:
        t.start()
        time.sleep(0.4)  # stagger arrivals like real users
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    _, health1, _ = req(args.base, "/healthz")
    print(f"\n=== loadsim: {args.users} users × {args.rounds} rounds in {wall:.1f}s ===")
    print(stats.report())
    print("\nend healthz:", json.dumps(health1, indent=None))
    mem = health1.get("mem", {})
    cpu = health1.get("cpu", {})
    print(f"\nverdict: mem peak {mem.get('peak_mb', mem.get('rss_peak_mb'))}MB"
          f" / limit {mem.get('limit_mb')}MB | cpu throttled {cpu.get('nr_throttled', 'n/a')}×"
          f" | gate {json.dumps((health1.get('engine') or {}).get('gate'))}")


if __name__ == "__main__":
    main()
