# Forma

AI-agent 3D design platform — a **generic shell** (conversation, intent sheet,
versions, viewer, validation, export) over **pluggable geometry engines**,
shipped one vertical at a time. Full product spec:
[`../ai-3d-product-plan.md`](../ai-3d-product-plan.md). Living build doc:
[`docs/PROJECT.md`](docs/PROJECT.md).

**Status: Phase 0 vertical slice.** Engine 1 (precision — build123d/OCCT) runs
end to end: program → sandboxed execution → STL/STEP/GLB → validation gate →
browser viewer, plus a Claude agent loop on top.

## Setup

```sh
uv venv --python 3.12 .venv
uv pip install -e .
```

> First execution can be slow (~1–2 min): macOS verifies the OCCT dylib once.

## Use

```sh
# Run a program with parameter overrides, get validated STL/STEP/GLB
.venv/bin/python -m forma.cli run examples/simple_box.py --set length=200 --set wall=3

# Web playground: editor + params + 3D viewer + validation report
.venv/bin/uvicorn forma.api.app:app --reload    # → http://127.0.0.1:8000

# Agent REPL — bring any provider's key (Anthropic, OpenAI, Gemini, xAI/Grok, …)
export ANTHROPIC_API_KEY=...            # or OPENAI_API_KEY / GEMINI_API_KEY / XAI_API_KEY
.venv/bin/python -m forma.cli chat                       # default: anthropic/claude-opus-4-8
.venv/bin/python -m forma.cli chat --model openai/gpt-5.2
.venv/bin/python -m forma.cli chat --model gemini/gemini-3-pro
.venv/bin/python -m forma.cli chat --model xai/grok-4
```

The agent is provider-agnostic (LiteLLM): set `FORMA_MODEL` or pass `--model`
in `provider/model-id` form; the matching provider env-var key is used.

## Layout

```
forma/
  engines/base.py              engine contract (the thing that keeps the shell generic)
  engines/precision/           Engine 1: build123d/OCCT — sandbox runner + validators
  validation/mesh.py           shared mesh gate (watertight, winding, bbox-vs-BREP)
  agent/corpus.py              playbook corpus seed (socket-enclosure learnings)
  agent/orchestrator.py        Claude tool-use loop (run_cad, ask_user)
  api/app.py                   FastAPI: /api/execute, /api/runs, static artifacts
  cli.py                       forma run / forma chat
web/index.html                 three.js playground (editor + viewer + report)
examples/simple_box.py         reference program (PARAMS + build())
```

## Program contract (precision engine)

A program is a Python file defining `PARAMS` (list of parameter specs — every
requirement number is a parameter) and `build(params) -> Part` (build123d
algebra mode, mm). The runner owns export/measurement/validation; requirement
facts are asserted inside `build()`. See `examples/simple_box.py`.
