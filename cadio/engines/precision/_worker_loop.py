"""Resident sandbox worker — executed with `python -I`, never imported by the host.

Pays the expensive OCCT/build123d import once, then serves jobs forever:
one JSON request per stdin line -> one JSON result per stdout line.
Job errors are returned as results; the worker only dies if its process does.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build123d  # noqa: F401  — the ~3s import, done once per worker
import _sandbox_runner as lib

print(json.dumps({"ready": True}), flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        result = lib.run_job(
            program=req["program"],
            outdir=req["outdir"],
            params=req.get("params"),
            preview=bool(req.get("preview")),
            coarse=bool(req.get("coarse")),
        )
    except Exception as exc:  # malformed request — report, keep serving
        result = {"ok": False, "error": f"worker request error: {exc}"}
    print(json.dumps(result), flush=True)
