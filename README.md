# CADIO

**Every Idea Deserves a Dimension.**

CADIO is an AI-native 3D creation platform that transforms ideas into fully
editable, production-ready 3D models. Describe your vision, provide reference
images, and collaborate with an intelligent design agent that asks the right
questions, generates tweakable parameters, enables localized edits, executes
custom modeling code, and exports assets in industry-standard formats.

This repo is the backend: a **generic shell** (conversation, intent sheet,
versions, viewer, validation, export) over **pluggable geometry engines**,
shipped one vertical at a time. Living build doc: [`docs/PROJECT.md`](docs/PROJECT.md).

**Status: Phase 0 vertical slice.** Engine 1 (precision — build123d/OCCT) runs
end to end: program → sandboxed execution → STL/STEP/GLB → validation gate →
browser viewer, plus a Claude agent loop on top.

The frontend lives in a separate repo: `cadio-frontend`.

## Setup

```sh
uv venv --python 3.12 .venv
uv pip install -e .
```

> First execution can be slow (~1–2 min): macOS verifies the OCCT dylib once.

## Run

**One command — that's it:**

```sh
uv run cadio            # → http://127.0.0.1:8000
```

Auto-reload is on and safe (all runtime data lives in `~/.cadio`, outside the
repo). No flags to remember. API keys are entered in the UI (settings, stored
in your browser); provider env vars still work as a fallback.

You land on the project home; create a project and start describing parts. The
agent asks clarifying questions, builds the model (and now *looks at its own
renders* to check the shape), and you fine-tune with realtime Params sliders,
edit code, reload versions, and export STL/STEP/GLB. Attach reference images or
STEP/STL geometry with the paperclip. Everything persists per project and
conversations resume across restarts.

For frontend dev (hot reload), run the backend here and the frontend from the
`cadio-frontend` repo: `npm run dev` → http://localhost:5173.
Other CLI verbs: `uv run cadio run <program.py>`, `uv run cadio chat`.

```sh
# run a program directly:
uv run cadio run examples/simple_box.py --set length=200 --set wall=3

# terminal agent (bring any provider's key):
export GEMINI_API_KEY=...    # or ANTHROPIC_API_KEY / OPENAI_API_KEY / XAI_API_KEY
uv run cadio chat --model gemini/gemini-2.5-pro
```

The agent is provider-agnostic (LiteLLM): in the web UI you pick the model from
a live list; on the CLI set `CADIO_MODEL` or pass `--model` as
`provider/model-id`. To list what a key can access:

```sh
# Gemini (AI Studio key)
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" | grep '"name"'
# OpenAI
curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" | grep '"id"'
```

## Layout

```
cadio/
  engines/base.py              engine contract (the thing that keeps the shell generic)
  engines/precision/           Engine 1: build123d/OCCT — sandbox runner + validators
  validation/mesh.py           shared mesh gate (watertight, winding, bbox-vs-BREP)
  agent/corpus.py              playbook corpus seed (socket-enclosure learnings)
  agent/orchestrator.py        Claude tool-use loop (run_cad, ask_user)
  api/app.py                   FastAPI: /api/execute, /api/runs, static artifacts
  cli.py                       cadio run / cadio chat
examples/simple_box.py         reference program (PARAMS + build())
```

## Program contract (precision engine)

A program is a Python file defining `PARAMS` (list of parameter specs — every
requirement number is a parameter) and `build(params) -> Part` (build123d
algebra mode, mm). The runner owns export/measurement/validation; requirement
facts are asserted inside `build()`. See `examples/simple_box.py`.
