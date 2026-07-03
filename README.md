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
cd frontend && npm install && npm run build && cd ..
```

> First execution can be slow (~1–2 min): macOS verifies the OCCT dylib once.

## Use

**Web workspace (primary):** export your provider key first, then

```sh
.venv/bin/python -m forma.cli serve            # → http://127.0.0.1:8000
# (plain `uvicorn forma.api.app:app --reload` also works — all runtime data
#  lives in ~/.forma, so runs can't trigger the dev reloader)
```

API keys are entered in the UI (⚙ settings — stored in your browser, sent
per-connection); exporting provider env vars still works as a fallback.

Chat with the agent on the left (it asks clarifying questions as inline
forms), watch the model appear in the 3D viewer, tweak dimensions with the
Params sliders (rebuilds without the LLM), edit raw code in the Code tab,
reload old versions from Runs, export STL/STEP/GLB top-right. Set the model id
in the header (LiteLLM format, e.g. `gemini/gemini-2.5-pro`) and hit connect.

Frontend dev (hot reload): `cd frontend && npm run dev` → http://localhost:5173
(proxies API/websocket/artifacts to uvicorn on :8000).

```sh
# CLI alternatives:
.venv/bin/python -m forma.cli run examples/simple_box.py --set length=200 --set wall=3

# Agent REPL — bring any provider's key (Anthropic, OpenAI, Gemini, xAI/Grok, …)
export ANTHROPIC_API_KEY=...            # or OPENAI_API_KEY / GEMINI_API_KEY / XAI_API_KEY
.venv/bin/python -m forma.cli chat                       # default: anthropic/claude-opus-4-8
.venv/bin/python -m forma.cli chat --model gemini/gemini-2.5-pro
```

The agent is provider-agnostic (LiteLLM): set `FORMA_MODEL` or pass `--model`
as `provider/model-id`, using the **exact model id your provider serves** —
a wrong id 404s. To list what your key can access:

```sh
# Gemini (AI Studio key)
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" | grep '"name"'
# OpenAI
curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" | grep '"id"'
```

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
